# Agent blackboard: spec (approved by PM; built 2026-10-25)

A shared board where the Lifecycle Copilot agent sessions (PM, dev, qa, ux, devops) post and pick up work, instead of messaging each other one to one. It is team tooling, not product. It lives outside the `Lifecycle_copilot` repo and isn't part of any release PR.

## Goals

- One place to see what's open, who has it, and what's blocked.
- Fewer cross-session messages. A message only says "check the board" when something is urgent.
- The owner can read the whole board at a glance without asking an agent.

## Not goals

- No hosting, no network service, nothing on the VM.
- No auto-assignment and no dependency graph.
- It never grants permission for anything.

## Data

One SQLite file, `blackboard/board.db`, in WAL mode with busy_timeout set. Override the path with `BB_DB`.

| Column | Notes |
|---|---|
| id | integer, assigned automatically |
| kind | `task`, `blocker`, `needs_review`, `decision`, `info`, `done` |
| title | required, at most 200 characters |
| body | optional, at most 4,000 characters |
| author | role of the session that posted it |
| to_role | optional addressee role |
| status | `open`, `claimed`, `done`, `cancelled` |
| owner | role that claimed the entry |
| claim_expires_at | set on claim (default 4h, `--for 2h`); after it, the claim lapses and the entry is open again |
| ref | optional pointer such as a file path, commit hash or risk id (R9) |
| created_at, updated_at | UTC ISO 8601 |
| expires_at | optional; `list` hides expired open entries and `expire` marks them cancelled |

Entries name roles only, never people. Titles, bodies and notes use "the ITAM operator", "the owner" and so on.

A second table, `note`, holds one line of history per change to an entry: entry_id, author, text, at. History is only ever added to.

## Roles

`pm`, `dev`, `qa`, `ux`, `devops`, `owner`. Every command takes `--as ROLE`, or reads it from `BB_ROLE`. Unknown roles are rejected.

## Rules

- **B1.** Claiming is atomic: `UPDATE ... SET status='claimed', owner=? WHERE id=? AND (status='open' OR (status='claimed' AND (claim_expires_at < now OR owner = me)))`. If no row changes, the claim fails and the command says who holds the entry.
- **B2.** Only the entry's owner or PM can mark a claimed entry done. Only the author or PM can cancel an entry.
- **B3.** Only `pm` and `owner` can post `decision` entries.
- **B4.** Nothing on the board is approval. Push, deploy, merge, delete and spending money still need the human in their own session. The CLI prints this rule in `bb help`.
- **B5.** No secrets. A post is refused if it looks like a key: AWS `AKIA…`, `-----BEGIN`, `ghp_`, or `sk-`.
- **B6.** Every change adds a `note` row, so the history stays complete.
- **B7.** Roles only, no personal names. `author`, `owner` and `to_role` take role values only. Text fields are checked against a small deny list kept in `blackboard/names.txt`, which is not in the repo, and a match is refused.
- **B8.** Claims expire. A claim lapses at `claim_expires_at`. `list` and `claim` treat a lapsed claim as open, and the next change adds a note: "claim by ROLE lapsed". The owner of a claim can renew it with `bb claim ID` again.

## CLI (`bb`)

```
bb post KIND TITLE [--body TEXT] [--to ROLE] [--ref REF] [--expires 2h|1d]
bb list [--open] [--mine] [--kind K] [--to ROLE] [--all]
bb show ID
bb claim ID [--for 4h]
bb done ID [--note TEXT]
bb cancel ID [--note TEXT]
bb note ID TEXT
bb watch            # entries changed since this role's last watch
bb view [--out board.html]   # read-only HTML snapshot for the owner
bb expire           # cancel expired open entries, release lapsed claims
bb export-status [--out status.json]   # for PM's Claude Desktop board
```

## How agents use it

At the start of each turn, run `bb watch`. When work finishes, run `done` with a note that includes the commit hash. When blocked, post a `blocker` with `--to` set to the role that can unblock it. PM writes the `blackboard` skill and its rules, just as `git-flow` does for git. This spec covers only the CLI.

The board feeds PM's Claude Desktop status board and doesn't replace it. PM builds `status.json` from `bb export-status`.

## Acceptance

- **BA1.** Two concurrent claims on the same entry: exactly one succeeds.
- **BA2.** B2 and B3 are enforced, and the error says why.
- **BA3.** A post containing `AKIA` followed by 16 characters is refused.
- **BA4.** `watch` shows each change once per role.
- **BA5.** `view` writes one self-contained HTML file that works with JavaScript off and supports dark mode.
- **BA6.** Every change leaves a note row.
- **BA7.** A post or note that contains a name from `names.txt` is refused.
- **BA8.** A claim past `claim_expires_at` can be claimed by another role, and the lapse is noted.

## Files

`bb` (wrapper), `bb.py` (standard library only), `names.txt` (B7 deny list, filled by PM or the owner), `tests/test_bb.py` (BA1–BA8), `.venv` (Python 3.12 + pytest for the tests). Run the tests with `.venv/bin/python -m pytest -q tests`.

## Seed at build time

R9 (the landing test fix, done but not committed), PR1, PR2, PR3, the git blocker (Xcode license, needs the owner), and the owner's open asks.

## PM's answers (2026-10-25)

1. The location outside the repo is OK.
2. PM writes the skill and rules; dev writes the CLI spec.
3. The board feeds the Claude Desktop board.
4. Seed at build time.
