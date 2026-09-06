> **EVIDENCE — G5 PASS.** Release B gate G5: retained production dump restored
> into a disposable local PostgreSQL, candidate schema loaded, strict migration
> run, second run proven idempotent.
>
> Promoted verbatim from the loose working file `G5_RELEASE_B_EVIDENCE.md`
> (2026-09-02); body unchanged below this header. Status of record:
> `docs/03_CURRENT_WORK.md`.

# Release B — G5 production-dump restore evidence

Date: 2026-09-02 (America/Vancouver)

Candidate: `release/payment-security` at
`f6081196e6dfdb27c992a73c502dbb0911793799`.

Source archive: `rms_prod_20260826_125948.dump`, a custom-format dump created
with PostgreSQL 18.6 from the production Neon database. The archive was restored
with PostgreSQL 18 into the disposable local database `g5_release_b_restore`.
Production was read neither written during this gate; the existing local archive
was used.

## Before

- `payment_instrument`: 9 rows.
- Instruments: visa, mastercard, amex, cash, contactless, etransfer, ubereats,
  doordash, card_terminal.
- `payment_attempt`: absent.
- `refund_attempt`: absent.
- `payment`: 10 rows.
- `refund`: 0 rows.

## Migration

The candidate schema was loaded, then `migrate.run(engine, strict=True)` ran.
Reported changes:

```text
menu_item.station_id
order_item.station_id
modifier.station_id
modifier_option.station_id
payment_instrument.provider
backfilled up to 33 preparation_task(s) from fired order items
backfilled payment_instrument card_terminal->square_terminal (1)
```

The non-Payment entries are older Kitchen migrations because the retained dump
predates those deployed schema changes. They are not Release B scope.

## After

- `payment_instrument`: 9 rows, unchanged.
- Providers: 8 `manual`, 1 `square_terminal`.
- `card_terminal`: exactly one row, provider `square_terminal`.
- `payment_attempt`: present, 0 rows.
- `refund_attempt`: present, 0 rows.
- `payment`: 10 rows, unchanged.
- `refund`: 0 rows, unchanged.

The second strict migration run returned `[]`, proving idempotency on the same
restored database.

## Procedural correction

The first invocation omitted `import app.models.oltp` before
`Base.metadata.create_all(engine)`. That does not reproduce application boot:
the metadata was empty, and strict migration stopped on the older
`menu_item.station_id` foreign key because `station` had not been created. The
disposable database was dropped and restored anew before the valid run above.
No result from the invalid invocation is used as gate evidence.

## Result

G5 PASS. The disposable database and container-side archive copy were removed
after verification. No repository file, branch, commit, remote, Render setting,
or production data was changed.
