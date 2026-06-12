# Amazon Orders Sync

Sync Amazon order and transaction data into a local SQLite database, then optionally match and split those transactions into Actual Budget.

This project includes:
- A FastAPI web app for setup, account management, and manual sync actions.
- A scheduler-based sync engine for ongoing automated syncs.
- Docker Compose services for both this app and Actual Budget server.
- Secret scanning via pre-commit (local) and GitHub Actions (CI).

## Features

- Sync Amazon orders (with full order details).
- Sync Amazon transactions.
- Store data locally in SQLite under the data directory.
- Optional Actual Budget push:
   - Runs Actual bank sync once per sync cycle for the configured Actual account.
  - Finds matching Actual transactions.
  - Creates split transactions with item-level detail.
- Web UI for:
  - App settings (schedule, log level, lookback).
  - Account add/update/delete.
  - Authenticate account sessions.
  - Trigger manual syncs.

## Project Layout

- app/: sync engine, config, database, Amazon and Actual matching logic.
- webapp/: FastAPI app and frontend UI.
- data/: runtime config, sqlite DB, session cookies, and Actual server data.
- actual-budget-api.js: Node wrapper around @actual-app/api.
- docker-compose.yml: app + Actual Budget services.
- Dockerfile: container build for the app.

## Requirements

For Docker usage:
- Docker Desktop (or Docker Engine + Compose plugin).

For local (non-Docker) usage:
- Python 3.12+
- Node.js (for the Actual Budget API bridge)
- Chromium dependencies (if running browser automation locally)

## Quick Start (Docker Recommended)

1. Copy environment template:

   cp .env.example .env

2. Start services:

   docker compose up -d --build

3. Open the app web UI:

   http://localhost:8080

4. Open Actual Budget server UI (optional, if enabled in compose):

   http://localhost:5006

5. Configure accounts and settings in the web UI.

Notes:
- The app persists data in ./data.
- The sync schedule is interpreted in UTC.

## Configuration

Runtime config is stored in data/config.json and managed by the web UI.
A template is provided at data/config.example.json.

Key settings:
- sync_schedule: cron expression (5-part, UTC), default: 0 2 * * *
- lookback_days: order lookback window for sync
- log_level: DEBUG, INFO, WARNING, ERROR
- accounts[]: Amazon credentials and optional TOTP secret
- actual_budget: optional section for Actual Budget integration

Environment variables commonly used:
- DATA_DIR (default: /data)
- LOG_LEVEL
- SYNC_WEBAPP_PORT (compose host mapping to container 8080)
- ACTUAL_WEBAPP_PORT (compose host mapping to Actual server 5006)
- ACTUAL_SERVER_URL
- ACTUAL_SERVER_PASSWORD

## Running Without Docker

1. Create and activate a virtual environment.
2. Install Python dependencies:

   pip install -r requirements.txt

3. Install Node dependency:

   npm install

4. Set DATA_DIR (optional) and start web app:

   python -m webapp

The web app defaults to port 8080.

Scheduler-only mode is also available:

python -m app.main

## Actual Budget Integration

When actual_budget is configured, the sync process can push pending transactions into Actual Budget.

At a high level:
- Sync all configured Amazon accounts first (orders + Amazon transaction history).
- Run bank sync once for the configured Actual account (actual_budget.account_id).
- Pull existing transactions from the configured Actual account.
- Find matching local Amazon transactions by signed amount and date window.
- If multiple candidates exist, select the closest candidate deterministically.
- Build split transactions by item and push updates.

Execution order for a full sync:
- Phase 1: Amazon sync for all accounts.
- Phase 2: Single Actual bank sync.
- Phase 3: Actual matching and split updates for all Amazon accounts.

Bank sync errors:
- Bank sync progress and failures are logged.
- If bank sync fails, the error is also written to the amazon_sync_errors table so it is visible in the web UI error view.

Recommended rollout:
- Start with dry_run = true in settings.
- Review logs and matching behavior.
- Switch dry_run to false after validation.

## Security and Privacy

This repo is configured to avoid committing personal data:
- Local secrets and runtime data are ignored in .gitignore.
- data/config.json and local DB/session artifacts are ignored.
- .env is ignored; .env.example is tracked as a template only.

Secret scanning is enabled:
- Local pre-commit hook: gitleaks via .pre-commit-config.yaml
- CI scan workflow: .github/workflows/secret-scan.yml

To install/refresh local hooks:

pre-commit install

To run scanner manually:

pre-commit run gitleaks --all-files

## Container Publishing (GitHub Actions)

This repo publishes a Docker image to GitHub Container Registry (GHCR) using the workflow at .github/workflows/docker-publish.yml.

Trigger behavior:
- Push/merge to main: builds and publishes image.
- Pull requests to main: builds only (no push).
- Semver tag push (v*.*.*): publishes versioned tags.

Published image name:
- ghcr.io/kilo323/amazon-actual-sync

Tags:
- latest on main branch pushes.
- semver tags on version tag pushes (for example v1.2.3).

Pull examples:

docker pull ghcr.io/kilo323/amazon-actual-sync:latest
docker pull ghcr.io/kilo323/amazon-actual-sync:v1.2.3

Notes:
- Packages are pushed with GITHUB_TOKEN from Actions.
- The workflow signs published images with cosign on non-PR events.

## Troubleshooting

- Login/auth failures:
  - Re-authenticate account from the web UI.
  - Verify username/password and TOTP secret.
- Actual Budget push issues:
  - Confirm server URL/password and account IDs.
  - Use dry_run mode to validate matching first.
- Scheduler not running at expected time:
  - Confirm cron expression is valid and in UTC.

## Development Notes

- The sync engine and web app both use app/config.py for shared config handling.
- Persistent files are stored under DATA_DIR.
- test_module/ contains raw/testing artifacts and is intentionally ignored.

## License

No license file is currently included. Add a LICENSE file before publishing if needed.
