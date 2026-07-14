# AGENTS.md

## Cursor Cloud specific instructions

### What this is
"Agentic Trading Desk" — a single product consisting of three stdlib-only Python 3 CLI
scripts under `scripts/` (`indicators.py`, `macro_pillar.py`, `score.py`). There are no
long-running services, no ports, no database, and no build step. See `README.md` and
`SKILL.md` for the product overview and the intended agent workflow.

### Environment
- Requires only Python 3 (works on 3.12). No third-party packages, no `requirements.txt`/
  `pyproject.toml`, and nothing to install — the scripts import from the standard library only.
  The update script is therefore effectively a no-op (a Python version check).

### Running the scripts (non-obvious caveats)
- `score.py` does `import indicators`, so run it from inside `scripts/` (or put `scripts/`
  on `PYTHONPATH`). Running `python3 scripts/score.py` from the repo root also works because
  Python adds the script's own directory to `sys.path`.
- Every script has a built-in self-test: run it with no file argument to generate synthetic
  data and print output (useful smoke test), e.g. `python3 scripts/score.py`.
- Self-test banners are printed to stderr; the actual result goes to stdout.
- Logical data flow: `macro_pillar.py` produces `pillar_score` → inject it as `macro_score`
  into the ticker JSON consumed by `score.py`; `score.py` calls `indicators.py` internally.
- All three accept `--json` for machine-readable output (except `indicators.py`, which is
  always JSON). Input JSON formats are documented in `README.md`.

### Lint / test / build
- No lint or test framework is configured. Use `python3 -m py_compile scripts/*.py` as a
  syntax check, and the built-in self-tests (`python3 scripts/<name>.py`) as functional checks.
