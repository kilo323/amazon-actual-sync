#!/usr/bin/env python3
"""
Reset Actual Budget sync markers in the local SQLite database so transactions
will be re-evaluated and re-synced on the next run.

Usage:
    python reset_actual_sync.py [--account USERNAME] [--dry-run] [--yes]

By default resets `actual_synced_at` (and optionally `actual_transaction_id`)
for all transactions. Use --account to limit to a specific Amazon account.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).with_name("data") / "orders.db"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset Actual Budget sync markers in orders.db"
    )
    parser.add_argument(
        "--account",
        metavar="USERNAME",
        help="Only reset transactions for the given Amazon account username",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=str(DB_PATH),
        help=f"Path to the SQLite database (default: {DB_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be updated without making changes",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )
    parser.add_argument(
        "--keep-actual-id",
        action="store_true",
        help="Do NOT clear the actual_transaction_id column",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Verify the transactions table has the columns we need
    cur.execute("PRAGMA table_info(transactions)")
    columns = {row["name"] for row in cur.fetchall()}
    if "actual_synced_at" not in columns:
        print(
            "ERROR: 'actual_synced_at' column not found in transactions table. "
            "Database may be from an older version.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Build query
    where_clauses = ["actual_synced_at IS NOT NULL"]
    params: list = []

    if args.account:
        cur.execute("SELECT id FROM accounts WHERE username = ?", (args.account,))
        row = cur.fetchone()
        if not row:
            print(f"No account found with username: {args.account}", file=sys.stderr)
            sys.exit(1)
        account_id = row["id"]
        where_clauses.append("account_id = ?")
        params.append(account_id)

    where_sql = " AND ".join(where_clauses)

    # Count affected rows
    count_sql = f"SELECT COUNT(*) FROM transactions WHERE {where_sql}"
    cur.execute(count_sql, params)
    count = cur.fetchone()[0]

    if count == 0:
        print("No synced transactions found to reset.")
        sys.exit(0)

    scope = f"for account '{args.account}'" if args.account else "across all accounts"
    print(f"Found {count} synced transaction(s) {scope} to reset.")

    if args.dry_run:
        print("[DRY RUN] No changes were made.")
        sys.exit(0)

    if not args.yes:
        try:
            confirm = input("Proceed? [y/N]: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            sys.exit(1)
        if confirm.lower() not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    # Perform update
    set_parts = ["actual_synced_at = NULL"]
    if "actual_transaction_id" in columns and not args.keep_actual_id:
        set_parts.append("actual_transaction_id = NULL")

    update_sql = f"UPDATE transactions SET {', '.join(set_parts)} WHERE {where_sql}"
    cur.execute(update_sql, params)
    conn.commit()

    print(f"Reset {cur.rowcount} transaction(s). They will be re-synced on the next run.")
    conn.close()


if __name__ == "__main__":
    main()
