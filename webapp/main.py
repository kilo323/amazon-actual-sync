import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import AppConfig, AccountConfig, ActualBudgetConfig, DATA_DIR
from app.database import Database
from app.sync import sync_all, sync_account, sync_all_filter, sync_actual_budget_only, authenticate_account
from app import actual_budget as actual_budget_module

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FastAPI lifespan – scheduler + shared state
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    def _dir_writable(path: Path) -> tuple[bool, str]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return True, "ok"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    config = AppConfig.load()
    db_dir = Path(config.db_path).parent
    data_dir = Path(DATA_DIR)
    uid = os.getuid() if hasattr(os, "getuid") else -1
    gid = os.getgid() if hasattr(os, "getgid") else -1

    data_dir_ok, data_dir_detail = _dir_writable(data_dir)
    db_dir_ok, db_dir_detail = _dir_writable(db_dir)
    logger.info(
        "Startup filesystem check: uid=%s gid=%s data_dir=%s writable=%s (%s) db_dir=%s writable=%s (%s) db_path=%s",
        uid,
        gid,
        data_dir,
        data_dir_ok,
        data_dir_detail,
        db_dir,
        db_dir_ok,
        db_dir_detail,
        config.db_path,
    )

    db = Database(config.db_path)

    app.state.db = db
    app.state.syncing = False
    app.state.last_run: Optional[datetime] = None
    app.state.auth_failed: set = set()  # usernames that failed login on last attempt

    scheduler = AsyncIOScheduler(timezone="UTC")
    app.state.scheduler = scheduler

    async def _run_full_sync() -> None:
        if app.state.syncing:
            logger.warning("Sync already running – skipping")
            return
        app.state.syncing = True
        try:
            cfg = AppConfig.load()
            current_db = Database(cfg.db_path)
            # Optimistically clear auth failures before attempting
            for a in cfg.accounts:
                app.state.auth_failed.discard(a.username)
            await asyncio.to_thread(sync_all, cfg, current_db, app.state.auth_failed)
            app.state.last_run = datetime.utcnow()
        finally:
            app.state.syncing = False

    def _reschedule(cfg: AppConfig) -> None:
        parts = cfg.sync_schedule.split()
        if len(parts) != 5:
            logger.error("Invalid sync_schedule %r – expected 5-field cron", cfg.sync_schedule)
            return
        minute, hour, day, month, dow = parts
        trigger = CronTrigger(
            minute=minute, hour=hour, day=day, month=month,
            day_of_week=dow, timezone="UTC",
        )
        if scheduler.get_job("sync_orders"):
            scheduler.reschedule_job("sync_orders", trigger=trigger)
        else:
            scheduler.add_job(_run_full_sync, trigger, id="sync_orders")

    app.state.run_full_sync = _run_full_sync
    app.state.reschedule = _reschedule

    _reschedule(config)
    scheduler.start()

    yield

    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# App + static files
# ---------------------------------------------------------------------------

app = FastAPI(title="Amazon Orders Sync", lifespan=lifespan)

_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(str(_STATIC / "index.html"))


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AccountIn(BaseModel):
    username: str
    password: str
    otp_secret_key: Optional[str] = None
    label: Optional[str] = None
    domain: Optional[str] = None
    tags: Optional[List[str]] = None


class AccountUpdate(BaseModel):
    username: str
    password: Optional[str] = None   # blank = keep existing
    otp_secret_key: Optional[str] = None
    clear_otp: bool = False
    label: Optional[str] = None
    domain: Optional[str] = None
    tags: Optional[List[str]] = None


VALID_FILTERS = {"last30", "months-3"}


def _validate_time_filter(value: str) -> str:
    if value in VALID_FILTERS:
        return value
    if value.startswith("year-") and value[5:].isdigit() and len(value[5:]) == 4:
        return value
    raise HTTPException(400, f"Invalid time_filter '{value}'. Must be 'last30', 'months-3', or 'year-YYYY'.")


class SyncFilterIn(BaseModel):
    time_filter: str  # e.g. "last30", "months-3", "year-2024"


class ActualBudgetIn(BaseModel):
    budget_sync_id: str = ""
    account_ids: List[str] = []
    dry_run: bool = True
    server_url: str = "http://localhost:5006"
    server_password: str = ""
    actual_sync_start_date: Optional[str] = None
    amazon_category_name: str = ""


class SettingsIn(BaseModel):
    sync_schedule: str
    lookback_days: int
    log_level: str
    actual_budget: Optional[ActualBudgetIn] = None


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/api/status")
async def get_status():
    config = AppConfig.load()
    db: Database = app.state.db
    scheduler: AsyncIOScheduler = app.state.scheduler

    job = scheduler.get_job("sync_orders")
    next_run = job.next_run_time.isoformat() if (job and job.next_run_time) else None

    accounts = []
    for acct in config.accounts:
        info = db.get_account_info(acct.username)
        # A session is considered active when the account has ever synced
        # successfully or been logged in. The TOTP secret drives login automatically.
        has_session = bool(info and (info.get("authenticated_at") or info.get("last_synced_at")))
        auth_failed = acct.username in app.state.auth_failed
        needs_auth = not has_session or auth_failed
        accounts.append({
            "username": acct.username,
            "label": acct.label,
            "last_synced_at": info["last_synced_at"] if info else None,
            "has_totp": bool(acct.otp_secret_key),
            "authenticated": has_session,
            "auth_failed": auth_failed,
            "needs_auth": needs_auth,
        })

    return {
        "syncing": app.state.syncing,
        "last_run": app.state.last_run.isoformat() if app.state.last_run else None,
        "next_run": next_run,
        "accounts": accounts,
    }


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.get("/api/settings")
async def get_settings():
    config = AppConfig.load()
    ab = None
    if config.actual_budget is not None:
        ab = {
            "budget_sync_id": config.actual_budget.budget_sync_id,
            "account_ids": config.actual_budget.account_ids,
            "dry_run": config.actual_budget.dry_run,
            "server_url": config.actual_budget.server_url,
            "server_password": config.actual_budget.server_password,
            "actual_sync_start_date": config.actual_budget.actual_sync_start_date,
            "amazon_category_name": config.actual_budget.amazon_category_name,
        }
    return {
        "sync_schedule": config.sync_schedule,
        "lookback_days": config.lookback_days,
        "log_level": config.log_level,
        "db_path": config.db_path,
        "actual_budget": ab,
    }


@app.put("/api/settings")
async def update_settings(body: SettingsIn):
    config = AppConfig.load()
    config.sync_schedule = body.sync_schedule
    config.lookback_days = max(1, body.lookback_days)
    config.log_level = body.log_level.upper()
    if body.actual_budget is not None:
        config.actual_budget = ActualBudgetConfig(
            budget_sync_id=body.actual_budget.budget_sync_id,
            account_ids=ActualBudgetConfig._normalize_account_ids(body.actual_budget.account_ids),
            dry_run=body.actual_budget.dry_run,
            server_url=body.actual_budget.server_url,
            server_password=body.actual_budget.server_password,
            actual_sync_start_date=body.actual_budget.actual_sync_start_date or None,
            amazon_category_name=body.actual_budget.amazon_category_name,
        )
    else:
        config.actual_budget = None
    config.save()
    app.state.reschedule(config)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Accounts CRUD
# ---------------------------------------------------------------------------

@app.get("/api/accounts")
async def list_accounts():
    config = AppConfig.load()
    return [
        {
            "index": i,
            "username": a.username,
            "label": a.label,
            "domain": a.domain,
            "has_otp": bool(a.otp_secret_key),
            "tags": a.tags,
        }
        for i, a in enumerate(config.accounts)
    ]


@app.post("/api/accounts", status_code=201)
async def add_account(body: AccountIn):
    config = AppConfig.load()
    if any(a.username == body.username for a in config.accounts):
        raise HTTPException(409, f"Account '{body.username}' already exists")
    config.accounts.append(AccountConfig(
        username=body.username,
        password=body.password,
        otp_secret_key=body.otp_secret_key or None,
        label=body.label or None,
        domain=body.domain or None,
        tags=body.tags or [],
    ))
    config.save()
    return {"index": len(config.accounts) - 1}


@app.put("/api/accounts/{idx}")
async def update_account(idx: int, body: AccountUpdate):
    config = AppConfig.load()
    if idx < 0 or idx >= len(config.accounts):
        raise HTTPException(404, "Account not found")
    existing = config.accounts[idx]
    config.accounts[idx] = AccountConfig(
        username=body.username,
        password=body.password if body.password else existing.password,
        otp_secret_key=(
            None if body.clear_otp
            else (body.otp_secret_key if body.otp_secret_key else existing.otp_secret_key)
        ),
        label=body.label or None,
        domain=body.domain or None,
        tags=body.tags if body.tags is not None else existing.tags,
    )
    config.save()
    return {"ok": True}


@app.delete("/api/accounts/{idx}")
async def delete_account(idx: int):
    config = AppConfig.load()
    if idx < 0 or idx >= len(config.accounts):
        raise HTTPException(404, "Account not found")
    removed = config.accounts.pop(idx)
    config.save()
    # Remove persisted session file if present
    db: Database = app.state.db
    info = db.get_account_info(removed.username)
    if info:
        try:
            os.remove(os.path.join(DATA_DIR, f".session-{info['id']}.json"))
        except FileNotFoundError:
            pass
    return {"ok": True}


# ---------------------------------------------------------------------------
# TOTP code endpoint
# ---------------------------------------------------------------------------

@app.get("/api/accounts/{idx}/totp")
async def get_totp_code(idx: int):
    """Return the current TOTP code and remaining seconds for the active window."""
    import time
    try:
        import pyotp
    except ImportError:
        raise HTTPException(500, "pyotp is not installed")

    config = AppConfig.load()
    if idx < 0 or idx >= len(config.accounts):
        raise HTTPException(404, "Account not found")
    account = config.accounts[idx]
    if not account.otp_secret_key:
        raise HTTPException(400, "No TOTP secret configured for this account")

    totp = pyotp.TOTP(account.otp_secret_key.replace(" ", "").strip())
    code = totp.now()
    remaining = 30 - (int(time.time()) % 30)
    return {"code": code, "remaining": remaining}


# ---------------------------------------------------------------------------
# Authenticate (login only, no order fetch)
# ---------------------------------------------------------------------------

@app.post("/api/accounts/{idx}/authenticate")
async def authenticate(idx: int):
    if app.state.syncing:
        raise HTTPException(409, "A sync is already in progress")
    config = AppConfig.load()
    if idx < 0 or idx >= len(config.accounts):
        raise HTTPException(404, "Account not found")
    account = config.accounts[idx]
    db: Database = app.state.db

    async def _run() -> None:
        app.state.syncing = True
        try:
            await asyncio.to_thread(
                authenticate_account, account, db, app.state.auth_failed
            )
        finally:
            app.state.syncing = False

    asyncio.create_task(_run())
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sync triggers
# ---------------------------------------------------------------------------

@app.post("/api/sync")
async def trigger_full_sync():
    if app.state.syncing:
        raise HTTPException(409, "A sync is already in progress")
    asyncio.create_task(app.state.run_full_sync())
    return {"ok": True}


@app.post("/api/sync/filter")
async def trigger_filter_sync(body: SyncFilterIn):
    if app.state.syncing:
        raise HTTPException(409, "A sync is already in progress")
    tf = _validate_time_filter(body.time_filter)

    async def _run() -> None:
        app.state.syncing = True
        try:
            cfg = AppConfig.load()
            current_db = Database(cfg.db_path)
            for a in cfg.accounts:
                app.state.auth_failed.discard(a.username)
            await asyncio.to_thread(sync_all_filter, cfg, current_db, tf, app.state.auth_failed)
            app.state.last_run = datetime.utcnow()
        finally:
            app.state.syncing = False

    asyncio.create_task(_run())
    return {"ok": True}


@app.post("/api/accounts/{idx}/sync")
async def trigger_account_sync(idx: int):
    if app.state.syncing:
        raise HTTPException(409, "A sync is already in progress")
    config = AppConfig.load()
    if idx < 0 or idx >= len(config.accounts):
        raise HTTPException(404, "Account not found")
    account = config.accounts[idx]
    db: Database = app.state.db

    async def _run() -> None:
        app.state.syncing = True
        app.state.auth_failed.discard(account.username)
        try:
            await asyncio.to_thread(sync_account, account, db, config, None, app.state.auth_failed)
            app.state.last_run = datetime.utcnow()
        finally:
            app.state.syncing = False

    asyncio.create_task(_run())
    return {"ok": True}


@app.post("/api/sync/actual-budget")
async def trigger_actual_budget_sync():
    if app.state.syncing:
        raise HTTPException(409, "A sync is already in progress")

    async def _run() -> None:
        app.state.syncing = True
        try:
            cfg = AppConfig.load()
            current_db = Database(cfg.db_path)
            if not cfg.actual_budget:
                raise ValueError("Actual Budget is not configured")
            await asyncio.to_thread(sync_actual_budget_only, cfg, current_db)
            app.state.last_run = datetime.utcnow()
        finally:
            app.state.syncing = False

    asyncio.create_task(_run())
    return {"ok": True}


@app.post("/api/accounts/{idx}/sync/filter")
async def trigger_account_filter_sync(idx: int, body: SyncFilterIn):
    if app.state.syncing:
        raise HTTPException(409, "A sync is already in progress")
    config = AppConfig.load()
    if idx < 0 or idx >= len(config.accounts):
        raise HTTPException(404, "Account not found")
    tf = _validate_time_filter(body.time_filter)

    account = config.accounts[idx]
    db: Database = app.state.db

    async def _run() -> None:
        app.state.syncing = True
        app.state.auth_failed.discard(account.username)
        try:
            await asyncio.to_thread(sync_account, account, db, config, tf, app.state.auth_failed)
            app.state.last_run = datetime.utcnow()
        finally:
            app.state.syncing = False

    asyncio.create_task(_run())
    return {"ok": True}


# ---------------------------------------------------------------------------
# Orders summary
# ---------------------------------------------------------------------------

def _parse_date(value: str, field_name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"Invalid {field_name} '{value}'. Expected YYYY-MM-DD.") from exc


@app.get("/api/orders/summary")
async def get_orders_summary(start_date: Optional[str] = None, end_date: Optional[str] = None):
    end_dt = _parse_date(end_date, "end_date") if end_date else datetime.utcnow().date()
    # Inclusive 30-day window by default.
    start_dt = _parse_date(start_date, "start_date") if start_date else (end_dt - timedelta(days=29))

    if start_dt > end_dt:
        raise HTTPException(400, "start_date cannot be after end_date")

    db: Database = app.state.db
    orders = db.get_order_summaries(start_dt.isoformat(), end_dt.isoformat())

    total_amount = sum((o.get("grand_total") or 0.0) for o in orders)
    total_item_units = sum((o.get("item_count") or 0) for o in orders)

    return {
        "start_date": start_dt.isoformat(),
        "end_date": end_dt.isoformat(),
        "total_orders": len(orders),
        "total_item_units": total_item_units,
        "total_amount": round(total_amount, 2),
        "orders": orders,
    }


# ---------------------------------------------------------------------------
# Amazon sync errors
# ---------------------------------------------------------------------------

@app.get("/api/actual-budget/accounts")
async def get_actual_budget_accounts():
    """Return the list of accounts from the configured Actual Budget instance."""
    config = AppConfig.load()
    if not config.actual_budget or not config.actual_budget.budget_sync_id:
        return {"accounts": []}
    result = actual_budget_module._call_actual_api(
        {"action": "getAccounts", "budgetSyncId": config.actual_budget.budget_sync_id},
        config.actual_budget,
    )
    if "error" in result:
        logger.warning("Failed to load Actual Budget accounts: %s", result["error"])
        return {"accounts": []}
    accounts = sorted(result.get("data", []), key=lambda a: (a.get("name") or "").lower())
    return {
        "accounts": [
            {"id": a["id"], "name": a.get("name", ""), "type": a.get("type", "")}
            for a in accounts
        ]
    }


@app.get("/api/actual-budget/categories")
async def get_actual_budget_categories():
    """Return the list of categories from the configured Actual Budget instance."""
    config = AppConfig.load()
    if not config.actual_budget:
        return {"categories": []}
    result = actual_budget_module._call_actual_api(
        {"action": "getCategories", "budgetSyncId": config.actual_budget.budget_sync_id},
        config.actual_budget,
    )
    if "error" in result:
        logger.warning("Failed to load Actual Budget categories: %s", result["error"])
        return {"categories": []}
    categories = sorted(
        (c for c in result.get("data", []) if not c.get("is_income") and not c.get("hidden")),
        key=lambda c: (c.get("group_name") or "", c.get("name") or ""),
    )
    return {"categories": [{"id": c["id"], "name": c.get("name", ""), "group": c.get("group_name", "")} for c in categories]}


@app.get("/api/amazon-sync-errors")
async def list_amazon_sync_errors():
    """Return recent Amazon sync errors for all accounts."""
    db: Database = app.state.db
    config = AppConfig.load()
    errors = []
    for acct in config.accounts:
        info = db.get_account_info(acct.username)
        if not info:
            continue
        acct_errors = db.get_amazon_sync_errors(info["id"], limit=50)
        for err in acct_errors:
            err["account_label"] = acct.label or acct.username
            errors.append(err)
    # Sort by created_at descending across all accounts
    errors.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    return {"errors": errors[:100]}


@app.get("/api/amazon-sync-errors/{error_id}")
async def get_amazon_sync_error_detail(error_id: int):
    """Return a single Amazon sync error by id."""
    db: Database = app.state.db
    err = db.get_amazon_sync_error(error_id)
    if not err:
        raise HTTPException(404, "Error not found")
    # Attach account label for display
    config = AppConfig.load()
    for acct in config.accounts:
        info = db.get_account_info(acct.username)
        if info and info["id"] == err.get("account_id"):
            err["account_label"] = acct.label or acct.username
            break
    return err
