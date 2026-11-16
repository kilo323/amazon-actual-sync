# AGENTS.md

## What this repo is

`amazon-actual-sync` — syncs Amazon order and transaction data into a local
SQLite database, then optionally matches and splits those transactions into
[Actual Budget](https://actualbudget.dev). Ships a FastAPI web app for setup /
account management / manual syncs, plus a scheduler-based sync engine.

## Language & versions

- Python 3.12+ (core logic), **FastAPI** web app
- Node.js 22 (LTS, per Dockerfile) — drives the Actual Budget bridge
- Core dependency: `amazon-orders[browser]==5.2.*` (the Amazon scrape engine)
- Browser automation via Playwright + Chromium (`--no-sandbox` inside the container)

## Layout

- `app/` — the sync engine:
  - `sync.py` — Amazon login/orders/transactions sync; `app/main.py` runs this on a cron scheduler (scheduler-only mode).
  - `actual_budget.py` — Actual Bank sync, transaction matching, item-level split creation. `_assign_shipments_to_charges` / `_reconcile_sync_markers` are the pure, tested pieces.
  - `database.py` — SQLite schema + persistence.
  - `config.py` — shared `AppConfig` / `AccountConfig` / `ActualBudgetConfig` (pydantic-ish dataclasses).
  - `browser.py` — Playwright JS-challenge login fixes.
- `webapp/` — FastAPI app (`main.py`) + static UI; `__main__.py` launches uvicorn.
- `data/` — runtime config, sqlite DB, session cookies, Actual server data. Only `data/config.example.json` is tracked.
- `actual-budget-api.js` — Node wrapper around `@actual-app/api`. Reads a JSON
  request from stdin, writes JSON to stdout (all library logs go to **stderr**
  so they don't corrupt the JSON).
- `tests/` — stdlib `unittest`.

## Commands

Run (all verified to exist; see README for full rationale):

```bash
# Docker (recommended)
cp .env.example .env
docker compose up -d --build        # web UI at http://localhost:8080
python -m webapp                    # no-Docker web app (port from $WEBAPP_PORT, default 8080)
python -m app.main                  # scheduler-only mode

# Tests — stdlib unittest, no internet/heavy deps needed:
python -m unittest discover -s tests -v
```

`python -m unittest discover -s tests` passes (5 tests). It imports only
`app.*` modules, so it runs without installing the heavy `amazon-orders` pip
package.

## Lint / formatting / scanning

- **No** formatter or linter (no ruff/black/flake8/mypy/isort config anywhere).
- Secret scanning only: **gitleaks**.
  - Pre-commit: `.pre-commit-config.yaml` → `pre-commit install` / `pre-commit run gitleaks --all-files`.
  - CI: `.github/workflows/secret-scan.yml` (runs `gitleaks` on every PR and on pushes to `main`).
- CI image signing: `.github/workflows/docker-publish.yml` signs the GHCR
  image with cosign on non-PR events.

## Runtime config

Runtime config lives in `data/config.json` (gitignored, managed by the web UI);
template: `data/config.example.json` (tracked). Key settings:

- `sync_schedule` — 5-field cron in **UTC** (e.g. `0 2 * * *`)
- `lookback_days`, `log_level`, `accounts[]`, optional `actual_budget{}`
- Env vars: `DATA_DIR`, `LOG_LEVEL`, `WEBAPP_PORT`, `SYNC_WEBAPP_PORT`,
  `ACTUAL_WEBAPP_PORT`, `ACTUAL_SERVER_URL`, `ACTUAL_SERVER_PASSWORD`,
  `AMAZON_ACCOUNTS` (headless bootstrap, no web UI).

## Sync execution order

1. Sync all Amazon accounts (orders + transaction history).
2. Single Actual Bank sync for the configured account(s).
3. Match transactions and push split updates.

`actual_budget.dry_run` defaults to `true` — validate matching before setting
it to `false`.

## Gotchas

- Secrets / runtime data are gitignored: `.env`, `data/`, `*.session-*.json`,
  `.pem/.key`, etc. Only `data/config.example.json` is committed.
- Actual supports multiple accounts via `account_ids[]`; a legacy single
  `account_id` string is auto-migrated to a list.
- Multiple charges per line item are reconciled against **distinct shipments**;
  rewards/gift-card tender rows are not treated as card charges.
- Container runs as a non-root user (uid 99 / gid 100 by default).
