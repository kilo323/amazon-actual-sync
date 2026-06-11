import sqlite3
import sys

DB = "data/orders.db"

if len(sys.argv) < 2:
    print("Usage: python _show_order.py <order_number>")
    sys.exit(1)

ORDER = sys.argv[1]

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# Full order financials
o = conn.execute("""
    SELECT order_number, grand_total, subtotal, shipping_total, free_shipping,
           promotion_applied, coupon_savings, estimated_tax, refund_total, cancelled
    FROM orders WHERE order_number = ?
""", (ORDER,)).fetchone()

print("=== ORDER FINANCIALS ===")
for k in o.keys():
    print(f"  {k} = {o[k]}")

sub = o["subtotal"] or 0
ship = o["shipping_total"] or 0
tax = o["estimated_tax"] or 0
total = o["grand_total"] or 0
computed = sub + ship + tax
print(f"\n  subtotal + shipping + tax = {sub} + {ship} + {tax} = {computed:.2f}")
print(f"  grand_total               = {total}")
print(f"  difference (discount)     = {computed - total:.2f}")

print("\n=== ITEMS ===")
items = conn.execute("""
    SELECT i.title, i.price, i.quantity
    FROM order_items i
    JOIN orders o ON o.id = i.order_id
    WHERE o.order_number = ?
""", (ORDER,)).fetchall()
for r in items:
    qty = r["quantity"] if r["quantity"] is not None else 1
    print(f"  [{qty}x] ${r['price']}  {r['title'][:80]}")

print("\n=== TRANSACTIONS ===")
txns = conn.execute("""
    SELECT completed_date, grand_total, is_refund, payment_method, payment_method_last_4
    FROM transactions WHERE order_number = ?
    ORDER BY completed_date
""", (ORDER,)).fetchall()
for r in txns:
    kind = "REFUND" if r["is_refund"] else "CHARGE"
    print(f"  [{kind}] {r['completed_date']}  ${r['grand_total']}  {r['payment_method']} ...{r['payment_method_last_4']}")
if not txns:
    print("  (none)")

# --- Simulate Actual Budget output ---
print("\n=== SIMULATED ACTUAL BUDGET TRANSACTIONS ===")

gift_card_only = all(
    "Gift Card" in (r["payment_method"] or "") and "Visa" not in (r["payment_method"] or "")
    for r in txns
) if txns else True

if gift_card_only:
    print("  >> FILTERED OUT — payment method is Gift Card only, no credit card charge to match.")
    print(f"  >> NOTE: order.refund_total = ${o['refund_total']} but no refund transactions row exists")
    print("  >>       (gift card credits don't appear in Amazon transaction history)")
    print()
    print("  Hypothetical split if it WERE a credit card charge:")

# Build subtransactions
item_rows = [(r["title"][:50], r["price"], r["quantity"] if r["quantity"] else 1) for r in items]
item_total = sum(p * q for _, p, q in item_rows)

subtxns = []
for title, price, qty in item_rows:
    amt = round(price * qty, 2)
    item_tax = round(tax * (amt / item_total), 2) if item_total else 0
    subtxns.append((title, -amt))
    subtxns.append((f"Tax: {title}", -item_tax))

# Effective shipping = grand_total - subtotal - tax
effective_ship = round(total - sub - tax, 2)
if effective_ship > 0.005:
    subtxns.append(("Shipping", -effective_ship))

subtxn_sum = round(sum(a for _, a in subtxns), 2)
adjustment = round(-total - subtxn_sum, 2)
if abs(adjustment) >= 0.01:
    subtxns.append(("Adjustments", adjustment))

charge_label = "CHARGE" if not gift_card_only else "CHARGE (hypothetical)"
print(f"\n  [{charge_label}] {txns[0]['completed_date'] if txns else 'YYYY-MM-DD'}")
print(f"  Amazon - James                          ${-total:.2f}")
for name, amt in subtxns:
    sign = "-" if amt < 0 else "+"
    print(f"    {name:<45} {sign}${abs(amt):.2f}")
print(f"  {'Subtransaction total:':<48} ${sum(a for _, a in subtxns):.2f}  (should = -{total:.2f})")

# Refund
print()
refund = o["refund_total"] or 0
if refund:
    refund_txns = [r for r in txns if r["is_refund"]]
    if refund_txns:
        for rt in refund_txns:
            print(f"  [REFUND] {rt['completed_date']}")
            print(f"  Amazon Refund - James                   +${rt['grand_total']:.2f}")
            rf = rt["grand_total"]
            for name, amt in subtxns:
                scaled = round(amt * (rf / total), 2)
                sign = "+" if scaled > 0 else "-"
                print(f"    {name:<45} {sign}${abs(scaled):.2f}")
    else:
        print(f"  [REFUND] No transactions row — refund_total=${refund} went back to Gift Card.")
        print("           This refund CANNOT be auto-posted to Actual Budget from transactions data.")

conn.close()
