"""Initialise a fresh production database.

Creates the schema and the reference data a restaurant needs to start trading —
the menu, tables, floor and zones, sales channels, payment instruments — plus a
single owner account. Unlike the development seeder (``app.seed``) it generates
NO fake trading history and NO demo accounts with published PINs: the owner's
PIN comes from the ``OWNER_PIN`` environment variable, so a live system has no
default credentials.

Idempotent: if any staff already exist it assumes the restaurant is configured
and does nothing, so it is safe to run on every deploy.

    OWNER_PIN=1234 python -m app.bootstrap
"""
import os

from sqlalchemy import func, select

from app import migrate
from app.database import Base, SessionLocal, engine
from app.models.oltp import Role, Staff
from app.seed import seed_reference


def main() -> None:
    # create_all builds every table on a fresh database; migrate is a no-op on
    # Postgres (it patches legacy SQLite schemas only).
    Base.metadata.create_all(engine)
    migrate.run(engine)

    db = SessionLocal()
    try:
        if db.execute(select(func.count()).select_from(Staff)).scalar():
            print("Database already initialised (staff exist) — nothing to do.")
            return

        pin = os.environ.get("OWNER_PIN", "").strip()
        if not (pin.isdigit() and 4 <= len(pin) <= 8):
            raise SystemExit(
                "Refusing to create an owner without a PIN. "
                "Set OWNER_PIN to 4–8 digits, e.g. OWNER_PIN=1234 python -m app.bootstrap"
            )

        seed_reference(db, staff_list=[("Owner", Role.OWNER, pin)])
        db.commit()
        print(
            "Initialised: menu, tables, floor & zones, channels, payment "
            "instruments, and one owner account.\n"
            "Sign in as 'Owner' with your OWNER_PIN, then add staff and adjust "
            "the floor under Manage."
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
