import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator, Optional

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    label           TEXT,
    last_synced_at  TEXT,
    authenticated_at TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id              INTEGER NOT NULL REFERENCES accounts(id),
    order_number            TEXT    NOT NULL,
    order_placed_date       TEXT,
    grand_total             REAL,
    cancelled               INTEGER NOT NULL DEFAULT 0,
    recipient_name          TEXT,
    recipient_address       TEXT,
    payment_method          TEXT,
    payment_method_last_4   INTEGER,
    subtotal                REAL,
    shipping_total          REAL,
    free_shipping           REAL,
    promotion_applied       REAL,
    coupon_savings          REAL,
    reward_points           REAL,
    subscription_discount   REAL,
    total_before_tax        REAL,
    estimated_tax           REAL,
    refund_total            REAL,
    multibuy_discount       REAL,
    amazon_discount         REAL,
    gift_card               REAL,
    gift_wrap               REAL,
    order_details_link      TEXT,
    full_details_fetched    INTEGER NOT NULL DEFAULT 0,
    synced_at               TEXT    NOT NULL,
    UNIQUE(account_id, order_number)
);

CREATE TABLE IF NOT EXISTS order_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    title               TEXT,
    link                TEXT,
    price               REAL,
    seller_name         TEXT,
    condition           TEXT,
    quantity            INTEGER,
    image_link          TEXT,
    return_eligible_date TEXT
);

CREATE TABLE IF NOT EXISTS order_shipments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    delivery_status TEXT,
    tracking_link   TEXT,
    items_subtotal  REAL
);

CREATE TABLE IF NOT EXISTS order_shipment_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id   INTEGER NOT NULL REFERENCES order_shipments(id) ON DELETE CASCADE,
    order_item_id INTEGER NOT NULL REFERENCES order_items(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS transactions (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id              INTEGER NOT NULL REFERENCES accounts(id),
    order_number            TEXT,
    completed_date          TEXT    NOT NULL,
    payment_method          TEXT,
    payment_method_last_4   TEXT,
    grand_total             REAL,
    is_refund               INTEGER NOT NULL DEFAULT 0,
    seller                  TEXT,
    order_details_link      TEXT,
    synced_at               TEXT    NOT NULL,
    UNIQUE(account_id, order_number, completed_date, grand_total)
);
"""

# Columns added after initial schema; migrated at startup.
_MIGRATIONS = [
    ("orders", "free_shipping",         "REAL"),
    ("orders", "promotion_applied",     "REAL"),
    ("orders", "coupon_savings",        "REAL"),
    ("orders", "reward_points",         "REAL"),
    ("orders", "subscription_discount", "REAL"),
    ("orders", "total_before_tax",      "REAL"),
    ("orders", "refund_total",          "REAL"),
    ("orders", "multibuy_discount",     "REAL"),
    ("orders", "amazon_discount",       "REAL"),
    ("orders", "gift_card",             "REAL"),
    ("orders", "gift_wrap",             "REAL"),
    ("order_items", "return_eligible_date", "TEXT"),
    ("order_shipments", "items_subtotal", "REAL"),
    ("transactions", "actual_synced_at", "TEXT"),
    ("transactions", "actual_transaction_id", "TEXT"),
]

# Post-schema table creation for tables added after initial release.
_POST_SCHEMA = """
CREATE TABLE IF NOT EXISTS amazon_sync_errors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    order_number    TEXT    NOT NULL,
    error_type      TEXT    NOT NULL,
    message         TEXT    NOT NULL,
    order_json      TEXT,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS transaction_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    order_item_id   INTEGER NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
    UNIQUE(transaction_id, order_item_id)
);
"""


class Database:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.executescript(_POST_SCHEMA)
            # Migrate existing DBs that predate the authenticated_at column.
            try:
                conn.execute("ALTER TABLE accounts ADD COLUMN authenticated_at TEXT")
            except Exception:
                pass  # column already exists
            for table, column, col_type in _MIGRATIONS:
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                except Exception:
                    pass  # column already exists
        logger.debug("Database schema initialised at %s", self.db_path)

    def get_or_create_account(self, username: str, label: Optional[str]) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM accounts WHERE username = ?", (username,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE accounts SET label = ? WHERE id = ?", (label, row["id"])
                )
                return int(row["id"])
            cursor = conn.execute(
                "INSERT INTO accounts (username, label) VALUES (?, ?)", (username, label)
            )
            return cursor.lastrowid  # type: ignore[return-value]

    def get_last_synced(self, account_id: int) -> Optional[datetime]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_synced_at FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        if row and row["last_synced_at"]:
            return datetime.fromisoformat(row["last_synced_at"])
        return None

    def update_last_synced(self, account_id: int, synced_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET last_synced_at = ? WHERE id = ?",
                (synced_at.isoformat(), account_id),
            )

    def get_account_info(self, username: str) -> Optional[dict]:
        """Return {id, label, last_synced_at, authenticated_at} for the given username, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, label, last_synced_at, authenticated_at FROM accounts WHERE username = ?",
                (username,),
            ).fetchone()
        if row:
            return {
                "id": int(row["id"]),
                "label": row["label"],
                "last_synced_at": row["last_synced_at"],
                "authenticated_at": row["authenticated_at"],
            }
        return None

    def set_authenticated(self, account_id: int, when: Optional[datetime] = None) -> None:
        ts = (when or datetime.utcnow()).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET authenticated_at = ? WHERE id = ?",
                (ts, account_id),
            )

    def upsert_order(self, account_id: int, order, full_details: bool) -> bool:
        """Upsert an order and its items/shipments. Returns True if newly inserted."""
        synced_at = datetime.utcnow().isoformat()

        recipient_name: Optional[str] = None
        recipient_address: Optional[str] = None
        if order.recipient:
            recipient_name = order.recipient.name
            recipient_address = getattr(order.recipient, "address", None)

        order_placed: Optional[str] = (
            order.order_placed_date.isoformat() if order.order_placed_date else None
        )

        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM orders WHERE account_id = ? AND order_number = ?",
                (account_id, order.order_number),
            ).fetchone()

            def _fd(attr: str):
                """Return the order attribute only when full_details is True."""
                return getattr(order, attr, None) if full_details else None

            order_values = (
                order_placed,
                order.grand_total,
                1 if order.cancelled else 0,
                recipient_name,
                recipient_address,
                _fd("payment_method"),
                _fd("payment_method_last_4"),
                _fd("subtotal"),
                _fd("shipping_total"),
                _fd("free_shipping"),
                _fd("promotion_applied"),
                _fd("coupon_savings"),
                _fd("reward_points"),
                _fd("subscription_discount"),
                _fd("total_before_tax"),
                _fd("estimated_tax"),
                _fd("refund_total"),
                _fd("multibuy_discount"),
                _fd("amazon_discount"),
                _fd("gift_card"),
                _fd("gift_wrap"),
                order.order_details_link,
                1 if full_details else 0,
                synced_at,
            )

            if existing:
                order_id = int(existing["id"])
                conn.execute(
                    """UPDATE orders SET
                        order_placed_date       = ?,
                        grand_total             = ?,
                        cancelled               = ?,
                        recipient_name          = ?,
                        recipient_address       = ?,
                        payment_method          = ?,
                        payment_method_last_4   = ?,
                        subtotal                = ?,
                        shipping_total          = ?,
                        free_shipping           = ?,
                        promotion_applied       = ?,
                        coupon_savings          = ?,
                        reward_points           = ?,
                        subscription_discount   = ?,
                        total_before_tax        = ?,
                        estimated_tax           = ?,
                        refund_total            = ?,
                        multibuy_discount       = ?,
                        amazon_discount         = ?,
                        gift_card               = ?,
                        gift_wrap               = ?,
                        order_details_link      = ?,
                        full_details_fetched    = ?,
                        synced_at               = ?
                    WHERE id = ?""",
                    (*order_values, order_id),
                )
                conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
                conn.execute("DELETE FROM order_shipments WHERE order_id = ?", (order_id,))
                is_new = False
            else:
                cursor = conn.execute(
                    """INSERT INTO orders (
                        account_id, order_number,
                        order_placed_date, grand_total, cancelled,
                        recipient_name, recipient_address,
                        payment_method, payment_method_last_4,
                        subtotal, shipping_total,
                        free_shipping, promotion_applied, coupon_savings,
                        reward_points, subscription_discount, total_before_tax,
                        estimated_tax, refund_total, multibuy_discount,
                        amazon_discount, gift_card, gift_wrap,
                        order_details_link, full_details_fetched, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (account_id, order.order_number, *order_values),
                )
                order_id = cursor.lastrowid  # type: ignore[assignment]
                is_new = True

            for item in order.items:
                seller_name = item.seller.name if item.seller else None
                return_eligible_date = (
                    item.return_eligible_date.isoformat()
                    if getattr(item, "return_eligible_date", None)
                    else None
                )
                conn.execute(
                    """INSERT INTO order_items
                        (order_id, title, link, price, seller_name, condition, quantity, image_link, return_eligible_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        order_id,
                        item.title,
                        item.link,
                        item.price,
                        seller_name,
                        getattr(item, "condition", None),
                        getattr(item, "quantity", None),
                        getattr(item, "image_link", None),
                        return_eligible_date,
                    ),
                )

            # Build a title->item_id map for shipment->item linking
            item_id_by_title: dict = {}
            rows = conn.execute(
                "SELECT id, title FROM order_items WHERE order_id = ?", (order_id,)
            ).fetchall()
            for r in rows:
                item_id_by_title[r["title"]] = r["id"]

            for shipment in order.shipments:
                shipment_items = getattr(shipment, "items", []) or []
                items_subtotal = sum(
                    (getattr(it, "price", None) or 0) * max(getattr(it, "quantity", None) or 1, 1)
                    for it in shipment_items
                )
                cursor = conn.execute(
                    """INSERT INTO order_shipments (order_id, delivery_status, tracking_link, items_subtotal)
                    VALUES (?, ?, ?, ?)""",
                    (
                        order_id,
                        getattr(shipment, "delivery_status", None),
                        getattr(shipment, "tracking_link", None),
                        items_subtotal if shipment_items else None,
                    ),
                )
                shipment_id = cursor.lastrowid
                for sit in shipment_items:
                    sit_title = getattr(sit, "title", None)
                    if sit_title and sit_title in item_id_by_title:
                        conn.execute(
                            """INSERT OR IGNORE INTO order_shipment_items (shipment_id, order_item_id)
                            VALUES (?, ?)""",
                            (shipment_id, item_id_by_title[sit_title]),
                        )

        return is_new

    def upsert_transaction(self, account_id: int, transaction) -> bool:
        """Upsert a transaction. Returns True if newly inserted."""
        synced_at = datetime.utcnow().isoformat()
        completed_date = (
            transaction.completed_date.isoformat()
            if transaction.completed_date
            else None
        )
        with self._connect() as conn:
            existing = conn.execute(
                """SELECT id FROM transactions
                   WHERE account_id = ? AND order_number = ? AND completed_date = ? AND grand_total = ?""",
                (account_id, transaction.order_number, completed_date, transaction.grand_total),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE transactions SET
                        payment_method        = ?,
                        payment_method_last_4 = ?,
                        is_refund             = ?,
                        seller                = ?,
                        order_details_link    = ?,
                        synced_at             = ?
                    WHERE id = ?""",
                    (
                        getattr(transaction, "payment_method", None),
                        getattr(transaction, "payment_method_last_4", None),
                        1 if transaction.is_refund else 0,
                        getattr(transaction, "seller", None),
                        getattr(transaction, "order_details_link", None),
                        synced_at,
                        int(existing["id"]),
                    ),
                )
                return False
            conn.execute(
                """INSERT INTO transactions (
                    account_id, order_number, completed_date,
                    payment_method, payment_method_last_4,
                    grand_total, is_refund, seller, order_details_link, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account_id,
                    transaction.order_number,
                    completed_date,
                    getattr(transaction, "payment_method", None),
                    getattr(transaction, "payment_method_last_4", None),
                    transaction.grand_total,
                    1 if transaction.is_refund else 0,
                    getattr(transaction, "seller", None),
                    getattr(transaction, "order_details_link", None),
                    synced_at,
                ),
            )
        return True

    def update_transaction_actual_ids(self, transaction_ids: list, actual_ids: list) -> None:
        """Store the Actual Budget transaction id for each local transaction."""
        if not transaction_ids or not actual_ids or len(transaction_ids) != len(actual_ids):
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE transactions SET actual_transaction_id = ? WHERE id = ?",
                [(aid, tid) for tid, aid in zip(transaction_ids, actual_ids)],
            )

    def get_transactions_pending_sync(self, account_id: int, since_date: Optional[str] = None) -> list:
        """Return all transactions not yet pushed to Actual Budget, excluding gift card payments.

        Args:
            account_id: The account to query.
            since_date: Optional ISO date string (YYYY-MM-DD). When provided, only transactions
                        on or after this date are returned.
        """
        with self._connect() as conn:
            query = """
                SELECT
                    t.id            AS tx_id,
                    t.order_number,
                    t.completed_date,
                    t.grand_total   AS tx_total,
                    t.is_refund,
                    t.payment_method,
                    o.id            AS order_id,
                    o.subtotal,
                    o.shipping_total,
                    o.estimated_tax,
                    o.grand_total   AS order_total,
                    o.full_details_fetched
                FROM transactions t
                LEFT JOIN orders o
                       ON o.order_number = t.order_number
                      AND o.account_id   = t.account_id
                WHERE t.account_id       = ?
                  AND t.actual_synced_at IS NULL
                  AND (t.payment_method IS NULL OR t.payment_method NOT LIKE '%Gift Card%')
            """
            params: list = [account_id]
            if since_date:
                query += "  AND t.completed_date >= ?\n"
                params.append(since_date)
            query += "ORDER BY t.completed_date"
            rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_shipments_for_order(self, order_id: int) -> list:
        """Return shipments with their linked item ids and subtotal for an order."""
        with self._connect() as conn:
            shipments = conn.execute(
                "SELECT id, items_subtotal FROM order_shipments WHERE order_id = ?",
                (order_id,),
            ).fetchall()
            result = []
            for s in shipments:
                items = conn.execute(
                    """
                    SELECT oi.id, oi.title, oi.price, oi.quantity
                    FROM order_shipment_items si
                    JOIN order_items oi ON oi.id = si.order_item_id
                    WHERE si.shipment_id = ?
                    """,
                    (s["id"],),
                ).fetchall()
                result.append({
                    "shipment_id": s["id"],
                    "items_subtotal": s["items_subtotal"],
                    "items": [dict(i) for i in items],
                })
        return result

    def get_all_items_for_order(self, order_id: int) -> list:
        """Return all order_items rows for an order."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, title, price, quantity FROM order_items WHERE order_id = ?",
                (order_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_items_for_transaction(self, transaction_id: int) -> list:
        """Return order_items linked to a specific transaction via transaction_items.

        Returns an empty list if no items have been explicitly linked yet.
        """
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT oi.id, oi.title, oi.price, oi.quantity
                   FROM transaction_items ti
                   JOIN order_items oi ON oi.id = ti.order_item_id
                   WHERE ti.transaction_id = ?""",
                (transaction_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def link_transaction_items(self, transaction_id: int, item_ids: list) -> None:
        """Link a set of order_items to a transaction (idempotent)."""
        if not item_ids:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO transaction_items (transaction_id, order_item_id) VALUES (?, ?)",
                [(transaction_id, iid) for iid in item_ids],
            )

    def mark_transactions_synced(self, transaction_ids: list) -> None:
        """Set actual_synced_at = now() for the given transaction ids."""
        if not transaction_ids:
            return
        ts = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.executemany(
                "UPDATE transactions SET actual_synced_at = ? WHERE id = ?",
                [(ts, tid) for tid in transaction_ids],
            )

    def get_orders_needing_backfill(self, account_id: int) -> list:
        """Return list of (order_number,) for orders missing full details for the given account."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT order_number FROM orders
                   WHERE account_id = ? AND full_details_fetched = 0""",
                (account_id,),
            ).fetchall()
        return [row["order_number"] for row in rows]

    def get_oldest_order_date(self, account_id: int) -> Optional[datetime]:
        """Return the order_placed_date of the oldest order for the given account, or None."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT MIN(order_placed_date) AS oldest FROM orders
                   WHERE account_id = ? AND order_placed_date IS NOT NULL""",
                (account_id,),
            ).fetchone()
        if row and row["oldest"]:
            return datetime.fromisoformat(row["oldest"])
        return None

    def get_order_summaries(self, start_date: str, end_date: str) -> list:
        """Return orders (with items) within an inclusive order date range."""
        with self._connect() as conn:
            order_rows = conn.execute(
                """
                SELECT
                    o.id,
                    o.order_number,
                    o.order_placed_date,
                    o.grand_total,
                    o.cancelled,
                    o.order_details_link,
                    o.shipping_total,
                    o.free_shipping,
                    o.promotion_applied,
                    o.coupon_savings,
                    o.subscription_discount,
                    o.multibuy_discount,
                    o.amazon_discount,
                    o.reward_points,
                    o.gift_card,
                    o.refund_total,
                    a.username AS account_username,
                    a.label AS account_label
                FROM orders o
                JOIN accounts a ON a.id = o.account_id
                WHERE o.order_placed_date IS NOT NULL
                  AND o.order_placed_date >= ?
                  AND o.order_placed_date <= ?
                ORDER BY o.order_placed_date DESC, o.order_number DESC
                """,
                (start_date, end_date),
            ).fetchall()

            orders = [dict(r) for r in order_rows]
            if not orders:
                return []

            order_ids = [o["id"] for o in orders]
            placeholders = ",".join("?" for _ in order_ids)
            item_rows = conn.execute(
                f"""
                SELECT
                    order_id,
                    title,
                    price,
                    quantity
                FROM order_items
                WHERE order_id IN ({placeholders})
                ORDER BY id
                """,
                order_ids,
            ).fetchall()
            item_rows = [dict(r) for r in item_rows]

            shipment_rows = conn.execute(
                f"""
                SELECT
                    order_id,
                    delivery_status
                FROM order_shipments
                WHERE order_id IN ({placeholders})
                ORDER BY id
                """,
                order_ids,
            ).fetchall()
            shipment_rows = [dict(r) for r in shipment_rows]

        items_by_order: dict[int, list] = {}
        for d in item_rows:
            order_id = int(d["order_id"])
            items_by_order.setdefault(order_id, []).append(
                {
                    "title": d.get("title"),
                    "price": d.get("price"),
                    "quantity": d.get("quantity"),
                }
            )

        shipment_statuses_by_order: dict[int, list[str]] = {}
        for d in shipment_rows:
            order_id = int(d["order_id"])
            status = (d.get("delivery_status") or "").strip()
            if status:
                shipment_statuses_by_order.setdefault(order_id, []).append(status)

        for order in orders:
            order_items = items_by_order.get(int(order["id"]), [])
            order["items"] = order_items
            order["item_count"] = sum((it.get("quantity") or 1) for it in order_items)
            order["shipment_statuses"] = shipment_statuses_by_order.get(int(order["id"]), [])
            order["account_label"] = order.get("account_label") or order.get("account_username")

        return orders

    # -----------------------------------------------------------------------
    # Amazon sync error logging
    # -----------------------------------------------------------------------

    def log_amazon_sync_error(
        self,
        account_id: int,
        order_number: str,
        error_type: str,
        message: str,
        order_json: Optional[str] = None,
    ) -> None:
        """Log an error that occurred while syncing an Amazon order to Actual Budget."""
        ts = datetime.utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO amazon_sync_errors
                    (account_id, order_number, error_type, message, order_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (account_id, order_number, error_type, message, order_json, ts),
            )

    def truncate_amazon_sync_errors(self, account_id: int) -> None:
        """Delete all sync errors for the given account."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM amazon_sync_errors WHERE account_id = ?",
                (account_id,),
            )

    def get_amazon_sync_errors(self, account_id: int, limit: int = 100) -> list:
        """Return recent sync errors for the given account."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT id, order_number, error_type, message, order_json, created_at
                   FROM amazon_sync_errors
                   WHERE account_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (account_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_amazon_sync_error(self, error_id: int) -> Optional[dict]:
        """Return a single sync error by id."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT id, account_id, order_number, error_type, message, order_json, created_at
                   FROM amazon_sync_errors
                   WHERE id = ?""",
                (error_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_orders_by_amount_and_date(
        self,
        account_id: int,
        amount_cents: int,
        date_str: str,
        date_window_days: int = 3,
        tolerance_cents: int = 1,
    ) -> list:
        """Find Amazon transactions matching an Actual Budget amount within ±date_window_days.

        Args:
            amount_cents: The signed amount in cents (negative for charges, positive for refunds)
                          as reported by Actual Budget.
            tolerance_cents: Maximum allowed difference in cents between the Actual Budget amount
                             and the Amazon transaction amount. Default 1 cent.

          Returns rows with order + transaction details suitable for Actual Budget matching.
          Matching strategy:
          1) Look for exact date matches first; if there is exactly one match, return it.
          2) Otherwise, fall back to the +/- date window and return all matches sorted by
              closest amount then date.
        """
        from datetime import datetime, timedelta
        target_dt = datetime.strptime(date_str, "%Y-%m-%d").date()
        min_date = (target_dt - timedelta(days=date_window_days)).isoformat()
        max_date = (target_dt + timedelta(days=date_window_days)).isoformat()

        # Amazon transactions store amounts as negative for charges, positive for refunds
        # (same sign convention as Actual Budget). Convert cents to dollars for comparison.
        amount_dollars = amount_cents / 100.0
        tolerance_dollars = tolerance_cents / 100.0

        with self._connect() as conn:
            exact_rows = conn.execute(
                """
                SELECT
                    t.id            AS tx_id,
                    t.order_number,
                    t.seller,
                    t.completed_date,
                    t.grand_total   AS tx_total,
                    t.is_refund,
                    t.payment_method,
                    o.id            AS order_id,
                    o.subtotal,
                    o.shipping_total,
                    o.estimated_tax,
                    o.grand_total   AS order_total,
                    o.full_details_fetched,
                    o.gift_card,
                    o.promotion_applied,
                    o.coupon_savings,
                    o.reward_points,
                    o.subscription_discount,
                    o.multibuy_discount,
                    o.amazon_discount
                FROM transactions t
                LEFT JOIN orders o
                       ON o.order_number = t.order_number
                      AND o.account_id   = t.account_id
                WHERE t.account_id       = ?
                  AND t.completed_date   = ?
                  AND (t.payment_method IS NULL OR t.payment_method NOT LIKE '%Gift Card%')
                  AND t.actual_synced_at IS NULL
                  AND ABS(t.grand_total - ?) <= ?
                ORDER BY ABS(t.grand_total - ?), t.completed_date
                """,
                (account_id, date_str, amount_dollars, tolerance_dollars, amount_dollars),
            ).fetchall()

            if len(exact_rows) == 1:
                return [dict(exact_rows[0])]

            rows = conn.execute(
                """
                SELECT
                    t.id            AS tx_id,
                    t.order_number,
                    t.seller,
                    t.completed_date,
                    t.grand_total   AS tx_total,
                    t.is_refund,
                    t.payment_method,
                    o.id            AS order_id,
                    o.subtotal,
                    o.shipping_total,
                    o.estimated_tax,
                    o.grand_total   AS order_total,
                    o.full_details_fetched,
                    o.gift_card,
                    o.promotion_applied,
                    o.coupon_savings,
                    o.reward_points,
                    o.subscription_discount,
                    o.multibuy_discount,
                    o.amazon_discount
                FROM transactions t
                LEFT JOIN orders o
                       ON o.order_number = t.order_number
                      AND o.account_id   = t.account_id
                WHERE t.account_id       = ?
                  AND t.completed_date  >= ?
                  AND t.completed_date  <= ?
                  AND (t.payment_method IS NULL OR t.payment_method NOT LIKE '%Gift Card%')
                  AND t.actual_synced_at IS NULL
                  AND ABS(t.grand_total - ?) <= ?
                ORDER BY ABS(t.grand_total - ?), t.completed_date
                """,
                (account_id, min_date, max_date, amount_dollars, tolerance_dollars, amount_dollars),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_charge_count_for_order(self, order_number: str, account_id: int) -> int:
        """Return the number of non-gift-card charge transactions for an order."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) as cnt FROM transactions
                   WHERE order_number = ? AND account_id = ?
                   AND is_refund = 0
                   AND (payment_method IS NULL OR payment_method NOT LIKE '%Gift Card%')""",
                (order_number, account_id),
            ).fetchone()
        return row["cnt"] if row else 0

    def get_shipments_with_items_for_order(self, order_id: int) -> list:
        """Return shipments with their items for shipment-to-charge attribution.

        Each result contains shipment_id, calculated items_subtotal, and item details.
        """
        with self._connect() as conn:
            shipments = conn.execute(
                "SELECT id, delivery_status FROM order_shipments WHERE order_id = ?",
                (order_id,),
            ).fetchall()
            result = []
            for s in shipments:
                items = conn.execute(
                    """SELECT oi.id, oi.title, oi.price, oi.quantity
                       FROM order_shipment_items si
                       JOIN order_items oi ON oi.id = si.order_item_id
                       WHERE si.shipment_id = ?""",
                    (s["id"],),
                ).fetchall()
                item_list = [dict(i) for i in items]
                subtotal = sum(
                    (it["price"] or 0) * (it["quantity"] or 1) for it in item_list
                )
                result.append({
                    "shipment_id": s["id"],
                    "delivery_status": s["delivery_status"],
                    "items_subtotal": subtotal,
                    "items": item_list,
                })
        return result
