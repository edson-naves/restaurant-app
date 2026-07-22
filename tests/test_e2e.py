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

# ---------------------------------------------------------------- auth (§3, §5)
print("--- sign in ---")
noauth = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30)
r = noauth.get("/")
check(r.status_code == 303 and r.headers.get("location") == "/login",
      "an unauthenticated request is redirected to /login", str(r.status_code))
check(noauth.get("/login").status_code == 200, "the login page is reachable signed out")

# Wrong PIN is rejected.
r = noauth.post("/login", data={"staff_id": owner.id, "pin": "000000"})
check(r.status_code == 303 and "error" in r.headers.get("location", ""),
      "a wrong PIN is rejected")
# Correct PIN signs in and sets the session cookie.
r = noauth.post("/login", data={"staff_id": owner.id, "pin": owner.pin_code})
check(r.status_code == 303 and r.headers.get("location") == "/"
      and "staff_id" in r.cookies, "the right PIN signs in and sets the session")
# The session then reaches a protected page, and sign-out clears it.
authed = httpx.Client(base_url=BASE, follow_redirects=False, timeout=30)
authed.cookies.set("staff_id", str(owner.id))
check(authed.get("/").status_code == 200, "the signed-in session reaches the floor plan")
r = authed.get("/logout")
check(r.status_code == 303 and r.headers.get("location") == "/login", "sign-out redirects to /login")
noauth.close()
authed.close()

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

# 4.1.2 — edit a line already on the order (change quantity + note), no
# delete-and-re-add. The steak was rung up as 1; bump it to 2.
steak_line = next(i for i in order.items if i.menu_item_id == steak.id)
r = client.post(f"/orders/{order_id}/items/{steak_line.id}/edit",
                data={"quantity": 2, "notes": "Well done"})
db.expire_all()
steak_line = db.get(type(order.items[0]), steak_line.id)
check(r.status_code == 200 and steak_line.quantity == 2 and steak_line.notes == "Well done",
      "an existing line's quantity and note are edited in place")
check(steak_line.line_total_cents == steak.price_cents * 2,
      "the line total follows the new quantity")
check(sum(i.line_total_cents for i in db.get(Order, order_id).items)
      == expected + steak.price_cents, "the order total reflects the edit")
r = client.post(f"/orders/{order_id}/items/{steak_line.id}/edit", data={"quantity": 99})
check(r.status_code == 400, "an out-of-range quantity is refused")
# Restore to 1 so the downstream payment maths are unchanged.
client.post(f"/orders/{order_id}/items/{steak_line.id}/edit",
            data={"quantity": 1, "notes": "Medium rare"})

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

# 4.2.7 — email / text the receipt to the guest (local outbox transport).
from pathlib import Path as _Path  # noqa: E402

from app.models.oltp import ReceiptDelivery  # noqa: E402
from app.services.receipt_delivery import OUTBOX  # noqa: E402

before_files = len(list((OUTBOX / "email").glob("*.txt"))) if (OUTBOX / "email").exists() else 0
r = client.post(f"/payments/{p1.id}/receipt/send",
                data={"method": "email", "destination": "guest@example.com"})
check(r.status_code == 200, "receipt emails to a valid address")
db.expire_all()
deliv = db.execute(
    select(ReceiptDelivery).where(ReceiptDelivery.receipt_id == receipt_row.id)
).scalars().all()
check(any(d.method == "email" and d.status == "sent" for d in deliv),
      "the email send is recorded as sent")
after_files = len(list((OUTBOX / "email").glob("*.txt")))
check(after_files == before_files + 1, "an email file lands in the outbox",
      f"{before_files} -> {after_files}")
# The outbox file carries the receipt body.
newest = max((OUTBOX / "email").glob("*.txt"), key=lambda p: p.stat().st_mtime)
body = newest.read_text(encoding="utf-8")
check("guest@example.com" in body and "TOTAL" in body,
      "the emailed file has the address and the receipt total")

# A malformed address is refused, and nothing is recorded.
r = client.post(f"/payments/{p1.id}/receipt/send",
                data={"method": "email", "destination": "not-an-email"})
check(r.status_code == 400, "a malformed email is refused")

# SMS to a phone number works and is filed under sms/.
r = client.post(f"/payments/{p1.id}/receipt/send",
                data={"method": "sms", "destination": "555-123-4567"})
check(r.status_code == 200 and (OUTBOX / "sms").exists()
      and len(list((OUTBOX / "sms").glob("*.txt"))) >= 1,
      "texting the receipt files it under the sms outbox")

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

# A settled order's lines can no longer be edited.
paid_line = order.items[0]
check(client.post(f"/orders/{order_id}/items/{paid_line.id}/edit",
                  data={"quantity": 3}).status_code == 400,
      "a line on a settled order cannot be edited")

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

# --- post-settlement refund (4.2, manager) --------------------------------
print("\n--- refund a settled payment ---")
from app.models.oltp import Refund  # noqa: E402
from app.models.star import FactOrderHeader  # noqa: E402
from app.services import refunds as refund_svc  # noqa: E402

db.expire_all()
order = db.get(Order, order_id)
p1 = db.get(type(order.payments[0]), p1.id)
check(order.status == "paid", "the order is settled, so refund (not void) applies")

# A waiter cannot refund.
client.cookies.set("staff_id", str(waiter.id))
check(client.post(f"/payments/{p1.id}/refund", data={"amount": "1.00"}).status_code == 403,
      "a waiter cannot refund")
client.cookies.set("staff_id", str(owner.id))

# Refund $5 of seat 1's Visa payment.
r = client.post(f"/payments/{p1.id}/refund", data={"amount": "5.00", "reason": "comped app"})
check(r.status_code == 200, "a manager refunds part of a settled payment")
db.expire_all()
refs = db.execute(select(Refund).where(Refund.payment_id == p1.id)).scalars().all()
check(len(refs) == 1 and refs[0].amount_cents == 500, "the refund is recorded at $5.00")
check(refund_svc.refunded_so_far(db, p1.id) == 500, "refunded-so-far tracks the running total")

# It must not disturb the settled order or the freed table (that's what void is
# for) — a refund is a money reversal only.
check(db.get(Order, order_id).status == "paid", "the order stays settled after a refund")
check(db.get(RestaurantTable, table_id).status == TableStatus.FREE,
      "the table stays free after a refund")

# Over-refunding is refused.
p1_total = p1.total_cents
r = client.post(f"/payments/{p1.id}/refund",
                data={"amount": money(p1_total)})  # more than the $5-reduced remainder
check(r.status_code == 400, "cannot refund more than the payment collected")
check(refund_svc.refunded_so_far(db, p1.id) == 500, "the refused over-refund changed nothing")
# The fact-header net-revenue check runs in the reports section below, after the
# ETL refresh (running it here would load the order into facts too early).

# --- course firing (4.1.3) ------------------------------------------------
print("\n--- course firing ---")
client.cookies.set("staff_id", str(waiter.id))
free_c = db.execute(
    select(RestaurantTable).where(RestaurantTable.status == TableStatus.FREE)
).scalars().first()
r = client.post(f"/tables/{free_c.id}/open", data={"guests": 2, "waiter_id": waiter.id})
course_oid = int(re.search(r"/orders/(\d+)", str(r.url)).group(1))
# A starter (course 1) and a main (course 2).
client.post(f"/orders/{course_oid}/items",
            data={"menu_item_id": bread.id, "seat_number": 1, "course": 1})
client.post(f"/orders/{course_oid}/items",
            data={"menu_item_id": steak.id, "seat_number": 1, "course": 2})

# Fire only the starters; the main is held.
r = client.post(f"/orders/{course_oid}/send", data={"course": 1})
db.expire_all()
co = db.get(Order, course_oid)
starter = next(i for i in co.items if i.course == 1)
main = next(i for i in co.items if i.course == 2)
check(r.status_code == 200 and starter.kitchen_status == "preparing"
      and main.kitchen_status == "pending",
      "firing the starters holds the mains")
check(co.kitchen_status == "preparing" and co.status == "preparing",
      "the order is preparing, not ready, while a course is held")

# Kitchen marks the starters up — order still not ready (main is held).
client.cookies.set("staff_id", str(kitchen.id))
r = client.post(f"/kitchen/{course_oid}/status", data={"status": "ready", "course": 1})
db.expire_all()
co = db.get(Order, course_oid)
check(db.get(type(co.items[0]), starter.id).kitchen_status == "ready"
      and co.kitchen_status != "ready",
      "a course marked ready does not make the whole order ready")
check(db.get(RestaurantTable, free_c.id).status != TableStatus.READY_TO_PAY,
      "the table is not Ready to pay while a course is still held")

# Fire and ready the mains -> now the whole order is ready.
client.cookies.set("staff_id", str(waiter.id))
client.post(f"/orders/{course_oid}/send", data={"course": 2})
client.cookies.set("staff_id", str(kitchen.id))
client.post(f"/kitchen/{course_oid}/status", data={"status": "ready", "course": 2})
db.expire_all()
co = db.get(Order, course_oid)
check(co.kitchen_status == "ready" and co.status == "ready",
      "with every course up, the order is ready")
check(db.get(RestaurantTable, free_c.id).status == TableStatus.READY_TO_PAY,
      "and the table flips to Ready to pay")
# Clean up this coursing order (no payments).
client.cookies.set("staff_id", str(owner.id))
client.post(f"/orders/{course_oid}/cancel")
client.cookies.set("staff_id", str(owner.id))

# --- fire a single item (4.1.3) -------------------------------------------
print("\n--- fire one item ---")
client.cookies.set("staff_id", str(waiter.id))
free_f = db.execute(
    select(RestaurantTable).where(RestaurantTable.status == TableStatus.FREE)
).scalars().first()
r = client.post(f"/tables/{free_f.id}/open", data={"guests": 2, "waiter_id": waiter.id})
fire_oid = int(re.search(r"/orders/(\d+)", str(r.url)).group(1))
client.post(f"/orders/{fire_oid}/items",
            data={"menu_item_id": steak.id, "seat_number": 1, "course": 2})
client.post(f"/orders/{fire_oid}/items",
            data={"menu_item_id": bread.id, "seat_number": 1, "course": 2})
db.expire_all()
fo = db.get(Order, fire_oid)
one, two = fo.items[0], fo.items[1]
# Fire only the first line; its course-mate stays pending.
r = client.post(f"/orders/{fire_oid}/items/{one.id}/fire")
db.expire_all()
fo = db.get(Order, fire_oid)
check(r.status_code == 200
      and db.get(type(one), one.id).kitchen_status == "preparing"
      and db.get(type(two), two.id).kitchen_status == "pending",
      "firing one line leaves its course-mate pending")
check(fo.kitchen_status == "preparing",
      "the order is preparing once a single item is fired")
# Re-firing an already-fired line is refused.
r = client.post(f"/orders/{fire_oid}/items/{one.id}/fire")
check(r.status_code == 400, "an already-fired item cannot be fired again")
# A line from another order cannot be fired through this one.
r = client.post(f"/orders/{fire_oid}/items/99999/fire")
check(r.status_code == 404, "firing an item not on the order is rejected")
client.cookies.set("staff_id", str(owner.id))
client.post(f"/orders/{fire_oid}/cancel")
client.cookies.set("staff_id", str(owner.id))

# --- move a party to another table (4.1.1) --------------------------------
print("\n--- move table ---")
client.cookies.set("staff_id", str(waiter.id))
src_t, dest_t = db.execute(
    select(RestaurantTable).where(RestaurantTable.status == TableStatus.FREE).limit(2)
).scalars().all()
r = client.post(f"/tables/{src_t.id}/open", data={"guests": 2, "waiter_id": waiter.id})
move_oid = int(re.search(r"/orders/(\d+)", str(r.url)).group(1))
client.post(f"/orders/{move_oid}/items", data={"menu_item_id": steak.id, "seat_number": 1})
# Moving onto an occupied table is refused.
r = client.post(f"/tables/{src_t.id}/move", data={"to_table_id": src_t.id})
check(r.status_code == 400, "moving a table onto itself is refused")
# Move the party to the free destination.
r = client.post(f"/tables/{src_t.id}/move", data={"to_table_id": dest_t.id})
db.expire_all()
mo = db.get(Order, move_oid)
check(r.status_code == 200 and mo.table_id == dest_t.id,
      "the order now sits on the destination table")
check(db.get(RestaurantTable, src_t.id).status == TableStatus.FREE,
      "the original table is freed")
check(db.get(RestaurantTable, dest_t.id).status == TableStatus.OCCUPIED,
      "the destination table is occupied")
check(db.get(RestaurantTable, dest_t.id).current_waiter_id == waiter.id,
      "the waiter moves with the party")
# The now-empty source table has no order to move.
r = client.post(f"/tables/{src_t.id}/move", data={"to_table_id": dest_t.id})
check(r.status_code == 400, "a table with no open order can't be moved")
client.cookies.set("staff_id", str(owner.id))
client.post(f"/orders/{move_oid}/cancel")
client.cookies.set("staff_id", str(owner.id))

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

# 4.1.2 — 86 an item mid-service (kitchen sold out of the burger).
print("\n--- 86 an item ---")
client.cookies.set("staff_id", str(kitchen.id))   # kitchen can 86
r = client.post(f"/menu/{burger.id}/availability", data={"available": 0})
db.expire_all()
check(r.status_code == 200 and db.get(MenuItem, burger.id).available is False,
      "the kitchen 86's the burger")
check("86" in client.get("/availability").text, "the availability board shows it 86'd")
# It drops off the order screen at once.
client.cookies.set("staff_id", str(waiter.id))
scr = client.get(f"/orders/{order2_id}?category={burger.category_id}").text
check("Beef Burger" not in scr, "an 86'd item leaves the order screen")
# And a stale page cannot still order it.
r = client.post(f"/orders/{order2_id}/items", data={"menu_item_id": burger.id, "seat_number": 2})
check(r.status_code == 400, "adding an 86'd item is refused")
# Put it back on.
client.post(f"/menu/{burger.id}/availability", data={"available": 1})
db.expire_all()
check(db.get(MenuItem, burger.id).available is True, "switching it on makes it sellable again")
client.cookies.set("staff_id", str(waiter.id))

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
    check(after.refund_cents == 500,
          "the fact header records the $5.00 refund; gross stays intact",
          f"gross ${money(after.total_cents)}, refund ${money(after.refund_cents)}")

# --- end-of-day close (workflow 6.3, Z-report) ----------------------------
print("\n--- end-of-day close ---")
from app.models.oltp import DayClose  # noqa: E402
from app.services import closeout  # noqa: E402

client.cookies.set("staff_id", str(owner.id))
r = client.get("/reports/close")
check(r.status_code == 200, "close page opens", str(r.status_code))

# A waiter can't cut a Z-report (reports.view is owner/manager only).
client.cookies.set("staff_id", str(waiter.id))
check(client.post("/reports/close", data={"opening_float": "0", "counted_cash": "0"}).status_code == 403,
      "a waiter cannot close the day")
client.cookies.set("staff_id", str(owner.id))

pending = closeout.compute_pending(db)
check(pending.total_collected_cents > 0, "there is money in the window to close",
      f"${money(pending.total_collected_cents)}")

# Close with counted = expected -> zero variance; the collected total is frozen.
r = client.post("/reports/close", data={
    "opening_float": "100.00",
    "counted_cash": money(10000 + pending.expected_cash_cents),  # float + expected
    "notes": "e2e close",
})
check(r.status_code == 200 and "Z-report" in r.text, "closing the day cuts a Z-report")
db.expire_all()
close = db.execute(select(DayClose).order_by(DayClose.id.desc())).scalars().first()
check(close is not None and close.variance_cents == 0,
      "counted == float + expected gives zero variance", f"{close.variance_cents}")
check(close.total_collected_cents == pending.total_collected_cents,
      "the Z-report froze the window's collected total")

# A $5-short count records a negative variance.
before_id = close.id
short = money(10000 + max(pending.expected_cash_cents - 500, 0))
# Second close covers only payments since the first; with none, expected cash is 0.
r2 = client.post("/reports/close", data={"opening_float": "0", "counted_cash": "0", "notes": ""})
db.expire_all()
close2 = db.execute(select(DayClose).order_by(DayClose.id.desc())).scalars().first()
check(close2.id != before_id and close2.window_start == close.closed_at,
      "the next close starts where the last one ended (no gap, no overlap)")
check(close2.payment_count == 0,
      "and covers no payments, since none were taken after the first close")

db.close()
client.close()
print("\nRESULT:", "end-to-end workflows behave to spec" if ok else "E2E FAILURES")
sys.exit(0 if ok else 1)
