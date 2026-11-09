#!/bin/sh
# Agent blackboard CLI. See SPEC.md, or run: bb help
D="$(cd "$(dirname "$0")" && pwd)"
exec "$D/.venv/bin/python" -I "$D/bb.py" "$@"
