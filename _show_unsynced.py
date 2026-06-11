import sqlite3

DB = r"data/orders.db"
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

query = """
SELECT 
    t.order_number,
    t.completed_date,
    t.grand_total,
    t.payment_method,
    t.is_refund,
    t.seller
FROM transactions t
WHERE t.actual_synced_at IS NULL
  AND (t.payment_method IS NULL OR t.payment_method NOT LIKE '%Gift Card%')
ORDER BY t.completed_date
"""

print("Unsynced non-gift-card transactions:")
print("-" * 90)
print(f"{'Date':<12} | {'Order #':<20} | {'Type':<7} | {'Amount':>10} | {'Payment Method':<25} | {'Seller'}")
print("-" * 90)
for r in cur.execute(query).fetchall():
    refund_flag = "REFUND" if r["is_refund"] else "CHARGE"
    amt = f"${r['grand_total']:.2f}"
    print(
        f"{r['completed_date']:<12} | {r['order_number']:<20} | {refund_flag:<7} | {amt:>10} | {str(r['payment_method']):<25} | {r['seller']}"
    )
