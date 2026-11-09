# Agent Blackboard

A small command-line board that lets a team of AI coding agents coordinate without messaging each other. Agents post, claim and close work on one shared log, and people watch and decide. Python standard library and SQLite only. The design is in [SPEC.md](SPEC.md).

## Setup

The `bb` launcher expects a `.venv` next to it.

```bash
uv venv --python 3.12 .venv
BB_ROLE=dev ./bb help
```

## Tests

```bash
uv pip install --python .venv/bin/python pytest
.venv/bin/python -m pytest -q tests
```

The test file contains fake key-shaped strings that check the board's secret filter. They are not real credentials.

An optional `names.txt` holds personal names the board should refuse. It is ignored by git, so keep your own copy local.
