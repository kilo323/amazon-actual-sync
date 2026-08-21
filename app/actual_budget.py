"""
Match Amazon orders to existing Actual Budget credit-card transactions and
convert them into split transactions with item-level detail.

Uses the @actual-app/api Node.js library via a local subprocess.

Behaviour:
1. For each uncategorized Actual Budget transaction with an Amazon payee, look
up the matching Amazon order in our local DB by amount and date.
2. If a match is found, replace the existing transaction with a split
(is_parent=True) with subtransactions for each item + allocated tax/shipping.
3. If no match, or the totals don't reconcile, log an error and skip.
4. Mark successfully processed rows in the local DB so they are not re-processed.
"""
import json
import logging
import os
import subprocess
from datetime import datetime, timedelta
from typing import List, Optional

from .config import ActualBudgetConfig
from .database import Database

logger = logging.getLogger(__name__)

# Date window (days) when searching for a matching credit-card transaction.
_DATE_WINDOW_DAYS = 3

# Path to the Node.js script that wraps @actual-app/api
_NODE_SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "..", "actual-budget-api.js")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_supported_amazon_payee(payee_name: Optional[str]) -> bool:
    """Return True when an Actual Budget payee could correspond to an Amazon order.

    We intentionally ignore Amazon Pharmacy charges because they are credit-card
    transactions for pharmacy purchases, not Amazon order items with a matching
    local order record.
    """
    normalized = (payee_name or "").lower().strip()
    if not normalized or "amazon" not in normalized:
        return False
    if "amazon pharmacy" in normalized:
        return False
    return True


def _call_actual_api(payload: dict, config: Optional["ActualBudgetConfig"] = None) -> dict:
    """Invoke the Node.js Actual Budget API script and return parsed JSON."""
    script_path = os.path.abspath(_NODE_SCRIPT_PATH)
    env = os.environ.copy()
    if config is not None:
        env["ACTUAL_SERVER_URL"] = config.server_url
        env["ACTUAL_SERVER_PASSWORD"] = config.server_password
    try:
        result = subprocess.run(
            ["node", script_path],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired:
        logger.error("Actual Budget API call timed out")
        return {"error": "timeout"}
    except FileNotFoundError:
        logger.error("Node.js is not installed or not on PATH")
        return {"error": "node not found"}

    if result.returncode != 0:
        # The Node.js script writes errors as JSON to stdout (console.log) and
        # exits with code 1; stderr may be empty.
        error_msg = result.stderr.strip()
        if not error_msg:
            try:
                parsed = json.loads(result.stdout)
                error_msg = parsed.get("error") or result.stdout.strip() or "unknown script error"
            except (json.JSONDecodeError, AttributeError):
                error_msg = result.stdout.strip() or "unknown script error"
        logger.error("Actual Budget API script failed: %s", error_msg)
        return {"error": error_msg}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse Actual Budget API response: %s", exc)
        return {"error": f"invalid JSON: {exc}"}


def run_bank_sync(
    config: "ActualBudgetConfig",
    db: Optional["Database"] = None,
    account_id: Optional[int] = None,
) -> None:
    """Trigger a bank sync in Actual Budget for the configured Amazon account.

    Logs progress via the module logger. If *db* and *account_id* are provided,
    any error is also written to the amazon_sync_errors table so it surfaces in
    the webapp error view.
    """
    logger.info(
        "Starting bank sync for Actual Budget account '%s'", config.account_id
    )
    payload = {
        "action": "runBankSync",
        "budgetSyncId": config.budget_sync_id,
        "accountId": config.account_id,
    }
    result = _call_actual_api(payload, config)
    if "error" in result:
        error_msg = result["error"]
        logger.error(
            "Bank sync failed for Actual Budget account '%s': %s",
            config.account_id,
            error_msg,
        )
        if db is not None and account_id is not None:
            db.log_amazon_sync_error(
                account_id=account_id,
                order_number="bank_sync",
                error_type="bank_sync_error",
                message=f"Bank sync failed for Actual Budget account '{config.account_id}': {error_msg}",
            )
    else:
        logger.info(
            "Bank sync completed successfully for Actual Budget account '%s'",
            config.account_id,
        )


def _get_transactions_for_account(
    config: ActualBudgetConfig,
    since_date: str,
    until_date: str,
) -> list:
    """Fetch all transactions from the configured Actual Budget account."""
    payload = {
        "action": "getTransactions",
        "budgetSyncId": config.budget_sync_id,
        "accountId": config.account_id,
        "sinceDate": since_date,
        "untilDate": until_date,
    }
    result = _call_actual_api(payload, config)
    if "error" in result:
        logger.warning("Failed to fetch transactions from Actual Budget: %s", result["error"])
        return []
    return result.get("data", [])


def _ensure_tag(config: ActualBudgetConfig, tag_name: str) -> None:
    """Create the tag in Actual Budget if it doesn't already exist."""
    result = _call_actual_api(
        {"action": "getTags", "budgetSyncId": config.budget_sync_id},
        config,
    )
    if "error" in result:
        logger.warning("Failed to fetch tags from Actual Budget: %s", result["error"])
        return
    existing = {t.get("tag", "").lower() for t in result.get("data", [])}
    if tag_name.lower() in existing:
        return
    create_result = _call_actual_api(
        {"action": "createTag", "budgetSyncId": config.budget_sync_id, "tag": tag_name},
        config,
    )
    if "error" in create_result:
        logger.warning("Failed to create tag '%s': %s", tag_name, create_result["error"])
    else:
        logger.debug("Created tag '%s' in Actual Budget", tag_name)


def _get_category_id_by_name(config: ActualBudgetConfig, category_name: str) -> Optional[str]:
    """Resolve a category name to its Actual Budget ID (case-insensitive). Returns None if not found."""
    result = _call_actual_api(
        {"action": "getCategories", "budgetSyncId": config.budget_sync_id},
        config,
    )
    if "error" in result:
        logger.warning("Failed to fetch categories from Actual Budget: %s", result["error"])
        return None
    target = category_name.lower()
    for cat in result.get("data", []):
        if (cat.get("name") or "").lower() == target:
            return cat.get("id")
    logger.warning("Category '%s' not found in Actual Budget", category_name)
    return None


def _build_subtransactions(items: list, parent_cents: int) -> list:
    """
    Build one Actual Budget subtransaction per order item.

    The parent_cents total is distributed across items proportionally by
    price * quantity. Any difference (tax, shipping, discounts, etc.) is
    absorbed proportionally so the subtransaction sum always equals parent_cents
    exactly. The last item absorbs any rounding remainder.
    """
    if not items:
        return []

    bases = [((item.get("price") or 0.0) * (item.get("quantity") or 1)) for item in items]
    total_base = sum(bases)

    subs = []
    allocated = 0
    for idx, (item, base) in enumerate(zip(items, bases)):
        title = (item.get("title") or "Unknown item")[:100]
        if idx == len(items) - 1:
            amount = parent_cents - allocated
        elif total_base:
            amount = round(parent_cents * base / total_base)
            allocated += amount
        else:
            # All items have zero price — split evenly
            amount = parent_cents // len(items)
            allocated += amount
        subs.append({"notes": title, "amount": amount})

    return subs


def _identify_items_for_charge(
    db: "Database",
    order_id: int,
    order_number: str,
    account_id: int,
    charge_cents: int,
    order: dict,
) -> list:
    """
    Determine which items a specific credit card charge covers.

    Strategy:
    1. If only one non-gift-card charge exists for this order, the charge covers
       all items (partial payment after gift card is subtracted).
    2. If multiple charges exist, try to match shipments to this charge by
       computing each shipment's expected share (items_subtotal / order_subtotal
       * order_grand_total) and comparing to the charge amount.
    3. Fallback: return all items (proportional split still works correctly).
    """
    all_items = db.get_all_items_for_order(order_id)
    if not all_items:
        return []

    # If only one non-gift-card charge for this order, it covers everything
    charge_count = db.get_charge_count_for_order(order_number, account_id)
    if charge_count <= 1:
        return all_items

    # Multiple charges for same order — try shipment-based attribution
    shipments = db.get_shipments_with_items_for_order(order_id)
    if not shipments:
        return all_items

    order_subtotal = order.get("subtotal") or 0.0
    order_total = order.get("order_total") or 0.0
    if not order_subtotal or not order_total:
        return all_items

    charge_abs = abs(charge_cents)
    # Compute each shipment's expected charge (proportional share of order total)
    best_match = None
    best_diff = float("inf")
    # Also try combinations of shipments
    for shipment in shipments:
        ship_subtotal = shipment["items_subtotal"]
        if not ship_subtotal:
            continue
        expected_cents = round(ship_subtotal / order_subtotal * order_total * 100)
        diff = abs(charge_abs - expected_cents)
        if diff < best_diff:
            best_diff = diff
            best_match = shipment["items"]

    # Accept if within 5% tolerance
    if best_match and best_diff <= max(charge_abs * 0.05, 50):
        logger.debug(
            "Matched charge $%.2f to shipment items (diff=%d cents) for order %s",
            charge_abs / 100, best_diff, order_number,
        )
        return best_match

    # Try combining shipments for a better match
    from itertools import combinations
    for r in range(2, len(shipments) + 1):
        for combo in combinations(shipments, r):
            combo_subtotal = sum(s["items_subtotal"] for s in combo)
            if not combo_subtotal:
                continue
            expected_cents = round(combo_subtotal / order_subtotal * order_total * 100)
            diff = abs(charge_abs - expected_cents)
            if diff < best_diff:
                best_diff = diff
                best_match = []
                for s in combo:
                    best_match.extend(s["items"])

    if best_match and best_diff <= max(charge_abs * 0.05, 50):
        logger.debug(
            "Matched charge $%.2f to combined shipments (diff=%d cents) for order %s",
            charge_abs / 100, best_diff, order_number,
        )
        return best_match

    # Fallback: proportional split across all items still gives correct dollar amounts
    logger.debug(
        "Could not match charge $%.2f to specific shipment(s) for order %s, using all items",
        charge_abs / 100, order_number,
    )
    return all_items


# Tolerance (fraction of refund amount) for auto-matching a refund to items.
_REFUND_MATCH_TOLERANCE = 0.05


def _auto_match_refund_items(
    all_items: list,
    refund_cents: int,
    order: dict,
) -> Optional[list]:
    """
    Identify which item(s) were returned based on the refund amount.

    Strategy: each item's expected refund share equals its proportional slice of
    the order grand total (price * qty / subtotal * grand_total).  First tries
    single-item matches, then combinations of items.  Returns None if ambiguous
    or no match found (caller should fall back to all items).
    """
    order_subtotal = order.get("subtotal") or 0.0
    order_grand_total = abs(order.get("order_total") or 0.0)
    if not order_subtotal or not order_grand_total:
        return None

    refund_abs = abs(refund_cents)
    tolerance_cents = max(round(refund_abs * _REFUND_MATCH_TOLERANCE), 10)

    # Compute expected refund share for each item
    item_expected = []
    for item in all_items:
        price = item.get("price") or 0.0
        qty = item.get("quantity") or 1
        item_base = price * qty
        expected_cents = round(item_base / order_subtotal * order_grand_total * 100)
        item_expected.append((item, expected_cents))

    # Try single-item match first
    single_candidates = []
    for item, expected in item_expected:
        diff = abs(refund_abs - expected)
        if diff <= tolerance_cents:
            single_candidates.append((diff, [item]))

    if single_candidates:
        single_candidates.sort(key=lambda x: x[0])
        best_diff, best_items = single_candidates[0]
        logger.debug(
            "Auto-matched refund of $%.2f to single item (diff=%d cents): %s",
            refund_abs / 100, best_diff,
            best_items[0].get("title", "?")[:60],
        )
        return best_items

    # Try multi-item combinations (for cases where multiple items were returned together)
    from itertools import combinations
    combo_candidates = []
    for r in range(2, min(len(all_items), 5) + 1):  # Cap at 5 to avoid explosion
        for combo in combinations(item_expected, r):
            combo_expected = sum(exp for _, exp in combo)
            diff = abs(refund_abs - combo_expected)
            if diff <= tolerance_cents:
                combo_items = [item for item, _ in combo]
                combo_candidates.append((diff, combo_items))

    if combo_candidates:
        combo_candidates.sort(key=lambda x: x[0])
        best_diff, best_items = combo_candidates[0]
        titles = ", ".join(i.get("title", "?")[:30] for i in best_items)
        logger.debug(
            "Auto-matched refund of $%.2f to %d items (diff=%d cents): %s",
            refund_abs / 100, len(best_items), best_diff, titles,
        )
        return best_items

    return None



def _log_split(label: str, tx: dict, subs: list, actual_tx: Optional[dict] = None) -> None:
    kind = "REFUND" if tx["is_refund"] else "CHARGE"
    amount_dollars = abs(tx["tx_total"])
    sign = "+" if tx["is_refund"] else "-"
    actual_info = f" (Actual tx {actual_tx['id']})" if actual_tx else ""
    logger.info(
        "[DRY RUN] %s  %s  %s  %s%s%s",
        kind,
        tx["completed_date"],
        label,
        sign,
        f"${amount_dollars:.2f}",
        actual_info,
    )
    for s in subs:
        sub_dollars = abs(s["amount"] / 100)
        sub_sign = "+" if s["amount"] > 0 else "-"
        logger.info("  [DRY RUN]   %-55s %s$%.2f", s["notes"], sub_sign, sub_dollars)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def push_pending_transactions(
    db: Database,
    account_id: int,
    account_label: str,
    config: ActualBudgetConfig,
    account_tags: Optional[List[str]] = None,
) -> None:
    """
    Start from uncategorized Actual Budget transactions, find the matching
    Amazon order in our local DB, and convert the transaction into a split
    with item-level detail.

    Splits are one per order item. Each item's amount = purchase price +
    proportional share of shipping, tax, rebates, etc. (distributed so the
    sum exactly equals the credit card charge).

    For refunds, identifies which item(s) were returned and creates a split
    with only those items and their proportional refund shares.

    On success marks the local transaction row as synced and stores the Actual
    transaction id.
    """
    # Clear previous errors so the table only reflects the current run.
    db.truncate_amazon_sync_errors(account_id)

    # Determine date range for fetching existing transactions.
    if config.actual_sync_start_date:
        since = config.actual_sync_start_date
    else:
        since = (datetime.utcnow() - timedelta(days=90)).strftime("%Y-%m-%d")
    until = datetime.utcnow().strftime("%Y-%m-%d")

    existing_transactions = _get_transactions_for_account(config, since, until)
    logger.debug("Fetched %d existing transactions from Actual Budget", len(existing_transactions))

    # Resolve category filter based on config:
    # - non-empty amazon_category_name: match only transactions with that category
    # - empty string: match only uncategorized transactions
    amazon_category_id: Optional[str] = None
    if config.amazon_category_name:
        amazon_category_id = _get_category_id_by_name(config, config.amazon_category_name)

    # Filter candidates: include non-split Amazon-payee transactions. Skip
    # already-synced (#amazon_sync) but allow #sync_not_found through for re-processing.
    candidates = []
    for etx in existing_transactions:
        if etx.get("is_parent"):
            continue
        notes = (etx.get("notes") or "").lower()
        if "amazon_sync" in notes:
            continue
        # Category gate
        tx_category = etx.get("category") or None
        if amazon_category_id is not None:
            # Configured category: only include transactions in that category
            if tx_category != amazon_category_id:
                continue
        else:
            # No configured category: only include uncategorized transactions
            if tx_category is not None:
                continue
        payee_name = etx.get("payee_name") or etx.get("imported_payee") or ""
        if not _is_supported_amazon_payee(payee_name):
            continue
        candidates.append(etx)

    if not candidates:
        logger.debug("No unsynced Amazon transactions in Actual Budget for account_id=%s", account_id)
        return

    logger.info(
        "Found %d unsynced Amazon transaction(s) in Actual Budget (dry_run=%s)",
        len(candidates),
        config.dry_run,
    )

    # Ensure the amazon_sync and sync_not_found tags exist in Actual Budget
    _ensure_tag(config, "amazon_sync")
    _ensure_tag(config, "sync_not_found")

    synced_tx_ids: list = []

    for actual_tx in candidates:
        actual_tx_id = actual_tx["id"]
        actual_amount = actual_tx.get("amount") or 0  # Signed cents (neg=charge, pos=refund)
        actual_date = actual_tx.get("date")
        if not actual_date:
            continue

        # Match using signed amount — Amazon transactions use the same sign convention
        candidate_orders = db.find_orders_by_amount_and_date(
            account_id, actual_amount, actual_date, date_window_days=_DATE_WINDOW_DAYS
        )

        if not candidate_orders:
            logger.info(
                "No local Amazon order found for Actual tx %s (amount %+.2f, date %s)",
                actual_tx_id, actual_amount / 100.0, actual_date,
            )
            # Tag the transaction so it's deprioritized on subsequent runs
            if not config.dry_run:
                existing_notes = actual_tx.get("notes") or ""
                if "sync_not_found" not in existing_notes.lower():
                    new_notes = (existing_notes + " #sync_not_found").strip()
                    tag_payload = {
                        "action": "updateTransaction",
                        "budgetSyncId": config.budget_sync_id,
                        "transactionId": actual_tx_id,
                        "transaction": {"notes": new_notes},
                    }
                    tag_result = _call_actual_api(tag_payload, config)
                    if "error" in tag_result:
                        logger.warning(
                            "Failed to tag transaction %s with #sync_not_found: %s",
                            actual_tx_id, tag_result["error"],
                        )
                    else:
                        logger.debug("Tagged transaction %s with #sync_not_found", actual_tx_id)
            continue

        target_date = datetime.strptime(actual_date, "%Y-%m-%d").date()

        def _candidate_closeness(candidate: dict) -> tuple[int, int]:
            candidate_total_cents = int(round((candidate.get("tx_total") or 0.0) * 100))
            amount_diff = abs(candidate_total_cents - actual_amount)
            candidate_date = candidate.get("completed_date")
            date_diff = _DATE_WINDOW_DAYS + 1
            if candidate_date:
                try:
                    candidate_dt = datetime.strptime(candidate_date, "%Y-%m-%d").date()
                    date_diff = abs((candidate_dt - target_date).days)
                except ValueError:
                    pass
            return (amount_diff, date_diff)

        def _candidate_tiebreak(candidate: dict) -> int:
            tx_id = candidate.get("tx_id")
            return tx_id if isinstance(tx_id, int) else 0

        ranked_candidates = sorted(
            candidate_orders,
            key=lambda c: (*_candidate_closeness(c), _candidate_tiebreak(c)),
        )
        tx = ranked_candidates[0]

        if len(ranked_candidates) > 1:
            best_closeness = _candidate_closeness(tx)
            equally_close = [c for c in ranked_candidates if _candidate_closeness(c) == best_closeness]
            if len(equally_close) > 1:
                logger.info(
                    "Multiple equally close matches for Actual tx %s; selected tx_id=%s order=%s",
                    actual_tx_id,
                    tx.get("tx_id"),
                    tx.get("order_number") or "<unknown>",
                )
            else:
                logger.info(
                    "Selected closest match for Actual tx %s -> order %s (amount diff=%d cents, date diff=%d day(s))",
                    actual_tx_id,
                    tx.get("order_number") or "<unknown>",
                    best_closeness[0],
                    best_closeness[1],
                )
        elif tx.get("completed_date") == actual_date:
            logger.info(
                "Matched Actual tx %s using exact date %s -> order %s",
                actual_tx_id,
                actual_date,
                tx.get("order_number") or "<unknown>",
            )
        else:
            logger.info(
                "Matched Actual tx %s using tolerance window around %s -> order %s",
                actual_tx_id,
                actual_date,
                tx.get("order_number") or "<unknown>",
            )
        order_number = tx["order_number"] or ""
        seller = tx.get("seller") or ""
        is_refund = bool(tx["is_refund"])
        parent_cents = actual_amount  # Use Actual Budget's signed amount

        # Amazon Prime membership charges have no order number; identify them by
        # seller being "Amazon Prime" or starting with "D".
        is_prime_membership = (
            not order_number
            and (seller == "Amazon Prime" or seller.startswith("D"))
        )
        notes_label = "Annual Prime Membership" if is_prime_membership else order_number

        # --- Build subtransactions ---
        subs: list = []
        order_id = tx.get("order_id")
        full_details = bool(tx.get("full_details_fetched"))

        if is_prime_membership:
            subs = [{"notes": "Annual Prime Membership", "amount": parent_cents}]
        elif not order_id or not full_details:
            logger.info(
                "Order %s missing full details — skipping until backfill completes",
                order_number,
            )
            continue
        elif is_refund:
            # For refunds: identify which item(s) were returned
            all_items = db.get_all_items_for_order(order_id)  # type: ignore[arg-type]
            if not all_items:
                continue

            items_to_split = all_items  # Fallback

            # Check if items have already been explicitly linked to this refund
            linked = db.get_items_for_transaction(tx["tx_id"])
            if linked:
                items_to_split = linked
                logger.debug(
                    "Refund %s: using %d explicitly linked item(s)",
                    order_number, len(linked),
                )
            else:
                # Attempt to auto-identify the returned item(s) by amount
                auto = _auto_match_refund_items(all_items, parent_cents, tx)
                if auto:
                    items_to_split = auto
                    db.link_transaction_items(
                        tx["tx_id"], [i["id"] for i in auto]
                    )

            subs = _build_subtransactions(items_to_split, parent_cents)
        else:
            # For charges: determine which items this specific charge covers
            items_to_split = _identify_items_for_charge(
                db, order_id, order_number, account_id, parent_cents, tx
            )
            if not items_to_split:
                continue

            subs = _build_subtransactions(items_to_split, parent_cents)

        if not subs:
            continue

        if config.dry_run:
            _log_split(notes_label, tx, subs, actual_tx=actual_tx)
            continue

        # --- Live update ---
        tags = account_tags or []
        tags_str = " ".join(f"#{tag}" for tag in tags)
        notes = f"{notes_label} #amazon_sync"
        if tags_str:
            notes = f"{tags_str} {notes}"
        new_transaction: dict = {
            "account": actual_tx.get("account"),
            "date": actual_tx.get("date"),
            "amount": parent_cents,
            "payee": actual_tx.get("payee"),
            "imported_payee": actual_tx.get("imported_payee"),
            "notes": notes,
            "cleared": actual_tx.get("cleared"),
        }
        if subs:
            new_transaction["subtransactions"] = [
                {"amount": s["amount"], "notes": s["notes"]}
                for s in subs
            ]

        patch_payload = {
            "action": "createSplit",
            "budgetSyncId": config.budget_sync_id,
            "accountId": config.account_id,
            "transactionId": actual_tx_id,
            "transaction": new_transaction,
        }
        try:
            result = _call_actual_api(patch_payload, config)
            if "error" in result:
                raise RuntimeError(result["error"])
        except Exception as exc:
            logger.warning(
                "Failed to create split for transaction %s / order %s: %s",
                actual_tx_id, order_number, exc,
            )
            db.log_amazon_sync_error(
                account_id=account_id,
                order_number=order_number,
                error_type="UPDATE_FAILED",
                message=f"Failed to create split for transaction {actual_tx_id}: {exc}",
                order_json=json.dumps(tx, default=str),
            )
            continue

        # Mark synced immediately so subsequent candidates in this same run
        # cannot match the same DB transaction again.
        db.mark_transactions_synced([tx["tx_id"]])
        db.update_transaction_actual_ids([tx["tx_id"]], [actual_tx_id])
        synced_tx_ids.append(tx["tx_id"])
        logger.info(
            "Updated transaction %s for order %s with %d split(s)",
            actual_tx_id, order_number, len(subs),
        )

    if config.dry_run:
        logger.info("[DRY RUN] %d transaction(s) logged (not sent to Actual Budget)", len(candidates))
        return

    if synced_tx_ids:
        logger.info(
            "Successfully split %d transaction(s) in Actual Budget",
            len(synced_tx_ids),
        )
    else:
        logger.info("No transactions were updated in Actual Budget")
