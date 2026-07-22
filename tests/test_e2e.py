"""End-to-end walkthrough against the running server.

Drives the real HTTP app through the workflows the document specifies:
the dine-in flow (6.1 steps 1-10), seat-level multi-payer settlement (4.2.4),
partial order close (4.2.5), and role-based access control (section 3).

Run the server first:
    uvicorn app.main:app --port 8077
"""
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.oltp import (  # noqa: E402
    Order,
    PaymentInstrument,
    Receipt,
    RestaurantTable,
    Role,
    Seat,
    Staff,
    TableStatus,
)
from app.services.money import money  # noqa: E402
from app.services.payments import balance_panel, build_ledgers  # noqa: E402

BASE = "http://127.0.0.1:8079"
ok = True


def check(cond, label, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  -> ' + detail) if detail else ''}")


db = SessionLocal()
owner = db.execute(select(Staff).where(Staff.role == Role.OWNER)).scalars().first()
waiter = db.execute(select(Staff).where(Staff.role == Role.WAITER)).scalars().first()
kitchen = db.execute(select(Staff).where(Staff.role == Role.KITCHEN)).scalars().first()

client = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)

# ---------------------------------------------------------------- pages load
print("--- pages render ---")
for path in ["/", "/kitchen", "/delivery", "/reports", "/reports/daily",
             "/reports/best-sellers", "/reports/payments", "/reports/staff",
             "/reports/channels"]:
    client.cookies.set("staff_id", str(owner.id))
    r = client.get(path)
    check(r.status_code == 200, f"GET {path}", f"{r.status_code}, {len(r.text)}b")

# CSV export (4.3.3 / 6.3 step 5)
r = client.get("/reports/export.csv")
lines = r.text.strip().split("\n")
check(
    r.status_code == 200 and "text/csv" in r.headers.get("content-type", ""),
    "GET /reports/export.csv returns CSV",
    f"{len(lines)} rows",
)
check(
    lines[0].startswith("date,hour,order_code,payment_id,channel,item,instrument"),
    "CSV header is at allocation grain (item + instrument per row)",
)

# ---------------------------------------------------- section 3: access control
print("\n--- role-based access control (section 3) ---")
client.cookies.set("staff_id", str(kitchen.id))
r = client.get("/reports")
check(r.status_code == 403, "kitchen staff blocked from reports", f"{r.status_code}")

r = client.get("/kitchen")
check(r.status_code == 200, "kitchen staff can see the kitchen display")

# Kitchen status filter (4.1.3): each status shows only its own tickets, and
# the channel view is preserved alongside it.
client.cookies.set("staff_id", str(owner.id))
all_k = client.get("/kitchen?view=all&kstatus=all")
n_all = all_k.text.count('class="ticket ')
for st in ("pending", "preparing", "ready"):
    rk = client.get(f"/kitchen?view=all&kstatus={st}")
    n = rk.text.count('class="ticket ')
    check(rk.status_code == 200 and n <= n_all,
          f"kitchen status filter '{st}' shows a subset", f"{n} of {n_all}")
    # Every visible ticket's action button matches the selected status.
    if st == "pending":
        wrong = "Mark ready" in rk.text and n > 0 and "Start preparing" not in rk.text
        check(not wrong, "pending view shows 'Start preparing' tickets")
sub = sum(
    client.get(f"/kitchen?view=all&kstatus={s}").text.count('class="ticket ')
    for s in ("pending", "preparing", "ready")
)
check(sub == n_all, "the three statuses partition the full list", f"{sub} vs {n_all}")

rk = client.get("/kitchen?view=all&kstatus=bogus")
check(rk.status_code == 200 and rk.text.count('class="ticket ') == n_all,
      "an unknown status falls back to all")

# Kitchen chit fields (ref: printed ticket) — server, time in, total items.
kd = client.get("/kitchen?view=all&kstatus=all")
check("Server:" in kd.text, "ticket shows the server who fired the order")
check("Sent " in kd.text, "ticket shows the clock time it went to the kitchen")
check("Total items:" in kd.text, "ticket shows the total item count")
# The total is a sum of quantities. Cross-check one order against its items.
sample = db.execute(
    select(Order).where(
        Order.kitchen_status.in_(("pending", "preparing", "ready")),
        Order.status.not_in(("paid", "closed", "cancelled")),
    )
).scalars().first()
if sample is not None:
    want = sum(i.quantity for i in sample.items)
    check(f"Total items: {want}" in kd.text or want == 0,
          "the item total matches the order's line quantities", f"expected {want}")
client.cookies.set("staff_id", str(kitchen.id))

free_table = db.execute(
    select(RestaurantTable).where(RestaurantTable.status == TableStatus.FREE)
).scalars().first()
r = client.post(
    f"/tables/{free_table.id}/open",
    data={"guests": 3, "waiter_id": waiter.id},
)
check(r.status_code == 403, "kitchen staff cannot open a table", f"{r.status_code}")

client.cookies.set("staff_id", str(owner.id))
r = client.get("/reports")
check(r.status_code == 200, "owner can see reports")

# ------------------------------------------- workflow 6.1: dine-in end to end
print("\n--- dine-in workflow (section 6.1) ---")
client.cookies.set("staff_id", str(waiter.id))

table = db.execute(
    select(RestaurantTable).where(RestaurantTable.status == TableStatus.FREE)
).scalars().first()
table_id, table_no = table.id, table.number

# Step 1-2: seat guests, order created.
r = client.post(f"/tables/{table_id}/open", data={"guests": 3, "waiter_id": waiter.id})
check(r.status_code == 200, "step 1-2: host seats 3 guests, order created")
m = re.search(r"/orders/(\d+)", str(r.url))
order_id = int(m.group(1)) if m else None
check(order_id is not None, "redirected to the new order screen", f"order {order_id}")

db.expire_all()
table = db.get(RestaurantTable, table_id)
check(table.status == TableStatus.OCCUPIED, "system response: table -> Occupied")

order = db.get(Order, order_id)
check(len(order.seats) == 3, "a seat was created per guest (4.2.4)", f"{len(order.seats)} seats")

# Step 3: add items, one per seat + a shared starter.
from app.models.oltp import MenuItem  # noqa: E402

steak = db.execute(select(MenuItem).where(MenuItem.name == "Ribeye Steak")).scalar_one()
salmon = db.execute(select(MenuItem).where(MenuItem.name == "Grilled Salmon")).scalar_one()
burger = db.execute(select(MenuItem).where(MenuItem.name == "Beef Burger")).scalar_one()
bread = db.execute(select(MenuItem).where(MenuItem.name == "Garlic Bread")).scalar_one()

client.post(f"/orders/{order_id}/items", data={"menu_item_id": steak.id, "seat_number": 1, "quantity": 1, "notes": "Medium rare"})
client.post(f"/orders/{order_id}/items", data={"menu_item_id": salmon.id, "seat_number": 2, "quantity": 1})
client.post(f"/orders/{order_id}/items", data={"menu_item_id": burger.id, "seat_number": 3, "quantity": 1})
client.post(f"/orders/{order_id}/items", data={"menu_item_id": bread.id, "seat_number": 0, "quantity": 1})

db.expire_all()
order = db.get(Order, order_id)
check(len(order.items) == 4, "step 3: 4 items added", f"{len(order.items)} items")
expected = steak.price_cents + salmon.price_cents + burger.price_cents + bread.price_cents
actual = sum(i.line_total_cents for i in order.items)
check(actual == expected, "running total is correct", f"${money(actual)}")

# Step 4: send to kitchen.
r = client.post(f"/orders/{order_id}/send")
db.expire_all()
order = db.get(Order, order_id)
check(order.kitchen_status == "preparing", "step 4: sent to kitchen, status = Preparing")

# The ticket must appear on the kitchen display.
client.cookies.set("staff_id", str(kitchen.id))
r = client.get("/kitchen")
check(f"Table {table_no}" in r.text, "step 4: ticket appears on the kitchen display")

# 4.1.1 — a waiter can flag Ready to pay manually, before the kitchen is done.
client.cookies.set("staff_id", str(waiter.id))
r = client.post(f"/orders/{order_id}/ready-to-pay", data={"ready": 1})
db.expire_all()
check(r.status_code == 200 and db.get(RestaurantTable, table_id).status == TableStatus.READY_TO_PAY,
      "waiter marks the table Ready to pay by hand")
# It must not disturb the order's own kitchen/payment state.
check(db.get(Order, order_id).status != "ready",
      "the manual flag moves only the table, not the order status")
# And it toggles back.
r = client.post(f"/orders/{order_id}/ready-to-pay", data={"ready": 0})
db.expire_all()
check(db.get(RestaurantTable, table_id).status == TableStatus.OCCUPIED,
      "clearing it returns the table to Occupied")
client.cookies.set("staff_id", str(kitchen.id))   # step 5 acts as the kitchen

# Step 5: kitchen marks ready -> table becomes Ready to Pay.
r = client.post(f"/kitchen/{order_id}/status", data={"status": "ready"})
db.expire_all()
order = db.get(Order, order_id)
table = db.get(RestaurantTable, table_id)
check(order.kitchen_status == "ready", "step 5: kitchen marks order Ready")
check(table.status == TableStatus.READY_TO_PAY, "step 5: table -> Ready to Pay")

# Step 6: payment screen.
client.cookies.set("staff_id", str(waiter.id))
r = client.get(f"/orders/{order_id}/pay")
check(r.status_code == 200, "step 6: payment screen opens")
check("Unassigned items" in r.text, "unassigned shared item is surfaced, not silently dropped")

# Share the garlic bread across all 3 seats (4.2.4).
db.expire_all()
order = db.get(Order, order_id)
shared_item = next(i for i in order.items if i.menu_item_id == bread.id)
r = client.post(
    f"/orders/{order_id}/items/{shared_item.id}/share",
    data={"seat_numbers": [1, 2, 3]},
)
db.expire_all()
order = db.get(Order, order_id)
shared_item = next(i for i in order.items if i.menu_item_id == bread.id)
shares = sum(s.share_cents for s in shared_item.shares)
check(len(shared_item.shares) == 3, "4.2.4: shared item split across 3 seats")
check(shares == shared_item.line_total_cents,
      "shared split sums to the item exactly (no lost cent)",
      f"{money(shares)} == {money(shared_item.line_total_cents)}")

# Step 7-9: each seat pays with its own instrument (4.2.4).
visa = db.execute(select(PaymentInstrument).where(PaymentInstrument.code == "visa")).scalar_one()
cash = db.execute(select(PaymentInstrument).where(PaymentInstrument.code == "cash")).scalar_one()
etransfer = db.execute(select(PaymentInstrument).where(PaymentInstrument.code == "etransfer")).scalar_one()
ubereats = db.execute(select(PaymentInstrument).where(PaymentInstrument.code == "ubereats")).scalar_one()

# 4.2.1 — UberEats must be rejected on a dine-in order.
seat1 = db.execute(select(Seat).where(Seat.order_id == order_id, Seat.seat_number == 1)).scalar_one()
r = client.post(
    f"/orders/{order_id}/seats/{seat1.id}/pay",
    data={"instrument_id": ubereats.id, "tip_mode": "none"},
)
check(r.status_code == 400, "4.2.1: UberEats rejected on a dine-in order", f"{r.status_code}")

# Seat 1 pays by Visa with an 18% tip.
db.expire_all()
order = db.get(Order, order_id)
ledgers, _ = build_ledgers(db, order)
seat1_owed = ledgers[seat1.id].outstanding_cents
r = client.post(
    f"/orders/{order_id}/seats/{seat1.id}/pay",
    data={"instrument_id": visa.id, "tip_mode": "18", "card_last4": "4242"},
)
check(r.status_code == 200, "step 8-9: seat 1 pays by Visa + 18% tip")

db.expire_all()
order = db.get(Order, order_id)
p1 = order.payments[-1]
check(p1.tip_cents == round(seat1_owed * 18 / 100 + 0.5) or abs(p1.tip_cents - seat1_owed * 0.18) < 1,
      "18% tip computed on the seat's items", f"${money(p1.tip_cents)} on ${money(seat1_owed)}")
check(p1.card_last4 == "4242" and p1.card_brand == "Visa",
      "card brand + last 4 stored, never a raw number (section 5)")
check(len(p1.allocations) >= 2,
      "4.2.2: payment allocates to each item it covers", f"{len(p1.allocations)} allocations")

# Sales tax (section 4.2): GST + PST on the item subtotal; total is items + tax + tip.
from app.services import settings as settings_svc  # noqa: E402
from app.services.money import pct  # noqa: E402

_cfg = settings_svc.tax_config(db)
_base = p1.items_cents - p1.discount_cents
check(p1.tax_cents == pct(_base, _cfg.gst_rate) + pct(_base, _cfg.pst_rate),
      f"GST {_cfg.gst_rate}% + PST {_cfg.pst_rate}% charged on the item subtotal",
      f"${money(p1.tax_cents)} on ${money(p1.items_cents)}")
check(p1.total_cents == p1.items_cents - p1.discount_cents + p1.tax_cents + p1.tip_cents,
      "total = items - discount + tax + tip",
      f"${money(p1.total_cents)}")
check(p1.tax_cents > 0, "the tax line is non-zero for a real charge")

# Allocations still cover only item value — tax rides on the payment, not the
# allocation, so the reconciliation invariant is untouched.
check(sum(a.amount_cents for a in p1.allocations) == p1.items_cents,
      "allocations sum to item value, with no tax mixed in")

# The receipt shows GST, the item count, and the tip guide (ref: printed chit).
receipt_row = db.execute(
    select(Receipt).where(Receipt.payment_id == p1.id)
).scalars().first()
rr = client.get(f"/payments/{p1.id}/receipt")
check(rr.status_code == 200, "receipt renders for the payment")
for label in (f"GST ({_cfg.gst_rate}%)", "Total number of items:", "Please pay your server",
              "Tip guide", "GST#"):
    check(label in rr.text, f"receipt shows '{label}'")

# Seat 2 pays cash — different instrument, same order (4.2.2).
seat2 = db.execute(select(Seat).where(Seat.order_id == order_id, Seat.seat_number == 2)).scalar_one()
r = client.post(f"/orders/{order_id}/seats/{seat2.id}/pay", data={"instrument_id": cash.id, "tip_mode": "none"})
check(r.status_code == 200, "seat 2 pays Cash — a second instrument on the same order")

# Live balance panel must reflect partial collection (4.2.4).
db.expire_all()
order = db.get(Order, order_id)
panel = balance_panel(db, order)
check(panel.seats_paid == 2 and panel.seats_remaining == 1,
      "4.2.4: live balance shows 2 seats paid, 1 remaining")
check(panel.owed_cents > 0, "4.2.4: balance still shows an amount owed", f"${money(panel.owed_cents)}")
check(order.status == "partially_paid", "order stays open while a seat is unpaid", order.status)
check(db.get(RestaurantTable, table_id).status == TableStatus.READY_TO_PAY,
      "table is NOT freed while money is outstanding")

# --- void a payment (manager action, kept as a record) --------------------
print("\n--- void a payment ---")
db.expire_all()
order = db.get(Order, order_id)
p2 = next(p for p in order.payments if p.seat and p.seat.seat_number == 2 and not p.voided)

# A waiter cannot void.
client.cookies.set("staff_id", str(waiter.id))
r = client.post(f"/payments/{p2.id}/void", data={"reason": "test"})
check(r.status_code == 403, "a waiter cannot void a payment", str(r.status_code))

# A manager can. Voiding reopens seat 2's items.
client.cookies.set("staff_id", str(owner.id))
before_collected = balance_panel(db, order).collected_cents
r = client.post(f"/payments/{p2.id}/void", data={"reason": "charged the wrong seat"})
check(r.status_code == 200, "a manager voids the payment")
db.expire_all()
order = db.get(Order, order_id)
p2 = db.get(type(p2), p2.id)
check(p2.voided and p2.voided_by_id == owner.id, "the payment is flagged voided, not deleted")
check(len(p2.allocations) == 0, "its allocations are removed so the items reopen")
panel = balance_panel(db, order)
check(panel.collected_cents == before_collected - p2.total_cents,
      "collected drops by exactly the voided amount",
      f"{money(before_collected)} -> {money(panel.collected_cents)}")
check(panel.seats_paid == 1, "seat 2 is no longer counted as paid")
check(db.get(RestaurantTable, table_id).status == TableStatus.READY_TO_PAY,
      "the table is not freed by a void that leaves money owing")

# Re-pay seat 2 so the flow can complete.
seat2b = db.execute(select(Seat).where(Seat.order_id == order_id, Seat.seat_number == 2)).scalar_one()
r = client.post(f"/orders/{order_id}/seats/{seat2b.id}/pay", data={"instrument_id": cash.id, "tip_mode": "none"})
check(r.status_code == 200, "seat 2 can be re-paid after the void")
client.cookies.set("staff_id", str(waiter.id))

# Seat 3 settles by e-transfer -> order closes, table frees (step 10).
seat3 = db.execute(select(Seat).where(Seat.order_id == order_id, Seat.seat_number == 3)).scalar_one()
r = client.post(f"/orders/{order_id}/seats/{seat3.id}/pay", data={"instrument_id": etransfer.id, "tip_mode": "20"})
check(r.status_code == 200, "seat 3 settles by E-transfer")

db.expire_all()
order = db.get(Order, order_id)
panel = balance_panel(db, order)
check(panel.owed_cents == 0, "all item value collected", f"owed ${money(panel.owed_cents)}")
check(order.status == "paid", "step 10: order marked paid", order.status)
check(db.get(RestaurantTable, table_id).status == TableStatus.FREE,
      "step 10: table returns to Free on the floor plan")

# Three instruments on one order — the 4.2.2 headline requirement.
instruments_used = {p.instrument.name for p in order.payments}
check(len(instruments_used) == 3,
      "4.2.2: one order settled across 3 different instruments",
      ", ".join(sorted(instruments_used)))

# Every item traces to the instrument that paid for it.
traced = []
for item in order.items:
    names = {a.payment.instrument.name for a in item.allocations}
    paid = sum(a.amount_cents for a in item.allocations)
    traced.append((item.menu_item.name, sorted(names), paid == item.line_total_cents))
check(all(t[2] for t in traced), "4.2.2: every item fully traced to its instrument(s)")
for name, names, _ in traced:
    print(f"        {name:22} paid by {', '.join(names)}")

# --- cancel an order ------------------------------------------------------
print("\n--- cancel an order ---")
client.cookies.set("staff_id", str(owner.id))
# The just-settled order has payments -> cancel is blocked.
r = client.post(f"/orders/{order_id}/cancel", data={"reason": "test"})
check(r.status_code == 400, "a settled order cannot be cancelled", str(r.status_code))

# Open a fresh order on a free table and cancel it (no payments).
free_t = db.execute(
    select(RestaurantTable).where(RestaurantTable.status == TableStatus.FREE)
).scalars().first()
client.cookies.set("staff_id", str(waiter.id))
r = client.post(f"/tables/{free_t.id}/open", data={"guests": 2, "waiter_id": waiter.id})
db.expire_all()
fresh_order = db.execute(
    select(Order).where(Order.table_id == free_t.id, Order.status.notin_(("paid", "closed", "cancelled")))
).scalars().first()
# A waiter cannot cancel.
r = client.post(f"/orders/{fresh_order.id}/cancel", data={"reason": "mistake"})
check(r.status_code == 403, "a waiter cannot cancel an order", str(r.status_code))
# A manager can, and the table frees.
client.cookies.set("staff_id", str(owner.id))
r = client.post(f"/orders/{fresh_order.id}/cancel", data={"reason": "guest left"})
check(r.status_code == 200, "a manager cancels the unpaid order")
db.expire_all()
check(db.get(Order, fresh_order.id).status == "cancelled", "order is marked cancelled")
check(db.get(RestaurantTable, free_t.id).status == TableStatus.FREE,
      "cancelling frees the table")

# Receipts (4.2.7).
r = client.get(f"/payments/{order.payments[0].id}/receipt")
check(r.status_code == 200 and "TOTAL" in r.text, "4.2.7: receipt renders per payment")

# ------------------------------------------------ 4.2.5 partial order close
print("\n--- partial order close (section 4.2.5) ---")
table2 = db.execute(
    select(RestaurantTable).where(RestaurantTable.status == TableStatus.FREE)
).scalars().first()
r = client.post(f"/tables/{table2.id}/open", data={"guests": 2, "waiter_id": waiter.id})
order2_id = int(re.search(r"/orders/(\d+)", str(r.url)).group(1))

# Seat 1 orders two items; only one is paid before they leave.
client.post(f"/orders/{order2_id}/items", data={"menu_item_id": steak.id, "seat_number": 1})
client.post(f"/orders/{order2_id}/items", data={"menu_item_id": bread.id, "seat_number": 1})
client.post(f"/orders/{order2_id}/items", data={"menu_item_id": burger.id, "seat_number": 2})

db.expire_all()
order2 = db.get(Order, order2_id)
seat_a = db.execute(select(Seat).where(Seat.order_id == order2_id, Seat.seat_number == 1)).scalar_one()
steak_item = next(i for i in order2.items if i.menu_item_id == steak.id)
bread_item = next(i for i in order2.items if i.menu_item_id == bread.id and i.seat_id == seat_a.id)

# Guest ticks only the steak and departs early.
r = client.post(
    f"/orders/{order2_id}/seats/{seat_a.id}/pay",
    data={"instrument_id": visa.id, "tip_mode": "15", "partial": "1", "item_ids": steak_item.id},
)
check(r.status_code == 200, "departing guest pays only their ticked item")

db.expire_all()
order2 = db.get(Order, order2_id)
seat_a = db.get(Seat, seat_a.id)
p = order2.payments[-1]
check(p.is_partial_close is True, "4.2.5: payment flagged as a partial close")
check(p.items_cents == steak_item.line_total_cents,
      "4.2.5: only the ticked item was charged", f"${money(p.items_cents)}")
check(p.tip_cents == round(steak_item.line_total_cents * 15 / 100 + 0.5),
      "4.2.5: tip is proportional to the items actually paid", f"${money(p.tip_cents)}")
check(seat_a.status == "paid_partial", 'seat shows "Paid (partial)" in the table view', seat_a.status)

unticked_paid = sum(a.amount_cents for a in bread_item.allocations)
check(unticked_paid == 0, "4.2.5: the unticked item remains unpaid on the open order")
check(order2.status == "partially_paid", "4.2.5: rest of the table stays open and active", order2.status)
check(db.get(RestaurantTable, table2.id).status != TableStatus.FREE,
      "4.2.5: table is not released while other seats are still eating")

# Traceability: the partial close is linked to the same order (4.2.5).
check(p.order_id == order2_id, "4.2.5: partial close linked to the same table order ID")

r = client.get(f"/payments/{p.id}/receipt")
check(r.status_code == 200 and "PARTIAL CLOSE" in r.text,
      "4.2.5: receipt issued immediately, marked as a partial close")

# ------------------------------------------------ ETL picks up the new orders
print("\n--- reports reflect live trading ---")
from app.etl import run_etl  # noqa: E402
from app.models.star import FactOrderHeader  # noqa: E402

before = db.execute(select(FactOrderHeader).where(FactOrderHeader.order_id == order_id)).scalar_one_or_none()
check(before is None, "the just-closed order is not in the facts before ETL runs")

client.cookies.set("staff_id", str(owner.id))
r = client.post("/reports/refresh")
check(r.status_code == 200, "reports refresh (ETL) runs from the UI")

db.expire_all()
after = db.execute(select(FactOrderHeader).where(FactOrderHeader.order_id == order_id)).scalar_one_or_none()
check(after is not None, "the closed order now appears in fact_order_header")
if after:
    check(after.distinct_instruments == 3,
          "fact records that the order used 3 instruments", str(after.distinct_instruments))

db.close()
client.close()
print("\nRESULT:", "end-to-end workflows behave to spec" if ok else "E2E FAILURES")
sys.exit(0 if ok else 1)
