"""bb: the agent blackboard CLI (SPEC.md, B1-B8).

One SQLite file shared by the PM, dev, qa, ux and devops sessions. Standard
library only. Nothing on the board is approval (B4).
"""
import argparse
import html
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROLES = ("pm", "dev", "qa", "ux", "devops", "owner")
KINDS = ("task", "blocker", "needs_review", "decision", "info", "done")
DECISION_ROLES = ("pm", "owner")
TITLE_MAX, BODY_MAX = 200, 4000
DEFAULT_CLAIM = "4h"
SYSTEM = "bb"  # author of notes the tool writes itself, such as lapsed claims

RULE_B4 = ("Nothing on the board is approval. Push, deploy, merge, delete and spending money "
           "still need the human in their own session.")

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS entry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  author TEXT NOT NULL,
  to_role TEXT,
  status TEXT NOT NULL,
  owner TEXT,
  claim_expires_at TEXT,
  ref TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  expires_at TEXT
);
CREATE TABLE IF NOT EXISTS note (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL REFERENCES entry(id),
  author TEXT NOT NULL,
  text TEXT NOT NULL,
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (
  role TEXT PRIMARY KEY,
  last_note_id INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS note_no_update BEFORE UPDATE ON note
  BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;
CREATE TRIGGER IF NOT EXISTS note_no_delete BEFORE DELETE ON note
  BEGIN SELECT RAISE(ABORT, 'notes are append-only'); END;
"""


class Refused(Exception):
    """A rule said no. The message says which rule and why."""


def now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def parse_duration(text: str) -> timedelta:
    m = re.fullmatch(r"\s*(\d+)\s*([mhd])\s*", text or "")
    if not m or int(m.group(1)) == 0:
        raise Refused(f"bad duration {text!r}: use a number with m, h or d, like 30m, 2h or 1d")
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


def db_path() -> Path:
    return Path(os.environ.get("BB_DB") or HERE / "board.db")


def names_path() -> Path:
    return Path(os.environ.get("BB_NAMES") or HERE / "names.txt")


def connect(path=None) -> sqlite3.Connection:
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


class tx:
    """BEGIN IMMEDIATE ... COMMIT, so read-then-write steps can't interleave (B1)."""

    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, *_):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


# --- checks -----------------------------------------------------------------

def check_role(role, what="role") -> str:
    role = (role or "").strip().lower()
    if role not in ROLES:
        raise Refused(f"unknown {what} {role!r}: use one of {', '.join(ROLES)}")
    return role


def load_names() -> list[str]:
    p = names_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def check_text(*fields):
    """B5 no secrets, B7 no personal names."""
    names = load_names()
    for text in fields:
        if not text:
            continue
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                raise Refused("B5: that looks like a secret or key. Don't put secrets on the board.")
        for name in names:
            if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE):
                raise Refused("B7: entries name roles only, not people. "
                              "Say 'the owner' or 'the ITAM operator' instead.")


# --- core -------------------------------------------------------------------

def add_note(conn, entry_id, author, text, at=None):
    conn.execute("INSERT INTO note (entry_id, author, text, at) VALUES (?, ?, ?, ?)",
                 (entry_id, author, text, iso(at or now())))


def release_lapsed(conn, at=None) -> int:
    """B8: a claim past claim_expires_at goes back to open, with a note."""
    at = at or now()
    with tx(conn):
        rows = conn.execute("SELECT id, owner FROM entry WHERE status='claimed' AND claim_expires_at < ?",
                            (iso(at),)).fetchall()
        for r in rows:
            conn.execute("UPDATE entry SET status='open', owner=NULL, claim_expires_at=NULL, updated_at=? "
                         "WHERE id=?", (iso(at), r["id"]))
            add_note(conn, r["id"], SYSTEM, f"claim by {r['owner']} lapsed", at)
    return len(rows)


def get(conn, entry_id) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM entry WHERE id=?", (entry_id,)).fetchone()
    if row is None:
        raise Refused(f"no entry #{entry_id}")
    return row


def post(conn, role, kind, title, body="", to_role=None, ref=None, expires=None) -> int:
    role = check_role(role)
    if kind not in KINDS:
        raise Refused(f"unknown kind {kind!r}: use one of {', '.join(KINDS)}")
    if kind == "decision" and role not in DECISION_ROLES:
        raise Refused("B3: only pm and owner can post decision entries.")
    title, body = (title or "").strip(), (body or "").strip()
    if not title:
        raise Refused("a title is required")
    if len(title) > TITLE_MAX:
        raise Refused(f"title is {len(title)} characters; the limit is {TITLE_MAX}")
    if len(body) > BODY_MAX:
        raise Refused(f"body is {len(body)} characters; the limit is {BODY_MAX}")
    if to_role:
        to_role = check_role(to_role, "--to role")
    check_text(title, body, ref)
    t = now()
    exp = iso(t + parse_duration(expires)) if expires else None
    status = "done" if kind == "done" else "open"
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO entry (kind, title, body, author, to_role, status, ref, created_at, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, title, body, role, to_role, status, ref, iso(t), iso(t), exp))
        entry_id = cur.lastrowid
        add_note(conn, entry_id, role, f"posted {kind}", t)
    return entry_id


def claim(conn, role, entry_id, for_=DEFAULT_CLAIM):
    role = check_role(role)
    release_lapsed(conn)
    t = now()
    until = iso(t + parse_duration(for_))
    with tx(conn):
        cur = conn.execute(
            "UPDATE entry SET status='claimed', owner=?, claim_expires_at=?, updated_at=? "
            "WHERE id=? AND (status='open' OR (status='claimed' AND (claim_expires_at < ? OR owner=?)))",
            (role, until, iso(t), entry_id, iso(t), role))
        if cur.rowcount != 1:
            row = get(conn, entry_id)
            if row["status"] == "claimed":
                raise Refused(f"B1: #{entry_id} is claimed by {row['owner']} until {row['claim_expires_at']}")
            raise Refused(f"#{entry_id} is {row['status']} and can't be claimed")
        add_note(conn, entry_id, role, f"claimed until {until}", t)
    return until


def _finish(conn, role, entry_id, status, note):
    role = check_role(role)
    check_text(note)
    release_lapsed(conn)
    t = now()
    with tx(conn):
        row = get(conn, entry_id)
        if row["status"] in ("done", "cancelled"):
            raise Refused(f"#{entry_id} is already {row['status']}")
        if status == "done":
            if row["status"] == "claimed" and role not in (row["owner"], "pm"):
                raise Refused(f"B2: #{entry_id} is claimed by {row['owner']}; only they or pm can mark it done")
            if row["status"] == "open" and role not in (row["author"], row["to_role"], "pm"):
                raise Refused(f"B2: claim #{entry_id} first, or ask {row['author']} or pm to close it")
        elif role not in (row["author"], "pm"):
            raise Refused(f"B2: only {row['author']} (the author) or pm can cancel #{entry_id}")
        conn.execute("UPDATE entry SET status=?, claim_expires_at=NULL, updated_at=? WHERE id=?",
                     (status, iso(t), entry_id))
        add_note(conn, entry_id, role, f"{status}" + (f": {note}" if note else ""), t)


def done(conn, role, entry_id, note=""):
    _finish(conn, role, entry_id, "done", note)


def cancel(conn, role, entry_id, note=""):
    _finish(conn, role, entry_id, "cancelled", note)


def note(conn, role, entry_id, text):
    role = check_role(role)
    text = (text or "").strip()
    if not text:
        raise Refused("note text is required")
    if len(text) > BODY_MAX:
        raise Refused(f"note is {len(text)} characters; the limit is {BODY_MAX}")
    check_text(text)
    t = now()
    with tx(conn):
        get(conn, entry_id)
        conn.execute("UPDATE entry SET updated_at=? WHERE id=?", (iso(t), entry_id))
        add_note(conn, entry_id, role, text, t)


def expire(conn) -> tuple[int, int]:
    """Cancel open entries past expires_at, release lapsed claims."""
    lapsed = release_lapsed(conn)
    t = now()
    with tx(conn):
        rows = conn.execute("SELECT id FROM entry WHERE status='open' AND expires_at IS NOT NULL "
                            "AND expires_at < ?", (iso(t),)).fetchall()
        for r in rows:
            conn.execute("UPDATE entry SET status='cancelled', updated_at=? WHERE id=?", (iso(t), r["id"]))
            add_note(conn, r["id"], SYSTEM, "expired", t)
    return len(rows), lapsed


def entries(conn, open_only=False, mine=None, kind=None, to_role=None, show_all=False):
    release_lapsed(conn)
    sql, args = "SELECT * FROM entry WHERE 1=1", []
    if open_only:
        sql += " AND status IN ('open', 'claimed')"
    elif not show_all:
        sql += " AND status NOT IN ('done', 'cancelled')"
    if not show_all:
        sql += " AND NOT (status='open' AND expires_at IS NOT NULL AND expires_at < ?)"
        args.append(iso(now()))
    if mine:
        sql += " AND (owner=? OR author=? OR to_role=?)"
        args += [mine, mine, mine]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if to_role:
        sql += " AND to_role=?"
        args.append(to_role)
    return conn.execute(sql + " ORDER BY id", args).fetchall()


def notes(conn, entry_id):
    return conn.execute("SELECT * FROM note WHERE entry_id=? ORDER BY id", (entry_id,)).fetchall()


def watch(conn, role):
    """Entries changed by someone else since this role's last watch (BA4)."""
    role = check_role(role)
    release_lapsed(conn)
    with tx(conn):
        r = conn.execute("SELECT last_note_id FROM seen WHERE role=?", (role,)).fetchone()
        last = r["last_note_id"] if r else 0
        new = conn.execute("SELECT * FROM note WHERE id > ? ORDER BY id", (last,)).fetchall()
        top = new[-1]["id"] if new else last
        conn.execute("INSERT INTO seen (role, last_note_id) VALUES (?, ?) "
                     "ON CONFLICT(role) DO UPDATE SET last_note_id=excluded.last_note_id", (role, top))
    changed = {}
    for n in new:
        if n["author"] != role:
            changed.setdefault(n["entry_id"], []).append(n)
    return [(get(conn, i), ns) for i, ns in changed.items()]


# --- output -----------------------------------------------------------------

def line(row) -> str:
    who = row["author"] + (f"→{row['to_role']}" if row["to_role"] else "")
    extra = f" (claimed by {row['owner']} until {row['claim_expires_at']})" if row["status"] == "claimed" else ""
    ref = f" [{row['ref']}]" if row["ref"] else ""
    return f"#{row['id']:<4} {row['kind']:<12} {row['status']:<9} {who:<12} {row['title']}{ref}{extra}"


def export_status(conn) -> dict:
    release_lapsed(conn)
    rows = conn.execute("SELECT * FROM entry ORDER BY id").fetchall()
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    t = now()
    recent = iso(t - timedelta(days=1))
    keep = [dict(r) for r in rows if r["status"] in ("open", "claimed") or r["updated_at"] >= recent]
    return {"generated_at": iso(t), "rule": RULE_B4, "counts": counts, "entries": keep}


VIEW_CSS = """
:root { --bg:#f2f4f6; --sheet:#fff; --ink:#142033; --soft:#4a5668; --rule:#d7dde5;
  --open:#2340b0; --claimed:#8a5a00; --done:#1f7a4d; --cancelled:#6b7280; --blocker:#b3261e; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  --bg:#0f1520; --sheet:#161e2c; --ink:#e6ebf2; --soft:#a3aec0; --rule:#2a3547;
  --open:#8ea4ff; --claimed:#f0b84a; --done:#5fcf95; --cancelled:#9aa3af; --blocker:#ff8a80; color-scheme:dark; } }
:root[data-theme="dark"] { --bg:#0f1520; --sheet:#161e2c; --ink:#e6ebf2; --soft:#a3aec0; --rule:#2a3547;
  --open:#8ea4ff; --claimed:#f0b84a; --done:#5fcf95; --cancelled:#9aa3af; --blocker:#ff8a80; color-scheme:dark; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:400 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; padding:24px 16px; }
main { max-width:960px; margin:0 auto; }
h1 { font-size:1.5rem; margin:0 0 4px; } h2 { font-size:1.05rem; margin:28px 0 8px; }
.meta, .rule { color:var(--soft); font-size:0.9rem; margin:0 0 4px; }
.card { background:var(--sheet); border:1px solid var(--rule); border-left:4px solid var(--open); border-radius:6px; padding:10px 14px; margin:8px 0; }
.card.claimed { border-left-color:var(--claimed); } .card.done { border-left-color:var(--done); }
.card.cancelled { border-left-color:var(--cancelled); } .card.blocker { border-left-color:var(--blocker); }
.head { display:flex; flex-wrap:wrap; gap:4px 10px; align-items:baseline; }
.id, .tag { color:var(--soft); font-size:0.85rem; } .title { font-weight:600; overflow-wrap:anywhere; }
.body { margin:6px 0 0; white-space:pre-wrap; overflow-wrap:anywhere; }
details { margin-top:6px; font-size:0.85rem; color:var(--soft); }
"""


def view_html(conn) -> str:
    release_lapsed(conn)
    rows = conn.execute("SELECT * FROM entry ORDER BY id DESC").fetchall()
    e = html.escape
    groups = [("Blockers", lambda r: r["kind"] == "blocker" and r["status"] in ("open", "claimed")),
              ("Open and claimed", lambda r: r["kind"] != "blocker" and r["status"] in ("open", "claimed")),
              ("Done and cancelled", lambda r: r["status"] in ("done", "cancelled"))]
    parts = []
    for heading, test in groups:
        sel = [r for r in rows if test(r)]
        parts.append(f"<h2>{e(heading)} ({len(sel)})</h2>")
        if not sel:
            parts.append('<p class="meta">None.</p>')
        for r in sel:
            who = r["author"] + (f" → {r['to_role']}" if r["to_role"] else "")
            claim_txt = f" · claimed by {r['owner']} until {r['claim_expires_at']}" if r["status"] == "claimed" else ""
            ref = f" · {e(r['ref'])}" if r["ref"] else ""
            hist = "".join(f"<li>{e(n['at'])} {e(n['author'])}: {e(n['text'])}</li>" for n in notes(conn, r["id"]))
            cls = f"card {r['status']}" + (" blocker" if r["kind"] == "blocker" else "")
            parts.append(
                f'<section class="{cls}"><div class="head"><span class="id">#{r["id"]}</span>'
                f'<span class="tag">{e(r["kind"])} · {e(r["status"])}</span><span class="title">{e(r["title"])}</span></div>'
                f'<p class="meta">{e(who)}{e(claim_txt)}{ref} · updated {e(r["updated_at"])}</p>'
                + (f'<p class="body">{e(r["body"])}</p>' if r["body"] else "")
                + f"<details><summary>History</summary><ul>{hist}</ul></details></section>")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>Agent Blackboard</title><style>{VIEW_CSS}</style></head><body><main>"
            f"<h1>Agent blackboard</h1><p class=\"meta\">Snapshot {e(iso(now()))}. Read-only.</p>"
            f'<p class="rule">{e(RULE_B4)}</p>{"".join(parts)}</main></body></html>\n')


# --- CLI --------------------------------------------------------------------

HELP = f"""bb: the agent blackboard. Set your role with --as ROLE or BB_ROLE.

  bb post KIND TITLE [--body TEXT] [--to ROLE] [--ref REF] [--expires 2h|1d]
  bb list [--open] [--mine] [--kind K] [--to ROLE] [--all]
  bb show ID
  bb claim ID [--for 4h]
  bb done ID [--note TEXT]
  bb cancel ID [--note TEXT]
  bb note ID TEXT
  bb watch
  bb view [--out board.html]
  bb expire
  bb export-status [--out status.json]

Kinds: {', '.join(KINDS)}. Roles: {', '.join(ROLES)}.
{RULE_B4}
"""


def build_parser():
    p = argparse.ArgumentParser(prog="bb", add_help=False)
    p.add_argument("--as", dest="role", default=os.environ.get("BB_ROLE"))
    p.add_argument("--db", type=Path)
    # --as and --db also work after the command; SUPPRESS keeps the top-level value when they're absent
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as", dest="role", default=argparse.SUPPRESS)
    common.add_argument("--db", type=Path, default=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", parser_class=lambda **kw: argparse.ArgumentParser(parents=[common], **kw))
    s = sub.add_parser("post"); s.add_argument("kind"); s.add_argument("title")
    s.add_argument("--body", default=""); s.add_argument("--to"); s.add_argument("--ref"); s.add_argument("--expires")
    s = sub.add_parser("list"); s.add_argument("--open", action="store_true"); s.add_argument("--mine", action="store_true")
    s.add_argument("--kind"); s.add_argument("--to"); s.add_argument("--all", action="store_true")
    s = sub.add_parser("show"); s.add_argument("id", type=int)
    s = sub.add_parser("claim"); s.add_argument("id", type=int); s.add_argument("--for", dest="for_", default=DEFAULT_CLAIM)
    s = sub.add_parser("done"); s.add_argument("id", type=int); s.add_argument("--note", default="")
    s = sub.add_parser("cancel"); s.add_argument("id", type=int); s.add_argument("--note", default="")
    s = sub.add_parser("note"); s.add_argument("id", type=int); s.add_argument("text")
    sub.add_parser("watch")
    s = sub.add_parser("view"); s.add_argument("--out", type=Path, default=HERE / "board.html")
    sub.add_parser("expire")
    s = sub.add_parser("export-status"); s.add_argument("--out", type=Path)
    sub.add_parser("help")
    return p


NEEDS_ROLE = {"post", "claim", "done", "cancel", "note", "watch"}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd in (None, "help"):
        print(HELP)
        return 0
    conn = connect(args.db)
    try:
        role = check_role(args.role) if args.cmd in NEEDS_ROLE or getattr(args, "mine", False) else args.role
        c = args.cmd
        if c == "post":
            i = post(conn, role, args.kind, args.title, args.body, args.to, args.ref, args.expires)
            print(f"posted #{i}")
        elif c == "list":
            for r in entries(conn, args.open, role if args.mine else None, args.kind, args.to, args.all):
                print(line(r))
        elif c == "show":
            r = get(conn, args.id)
            print(line(r))
            if r["body"]:
                print("\n" + r["body"] + "\n")
            for n in notes(conn, args.id):
                print(f"  {n['at']} {n['author']}: {n['text']}")
        elif c == "claim":
            print(f"claimed #{args.id} until {claim(conn, role, args.id, args.for_)}")
        elif c == "done":
            done(conn, role, args.id, args.note)
            print(f"#{args.id} done")
        elif c == "cancel":
            cancel(conn, role, args.id, args.note)
            print(f"#{args.id} cancelled")
        elif c == "note":
            note(conn, role, args.id, args.text)
            print(f"noted on #{args.id}")
        elif c == "watch":
            changed = watch(conn, role)
            if not changed:
                print("nothing new")
            for r, ns in changed:
                print(line(r))
                for n in ns:
                    print(f"    {n['author']}: {n['text']}")
        elif c == "view":
            args.out.write_text(view_html(conn))
            print(f"wrote {args.out}")
        elif c == "expire":
            cancelled, lapsed = expire(conn)
            print(f"expired {cancelled} entr{'y' if cancelled == 1 else 'ies'}, released {lapsed} lapsed claim(s)")
        elif c == "export-status":
            text = json.dumps(export_status(conn), indent=2)
            if args.out:
                args.out.write_text(text + "\n")
                print(f"wrote {args.out}")
            else:
                print(text)
        return 0
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
