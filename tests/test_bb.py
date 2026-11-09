"""Acceptance tests for SPEC.md (BA1-BA8) plus the CLI edges."""
import json
import multiprocessing as mp
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import bb  # noqa: E402


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BB_DB", str(tmp_path / "board.db"))
    monkeypatch.setenv("BB_NAMES", str(tmp_path / "names.txt"))
    monkeypatch.delenv("BB_ROLE", raising=False)


@pytest.fixture
def conn():
    c = bb.connect()
    yield c
    c.close()


@pytest.fixture
def clock(monkeypatch):
    """Moves bb's clock forward without sleeping."""
    state = {"t": bb.now()}
    monkeypatch.setattr(bb, "now", lambda: state["t"])

    def advance(**kw):
        state["t"] += timedelta(**kw)
    return advance


def note_texts(conn, i):
    return [n["text"] for n in bb.notes(conn, i)]


# BA1: concurrent claims, exactly one wins

def _race(db, role, entry_id, barrier, out):
    c = bb.connect(db)
    barrier.wait()
    try:
        bb.claim(c, role, entry_id)
        out.put((role, True))
    except bb.Refused:
        out.put((role, False))
    finally:
        c.close()


def test_ba1_concurrent_claims_one_wins(conn, tmp_path):
    i = bb.post(conn, "pm", "task", "Race me")
    ctx = mp.get_context("spawn")
    roles = ["dev", "qa", "ux", "devops"]
    barrier, out = ctx.Barrier(len(roles)), ctx.Queue()
    procs = [ctx.Process(target=_race, args=(str(tmp_path / "board.db"), r, i, barrier, out)) for r in roles]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
    results = [out.get(timeout=5) for _ in roles]
    winners = [r for r, ok in results if ok]
    assert len(winners) == 1
    assert bb.get(conn, i)["owner"] == winners[0]
    assert sum(t.startswith("claimed") for t in note_texts(conn, i)) == 1


def test_ba1_second_claim_says_who_holds_it(conn):
    i = bb.post(conn, "pm", "task", "One owner")
    bb.claim(conn, "dev", i)
    with pytest.raises(bb.Refused, match="claimed by dev"):
        bb.claim(conn, "qa", i)


# BA2: B2 and B3

def test_ba2_only_owner_or_pm_marks_claimed_done(conn):
    i = bb.post(conn, "pm", "task", "Fix it")
    bb.claim(conn, "dev", i)
    with pytest.raises(bb.Refused, match="B2: .*claimed by dev"):
        bb.done(conn, "qa", i)
    bb.done(conn, "dev", i, "abc1234")
    assert bb.get(conn, i)["status"] == "done"
    j = bb.post(conn, "qa", "task", "Another")
    bb.claim(conn, "dev", j)
    bb.done(conn, "pm", j)
    assert bb.get(conn, j)["status"] == "done"


def test_ba2_open_entry_done_needs_author_addressee_or_pm(conn):
    i = bb.post(conn, "qa", "needs_review", "Look at this", to_role="dev")
    with pytest.raises(bb.Refused, match="B2"):
        bb.done(conn, "ux", i)
    bb.done(conn, "dev", i)


def test_ba2_only_author_or_pm_cancels(conn):
    i = bb.post(conn, "qa", "task", "Maybe")
    with pytest.raises(bb.Refused, match="B2: only qa"):
        bb.cancel(conn, "dev", i)
    bb.cancel(conn, "pm", i, "not needed")
    assert bb.get(conn, i)["status"] == "cancelled"
    with pytest.raises(bb.Refused, match="already cancelled"):
        bb.done(conn, "pm", i)


def test_ba2_decisions_are_pm_or_owner(conn):
    for role in ("dev", "qa", "ux", "devops"):
        with pytest.raises(bb.Refused, match="B3"):
            bb.post(conn, role, "decision", "We ship Friday")
    bb.post(conn, "pm", "decision", "PR order is 1, 2, 3")
    bb.post(conn, "owner", "decision", "Go")


# BA3: secrets refused

@pytest.mark.parametrize("text", [
    "key AKIAABCDEFGHIJKLMNOP here",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "token ghp_abcdefghijklmnopqrstuvwxyz0123",
    "sk-proj_abcdefghijklmnopqrst",
])
def test_ba3_secrets_refused(conn, text):
    with pytest.raises(bb.Refused, match="B5"):
        bb.post(conn, "dev", "info", "creds", body=text)
    i = bb.post(conn, "dev", "info", "clean")
    with pytest.raises(bb.Refused, match="B5"):
        bb.note(conn, "dev", i, text)
    with pytest.raises(bb.Refused, match="B5"):
        bb.post(conn, "dev", "info", "ref", ref=text)


def test_ba3_ordinary_words_pass(conn):
    bb.post(conn, "dev", "info", "risk-register and task-list updated", body="asks about disk-space")


# BA4: watch shows each change once per role

def test_ba4_watch_once_per_role(conn):
    i = bb.post(conn, "pm", "task", "First")
    assert [r["id"] for r, _ in bb.watch(conn, "dev")] == [i]
    assert bb.watch(conn, "dev") == []
    assert [r["id"] for r, _ in bb.watch(conn, "qa")] == [i]
    bb.claim(conn, "qa", i)
    seen = bb.watch(conn, "dev")
    assert [r["id"] for r, _ in seen] == [i] and [n["text"][:7] for n in seen[0][1]] == ["claimed"]
    assert bb.watch(conn, "qa") == []  # its own change isn't news to it
    assert bb.watch(conn, "dev") == []


# BA5: view is one self-contained file, no JS, dark mode

def test_ba5_view(conn):
    bb.post(conn, "pm", "blocker", "Git <blocked>", body="needs the owner", to_role="owner")
    bb.post(conn, "dev", "task", "Build bb")
    page = bb.view_html(conn)
    assert page.startswith("<!doctype html>") and "<title>Agent Blackboard</title>" in page
    assert "<script" not in page and "src=" not in page and "<link" not in page
    assert "prefers-color-scheme: dark" in page and ':root[data-theme="dark"]' in page
    assert "Git &lt;blocked&gt;" in page and "<blocked>" not in page
    assert bb.RULE_B4 in page


# BA6: every change leaves a note; notes can't be changed

def test_ba6_every_change_noted(conn, clock):
    i = bb.post(conn, "pm", "task", "Track me", expires="1h")
    bb.claim(conn, "dev", i, "1h")
    bb.note(conn, "dev", i, "halfway")
    bb.done(conn, "dev", i, "abc1234")
    j = bb.post(conn, "pm", "task", "Cancel me")
    bb.cancel(conn, "pm", j)
    k = bb.post(conn, "pm", "info", "Expire me", expires="1h")
    clock(hours=2)
    bb.expire(conn)
    assert note_texts(conn, i) == ["posted task", note_texts(conn, i)[1], "halfway", "done: abc1234"]
    assert note_texts(conn, i)[1].startswith("claimed until")
    assert note_texts(conn, j)[-1] == "cancelled" and note_texts(conn, k)[-1] == "expired"
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("UPDATE note SET text='x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM note")


# BA7: names refused

def test_ba7_names_refused(conn, tmp_path):
    (tmp_path / "names.txt").write_text("# deny list\nPat Example\nJordan  # trailing comment\n")
    with pytest.raises(bb.Refused, match="B7"):
        bb.post(conn, "dev", "info", "Ask pat example about it")
    with pytest.raises(bb.Refused, match="B7"):
        bb.post(conn, "dev", "info", "ok", body="JORDAN said so")
    i = bb.post(conn, "dev", "info", "Ask the ITAM operator")
    with pytest.raises(bb.Refused, match="B7"):
        bb.note(conn, "dev", i, "Jordan agreed")
    with pytest.raises(bb.Refused, match="B7"):
        bb.done(conn, "dev", i, "per Jordan")
    bb.note(conn, "dev", i, "Jordanian date formats")  # whole words only


def test_ba7_roles_only(conn):
    with pytest.raises(bb.Refused, match="unknown role"):
        bb.post(conn, "alice", "task", "x")
    with pytest.raises(bb.Refused, match="unknown --to role"):
        bb.post(conn, "dev", "task", "x", to_role="bob")


# BA8: lapsed claims

def test_ba8_lapsed_claim_can_be_taken_and_is_noted(conn, clock):
    i = bb.post(conn, "pm", "task", "Stale")
    bb.claim(conn, "dev", i, "2h")
    clock(hours=1)
    with pytest.raises(bb.Refused):
        bb.claim(conn, "qa", i)
    bb.claim(conn, "dev", i, "2h")  # renew
    clock(hours=3)
    assert bb.entries(conn)[0]["status"] == "open"
    bb.claim(conn, "qa", i)
    assert bb.get(conn, i)["owner"] == "qa"
    assert "claim by dev lapsed" in note_texts(conn, i)


def test_default_claim_is_four_hours(conn, clock):
    i = bb.post(conn, "pm", "task", "x")
    bb.claim(conn, "dev", i)
    clock(hours=3, minutes=59)
    assert bb.get(conn, i)["status"] == "claimed"
    clock(minutes=2)
    bb.release_lapsed(conn)
    assert bb.get(conn, i)["status"] == "open"


# list, expiry, export

def test_list_filters_and_expiry(conn, clock):
    a = bb.post(conn, "pm", "task", "A", to_role="dev")
    b = bb.post(conn, "qa", "blocker", "B")
    c = bb.post(conn, "dev", "info", "C", expires="1h")
    d = bb.post(conn, "dev", "done", "D shipped")
    ids = lambda rows: [r["id"] for r in rows]  # noqa: E731
    assert ids(bb.entries(conn)) == [a, b, c]
    assert ids(bb.entries(conn, show_all=True)) == [a, b, c, d]
    assert ids(bb.entries(conn, kind="blocker")) == [b]
    assert ids(bb.entries(conn, to_role="dev")) == [a]
    assert ids(bb.entries(conn, mine="dev")) == [a, c]
    clock(hours=2)
    assert ids(bb.entries(conn)) == [a, b]
    assert bb.expire(conn) == (1, 0)
    assert bb.get(conn, c)["status"] == "cancelled"


def test_export_status(conn):
    bb.post(conn, "pm", "task", "Open one")
    i = bb.post(conn, "pm", "task", "Closed one")
    bb.done(conn, "pm", i)
    data = json.loads(json.dumps(bb.export_status(conn)))
    assert data["counts"] == {"open": 1, "done": 1}
    assert [e["title"] for e in data["entries"]] == ["Open one", "Closed one"]
    assert data["rule"] == bb.RULE_B4


def test_input_limits(conn):
    with pytest.raises(bb.Refused, match="title is required"):
        bb.post(conn, "dev", "task", "  ")
    with pytest.raises(bb.Refused, match="limit is 200"):
        bb.post(conn, "dev", "task", "x" * 201)
    with pytest.raises(bb.Refused, match="limit is 4000"):
        bb.post(conn, "dev", "task", "x", body="y" * 4001)
    with pytest.raises(bb.Refused, match="unknown kind"):
        bb.post(conn, "dev", "chore", "x")
    with pytest.raises(bb.Refused, match="bad duration"):
        bb.post(conn, "dev", "task", "x", expires="soon")
    with pytest.raises(bb.Refused, match="no entry #99"):
        bb.claim(conn, "dev", 99)


# CLI

def run(*args, role=None):
    import os
    env = dict(os.environ)
    if role:
        env["BB_ROLE"] = role
    return subprocess.run([str(HERE / "bb"), *args], capture_output=True, text=True, env=env)


def test_cli_end_to_end(tmp_path):
    assert run("post", "task", "From the CLI", "--to", "qa", role="dev").stdout.strip() == "posted #1"
    assert run("claim", "1", "--as", "qa").returncode == 0
    r = run("--as", "ux", "claim", "1")
    assert r.returncode == 1 and "refused: B1" in r.stderr
    assert run("done", "1", "--note", "fine", role="qa").returncode == 0
    assert "qa: done: fine" in run("show", "1").stdout
    assert run("view", "--out", str(tmp_path / "b.html")).returncode == 0 and (tmp_path / "b.html").exists()
    assert json.loads(run("export-status").stdout)["counts"] == {"done": 1}
    assert bb.RULE_B4 in run("help").stdout


def test_cli_needs_a_role():
    r = run("post", "task", "x")
    assert r.returncode == 1 and "unknown role" in r.stderr
