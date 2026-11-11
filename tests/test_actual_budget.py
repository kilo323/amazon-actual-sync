import tempfile
import unittest
from pathlib import Path

from app.actual_budget import _assign_shipments_to_charges, _reconcile_sync_markers
from app.config import ActualBudgetConfig
from app.database import Database


class _MarkerDatabase:
    def __init__(self, rows):
        self.rows = rows
        self.cleared_ids = []

    def get_synced_transactions_in_range(self, account_id, start_date, end_date):
        return self.rows

    def clear_actual_sync_markers(self, transaction_ids):
        self.cleared_ids.extend(transaction_ids)


class ActualBudgetShipmentAssignmentTests(unittest.TestCase):
    def test_rewards_are_not_counted_as_card_charges(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = Database(str(Path(temp_dir) / "orders.db"))
            account_id = db.get_or_create_account("test@example.com", "Test")
            with db._connect() as conn:
                for amount, payment_method in (
                    (-16.22, "Prime Visa ****7943"),
                    (-6.41, "Prime Visa ****7943"),
                    (-10.26, "Prime Visa ****7943"),
                    (-4.10, "Amazon Chase Credit Card Rewards"),
                ):
                    conn.execute(
                        """INSERT INTO transactions (
                            account_id, order_number, completed_date, grand_total,
                            payment_method, is_refund, synced_at
                        ) VALUES (?, ?, ?, ?, ?, 0, ?)""",
                        (
                            account_id,
                            "113-2118242-8497866",
                            "2026-11-05",
                            amount,
                            payment_method,
                            "2026-11-11",
                        ),
                    )

            charges = db.get_charge_transactions_for_order(
                "113-2118242-8497866", account_id
            )

            self.assertEqual(len(charges), 3)
            self.assertEqual(
                [charge["amount_cents"] for charge in charges],
                [1622, 641, 1026],
            )

    def test_reconciled_charges_are_assigned_to_distinct_shipments(self):
        items = [
            {"id": 89, "price": 9.59, "quantity": None},
            {"id": 88, "price": 5.99, "quantity": None},
            {"id": 87, "price": 18.99, "quantity": None},
        ]
        shipments = [
            {"items": [items[2]]},
            {"items": [items[1]]},
            {"items": [items[0]]},
        ]
        charges = [
            {"tx_id": 42, "amount_cents": 1622},
            {"tx_id": 43, "amount_cents": 641},
            {"tx_id": 44, "amount_cents": 1026},
        ]

        assignment = _assign_shipments_to_charges(
            charges, shipments, order_subtotal=34.57, order_total=32.89,
            all_items=items,
        )

        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertEqual(
            {tx_id: item_list[0]["id"] for tx_id, item_list in assignment.items()},
            {42: 87, 43: 88, 44: 89},
        )

    def test_unreconciled_charges_do_not_use_global_assignment(self):
        item = {"id": 1, "price": 10.0, "quantity": 1}
        shipments = [{"items": [item]}]
        charges = [{"tx_id": 42, "amount_cents": 900}]

        assignment = _assign_shipments_to_charges(
            charges, shipments, order_subtotal=10.0, order_total=10.0,
            all_items=[item],
        )

        self.assertIsNone(assignment)

    def test_reconciliation_does_not_pair_same_amount_from_another_order(self):
        db = _MarkerDatabase(
            [
                {
                    "id": 43,
                    "order_number": "113-2118242-8497866",
                    "completed_date": "2026-11-05",
                    "grand_total": -6.41,
                }
            ]
        )
        existing_transactions = [
            (
                "actual-account",
                {
                    "id": "other-order-parent",
                    "is_parent": True,
                    "amount": -641,
                    "date": "2026-11-05",
                    "notes": "#kerri 113-8995155-4849041 #amazon_sync",
                },
            )
        ]

        _reconcile_sync_markers(
            db, 1, ActualBudgetConfig(dry_run=False), existing_transactions,
            since="2026-11-01", until="2026-11-11",
        )

        self.assertEqual(db.cleared_ids, [43])

    def test_reconciliation_keeps_marker_for_matching_order_parent(self):
        db = _MarkerDatabase(
            [
                {
                    "id": 43,
                    "order_number": "113-2118242-8497866",
                    "completed_date": "2026-11-05",
                    "grand_total": -6.41,
                }
            ]
        )
        existing_transactions = [
            (
                "actual-account",
                {
                    "id": "same-order-parent",
                    "is_parent": True,
                    "amount": -641,
                    "date": "2026-11-05",
                    "notes": "#kerri 113-2118242-8497866 #amazon_sync",
                },
            )
        ]

        _reconcile_sync_markers(
            db, 1, ActualBudgetConfig(dry_run=False), existing_transactions,
            since="2026-11-01", until="2026-11-11",
        )

        self.assertEqual(db.cleared_ids, [])


if __name__ == "__main__":
    unittest.main()
