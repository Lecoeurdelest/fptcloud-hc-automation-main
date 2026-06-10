---
name: dev-environment
description: How to run lint/mypy/tests/docker for this repo on the dev Windows machine
metadata:
  type: project
---

This repo targets **Python 3.11** (`requires-python >=3.11,<3.12`), but the
machine's default `python` is 3.12 with **no dev tools**. The full dev
toolchain (ruff, mypy, pytest, pydantic, redis, fakeredis, structlog,
coverage) is installed under **3.11 only**, at
`C:/Users/Admin/AppData/Local/Programs/Python/Python311/python.exe`. Run all
gates with that interpreter and `PYTHONPATH=src` (package is not pip-installed
editable):

- lint: `python -m ruff format --check src tests && python -m ruff check src tests && python -m mypy --strict src/`
- tests: `python -m pytest tests/unit -m unit -q`; integration needs `REDIS_URL` (use `docker compose up -d redis`)
- coverage gate: `python scripts/check_coverage.py --min-queue 85`

`make` is **not available** — run the Makefile targets by hand.

**Docker Desktop is unstable here**: builds crash the Linux engine mid-RUN
(`rpc error ... EOF` / `open //./pipe/dockerDesktopLinuxEngine: ... cannot find`),
and a crashed build can leave a corrupt base layer that later fails with
`exec /bin/sh: exec format error` (fix: `docker builder prune -f` + re-pull).
`docker compose up -d redis postgres` and short runs work; full image builds
are best left to CI (`docker-build` job in `.github/workflows/ci.yml`).

mypy `--strict` with redis-py 7.x needs `cast(...)` around stream-command
return values (they are typed `Awaitable[Any] | Any`); see the `_xadd` helper
and casts in [[queue-redis-typing]] context within `src/hc/queue/`.
