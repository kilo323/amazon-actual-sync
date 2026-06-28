import logging
import json
import os
from datetime import datetime, timedelta
from typing import List, Optional

from amazonorders.conf import AmazonOrdersConfig
from amazonorders.exception import AmazonOrdersError
from amazonorders.orders import AmazonOrders
from amazonorders.transactions import AmazonTransactions
from amazonorders.session import AmazonSession, cookies_file_lock, dict_from_cookiejar
from amazonorders.contrib.browser.playwright import PlaywrightAcicForm

from .browser import FixedPlaywrightJSAuthForm
from .config import AccountConfig, AppConfig, DATA_DIR
from .database import Database
from . import actual_budget as actual_budget_module

logger = logging.getLogger(__name__)


def _save_session_cookies(session: AmazonSession) -> None:
    """Persist authenticated cookies so the next run can reuse them and skip re-login."""
    cookies = dict_from_cookiejar(session.session.cookies)
    with cookies_file_lock:
        with open(session.config.cookie_jar_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(cookies))

def _time_filters_since(lookback_days: int) -> List[str]:
    """Return time_filter strings covering orders for the past lookback_days."""
    now = datetime.utcnow()
    reference_date = now - timedelta(days=lookback_days)
    days_back = lookback_days
    if days_back <= 30:
        return ["last30"]
    if days_back <= 90:
        return ["months-3"]
    return [f"year-{y}" for y in range(reference_date.year, now.year + 1)]


def _build_session(account: AccountConfig, account_id: int) -> AmazonSession:
    """Create a configured AmazonSession for the given account (does not login)."""
    session_config_path = os.path.join(DATA_DIR, f".session-{account_id}.json")
    ao_config = AmazonOrdersConfig(
        data={
            "output_dir": os.path.join(DATA_DIR, "output"),
            "cookie_jar_path": session_config_path,
        },
    )
    if account.domain:
        ao_config.set_domain(account.domain)
    auth_forms = AmazonSession.default_auth_forms(ao_config)
    insert_pos = len(auth_forms) - 2  # before the AcicAuthBlocker/JSAuthBlocker pair
    auth_forms.insert(insert_pos, PlaywrightAcicForm(ao_config))
    auth_forms.insert(insert_pos + 1, FixedPlaywrightJSAuthForm(ao_config))
    return AmazonSession(
        username=account.username,
        password=account.password,
        otp_secret_key=account.otp_secret_key,
        config=ao_config,
        auth_forms=auth_forms,
    )


def authenticate_account(
    account: AccountConfig,
    db: Database,
    auth_failures: Optional[set] = None,
) -> bool:
    """Login only – no order fetching. Returns True on success, False on failure."""
    account_id = db.get_or_create_account(account.username, account.label)
    session = _build_session(account, account_id)
    try:
        session.login()
    except AmazonOrdersError as exc:
        logger.error("Authentication failed for '%s': %s", account.display_name, exc)
        if auth_failures is not None:
            auth_failures.add(account.username)
        return False
    _save_session_cookies(session)
    db.set_authenticated(account_id)
    if auth_failures is not None:
        auth_failures.discard(account.username)
    logger.info("Authentication successful for '%s'", account.display_name)
    return True


def sync_account(
    account: AccountConfig,
    db: Database,
    config: AppConfig,
    time_filter: Optional[str] = None,
    auth_failures: Optional[set] = None,
) -> None:
    """Sync orders for an account.

    When time_filter is provided a targeted filter sync is performed and
    last_synced_at is NOT updated (it is a backfill, not a regular sync).
    """
    account_id = db.get_or_create_account(account.username, account.label)
    is_filter_sync = time_filter is not None

    if is_filter_sync:
        logger.info(
            "Filter sync for '%s': filter=%s",
            account.display_name,
            time_filter,
        )
    else:
        sync_started_at = datetime.utcnow()
        logger.info(
            "Syncing account '%s'",
            account.display_name,
        )

    session = _build_session(account, account_id)

    try:
        session.login()
    except AmazonOrdersError as exc:
        logger.error("Login failed for '%s': %s", account.display_name, exc)
        if auth_failures is not None:
            auth_failures.add(account.username)
        return

    _save_session_cookies(session)
    db.set_authenticated(account_id)

    amazon_orders_client = AmazonOrders(session)
    if is_filter_sync:
        time_filters = [time_filter]
    else:
        time_filters = _time_filters_since(config.lookback_days)
    logger.debug("Time filters for '%s': %s", account.display_name, time_filters)

    total_new = 0
    total_updated = 0

    for time_filter in time_filters:
        logger.info(
            "Fetching orders (filter=%s) for '%s'",
            time_filter,
            account.display_name,
        )
        try:
            orders = amazon_orders_client.get_order_history(
                time_filter=time_filter,
                full_details=True,
            )
        except AmazonOrdersError as exc:
            logger.error(
                "Failed to fetch orders for '%s' (filter=%s): %s",
                account.display_name,
                time_filter,
                exc,
            )
            continue

        for order in orders:
            try:
                is_new = db.upsert_order(account_id, order, full_details=True)
                if is_new:
                    total_new += 1
                else:
                    total_updated += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to store order %s for '%s': %s",
                    order.order_number,
                    account.display_name,
                    exc,
                )

        logger.info(
            "Fetched %d orders (filter=%s) for '%s'",
            len(orders),
            time_filter,
            account.display_name,
        )

    logger.info(
        "Sync complete for '%s': %d new, %d updated",
        account.display_name,
        total_new,
        total_updated,
    )

    # --- Backfill orders missing full details ---
    backfill_order_numbers = db.get_orders_needing_backfill(account_id)
    if backfill_order_numbers:
        logger.info(
            "Backfilling full details for %d order(s) for '%s'",
            len(backfill_order_numbers),
            account.display_name,
        )
        backfill_ok = backfill_failed = 0
        for order_number in backfill_order_numbers:
            try:
                order = amazon_orders_client.get_order(order_number)
                db.upsert_order(account_id, order, full_details=True)
                backfill_ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to backfill order %s for '%s': %s",
                    order_number,
                    account.display_name,
                    exc,
                )
                backfill_failed += 1
        logger.info(
            "Backfill complete for '%s': %d succeeded, %d failed",
            account.display_name,
            backfill_ok,
            backfill_failed,
        )

    # --- Transactions ---
    # Cover all orders in the DB, not just the recent sync window.
    oldest_order_date = db.get_oldest_order_date(account_id)
    if oldest_order_date:
        transaction_days = max((datetime.utcnow() - oldest_order_date).days + 1, config.lookback_days)
    else:
        transaction_days = config.lookback_days
    try:
        amazon_transactions_client = AmazonTransactions(session)
        transactions = amazon_transactions_client.get_transactions(days=transaction_days)
        tx_new = tx_updated = 0
        for tx in transactions:
            try:
                if db.upsert_transaction(account_id, tx):
                    tx_new += 1
                else:
                    tx_updated += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "Failed to store transaction for '%s': %s",
                    account.display_name,
                    exc,
                )
        logger.info(
            "Transactions synced for '%s': %d new, %d updated",
            account.display_name,
            tx_new,
            tx_updated,
        )
    except AmazonOrdersError as exc:
        logger.error("Failed to fetch transactions for '%s': %s", account.display_name, exc)

    if not is_filter_sync:
        db.update_last_synced(account_id, sync_started_at)

    try:
        session.logout()
    except AmazonOrdersError:
        pass


def sync_actual_budget_only(config: AppConfig, db: Database) -> None:
    """Push pending transactions to Actual Budget for all accounts without touching Amazon."""
    if not config.actual_budget:
        logger.warning("Actual Budget is not configured – skipping")
        return
    # Run bank sync once up-front (uses account_id=1 for error logging) so
    # credit-card transactions are in Actual before we attempt to match them
    # against Amazon orders.
    actual_budget_module.run_bank_sync(
        config.actual_budget, db=db, account_id=1
    )
    logger.info("Starting Actual Budget-only sync for %d account(s)", len(config.accounts))
    for account in config.accounts:
        account_id = db.get_or_create_account(account.username, account.label)
        try:
            actual_budget_module.push_pending_transactions(
                db, account_id, account.display_name, config.actual_budget, account.tags
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Unexpected error pushing Actual Budget for '%s': %s", account.display_name, exc
            )
    logger.info("Actual Budget-only sync complete")


def sync_all_filter(config: AppConfig, db: Database, time_filter: str, auth_failures: Optional[set] = None) -> None:
    logger.info(
        "Starting filter sync for %d account(s): filter=%s",
        len(config.accounts),
        time_filter,
    )
    for account in config.accounts:
        try:
            sync_account(account, db, config, time_filter=time_filter, auth_failures=auth_failures)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Unexpected error filter-syncing account '%s': %s", account.display_name, exc
            )
    logger.info("Filter sync complete")


def sync_all(config: AppConfig, db: Database, auth_failures: Optional[set] = None) -> None:
    """Sync Amazon orders for all accounts, then run bank sync, then push to Actual Budget."""
    logger.info("Starting sync for %d account(s)", len(config.accounts))

    # Phase 1: Sync all Amazon accounts (orders and transactions, but no Actual Budget yet)
    for account in config.accounts:
        try:
            sync_account(account, db, config, auth_failures=auth_failures)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Unexpected error syncing account '%s': %s", account.display_name, exc
            )
    logger.info("All Amazon accounts synced")

    # Phase 2: Run bank sync once (uses account_id=1 for error logging)
    if config.actual_budget:
        actual_budget_module.run_bank_sync(
            config.actual_budget, db=db, account_id=1
        )

        # Phase 3: Push pending transactions to Actual Budget for all accounts
        logger.info("Starting Actual Budget sync for %d account(s)", len(config.accounts))
        for account in config.accounts:
            account_id = db.get_or_create_account(account.username, account.label)
            try:
                actual_budget_module.push_pending_transactions(
                    db, account_id, account.display_name, config.actual_budget, account.tags
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Unexpected error pushing Actual Budget for '%s': %s",
                    account.display_name,
                    exc,
                )
        logger.info("Actual Budget sync complete")

    logger.info("Full sync complete")
