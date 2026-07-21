"""Reconcile the star schema against the OLTP source.

A dimensional model that does not tie back to the transactional data is worse
than no model at all, because it is confidently wrong. These checks assert
that no cent was invented, lost, or double counted on the way into the facts.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models.oltp import (
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentAllocation,
)
from app.models.star import (
    UNKNOWN_KEY,
    DimChannel,
    FactOrderHeader,
    FactOrderItem,
    FactPayment,
)
from app.services.money import money

db = SessionLocal()
ok = True


def check(cond, label, detail=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {label}{('  -> ' + detail) if detail else ''}")


CLOSED = (OrderStatus.PAID, OrderStatus.CLOSED)
closed_ids = [i for (i,) in db.execute(select(Order.id).where(Order.status.in_(CLOSED))).all()]

# Voided payments are kept for audit but carry no money into the facts, so the
# source money totals must exclude them to match. A closed order can legitimately
# hold a voided payment (voided, re-paid, then settled).
def live(*extra):
    return (Payment.order_id.in_(closed_ids), Payment.voided.is_(False), *extra)

# --- row counts -----------------------------------------------------------
n_orders = len(closed_ids)
n_header = db.execute(select(func.count()).select_from(FactOrderHeader)).scalar_one()
check(n_orders == n_header, "every closed order has exactly one header fact row",
      f"{n_orders} orders / {n_header} fact rows")

n_items_src = db.execute(
    select(func.count()).select_from(OrderItem).where(OrderItem.order_id.in_(closed_ids))
).scalar_one()
n_items_fact = db.execute(select(func.count()).select_from(FactOrderItem)).scalar_one()
check(n_items_src == n_items_fact, "every order item has exactly one item fact row",
      f"{n_items_src} items / {n_items_fact} fact rows")

n_alloc_src = db.execute(
    select(func.count()).select_from(PaymentAllocation)
    .join(Payment, Payment.id == PaymentAllocation.payment_id)
    .where(Payment.order_id.in_(closed_ids))
).scalar_one()
n_alloc_fact = db.execute(select(func.count()).select_from(FactPayment)).scalar_one()
check(n_alloc_src == n_alloc_fact, "fact_payment grain matches allocation count",
      f"{n_alloc_src} allocations / {n_alloc_fact} fact rows")

# --- money ----------------------------------------------------------------
src_total = db.execute(
    select(func.coalesce(func.sum(Payment.total_cents), 0)).where(*live())
).scalar_one()
fact_pay_total = db.execute(
    select(func.coalesce(func.sum(FactPayment.total_cents), 0))
).scalar_one()
check(src_total == fact_pay_total, "revenue: OLTP payments == fact_payment",
      f"{money(src_total)} vs {money(fact_pay_total)}")

hdr_total = db.execute(
    select(func.coalesce(func.sum(FactOrderHeader.total_cents), 0))
).scalar_one()
check(src_total == hdr_total, "revenue: OLTP payments == fact_order_header",
      f"{money(src_total)} vs {money(hdr_total)}")

src_tips = db.execute(
    select(func.coalesce(func.sum(Payment.tip_cents), 0)).where(*live())
).scalar_one()
fact_tips = db.execute(select(func.coalesce(func.sum(FactPayment.tip_cents), 0))).scalar_one()
check(src_tips == fact_tips, "tips survive proportional allocation to allocation grain",
      f"{money(src_tips)} vs {money(fact_tips)}")

src_disc = db.execute(
    select(func.coalesce(func.sum(Payment.discount_cents), 0)).where(*live())
).scalar_one()
fact_disc = db.execute(select(func.coalesce(func.sum(FactPayment.discount_cents), 0))).scalar_one()
check(src_disc == fact_disc, "discounts survive allocation to allocation grain",
      f"{money(src_disc)} vs {money(fact_disc)}")

src_tax = db.execute(
    select(func.coalesce(func.sum(Payment.tax_cents), 0)).where(*live())
).scalar_one()
fact_tax = db.execute(select(func.coalesce(func.sum(FactPayment.tax_cents), 0))).scalar_one()
check(src_tax == fact_tax, "GST survives proportional allocation to allocation grain",
      f"{money(src_tax)} vs {money(fact_tax)}")
hdr_tax = db.execute(select(func.coalesce(func.sum(FactOrderHeader.tax_cents), 0))).scalar_one()
check(src_tax == hdr_tax, "GST: OLTP payments == fact_order_header",
      f"{money(src_tax)} vs {money(hdr_tax)}")

# The whole equation must hold at payment level: total = items - disc + tax + tip.
bad_total = db.execute(
    select(func.count()).select_from(Payment).where(
        Payment.order_id.in_(closed_ids),
        Payment.total_cents
        != Payment.items_cents - Payment.discount_cents + Payment.tax_cents + Payment.tip_cents,
    )
).scalar_one()
check(bad_total == 0, "every payment total equals items - discount + tax + tip",
      f"{bad_total} rows break the identity")

# Item gross in facts must equal the source line totals.
src_gross = 0
for item in db.execute(select(OrderItem).where(OrderItem.order_id.in_(closed_ids))).scalars():
    src_gross += item.line_total_cents
fact_gross = db.execute(select(func.coalesce(func.sum(FactOrderItem.gross_cents), 0))).scalar_one()
check(src_gross == fact_gross, "item gross: OLTP == fact_order_item",
      f"{money(src_gross)} vs {money(fact_gross)}")

# Allocations must cover exactly the item value that was sold.
alloc_total = db.execute(
    select(func.coalesce(func.sum(PaymentAllocation.amount_cents), 0))
    .join(Payment, Payment.id == PaymentAllocation.payment_id)
    .where(Payment.order_id.in_(closed_ids))
).scalar_one()
check(alloc_total == src_gross,
      "every cent of sold item value is allocated to an instrument (req 4.2.2)",
      f"allocated {money(alloc_total)} of {money(src_gross)}")

# fact_payment amount must equal item value too.
fact_amount = db.execute(select(func.coalesce(func.sum(FactPayment.amount_cents), 0))).scalar_one()
check(fact_amount == src_gross, "fact_payment item value == item gross",
      f"{money(fact_amount)} vs {money(src_gross)}")

# --- referential integrity -----------------------------------------------
orphan_items = db.execute(
    select(func.count()).select_from(FactOrderItem).where(FactOrderItem.item_key == UNKNOWN_KEY)
).scalar_one()
check(orphan_items == 0, "no fact_order_item rows fell back to the Unknown item member",
      f"{orphan_items} rows")

bad_date = db.execute(
    select(func.count()).select_from(FactPayment).where(FactPayment.date_key == UNKNOWN_KEY)
).scalar_one()
check(bad_date == 0, "no fact_payment rows fell back to the Unknown date member")

# Delivery orders legitimately have no table/waiter -> Unknown is correct there.
delivery_key = db.execute(
    select(DimChannel.channel_key).where(DimChannel.channel_type == "delivery")
).scalars().all()
dine_unknown_table = db.execute(
    select(func.count()).select_from(FactOrderHeader).where(
        FactOrderHeader.table_key == UNKNOWN_KEY,
        FactOrderHeader.channel_key.not_in(delivery_key),
    )
).scalar_one()
check(dine_unknown_table == 0, "every dine-in order resolves to a real table dimension",
      f"{dine_unknown_table} rows")

# --- idempotency ----------------------------------------------------------
from app.etl import run_etl  # noqa: E402

before = db.execute(select(func.count()).select_from(FactPayment)).scalar_one()
run_etl(db, full_refresh=False, verbose=False)
after = db.execute(select(func.count()).select_from(FactPayment)).scalar_one()
check(before == after, "re-running the ETL loads nothing twice (idempotent)",
      f"{before} -> {after} rows")

print("\nRESULT:", "star schema reconciles to source" if ok else "RECONCILIATION FAILURES")
db.close()
sys.exit(0 if ok else 1)
