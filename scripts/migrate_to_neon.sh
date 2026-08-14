#!/usr/bin/env bash
# One-time migration of the restaurant Postgres from Render's free (expiring) database
# to Neon. Uses the official postgres Docker image, so nothing needs to be installed
# locally — only Docker. Dumps to a local file first (robust on Windows/PowerShell too;
# never pipe a binary dump through a PowerShell pipe).
#
#   SRC = Render "External Database URL"  (Render -> your Postgres 'restaurant' -> Connect)
#   DST = Neon connection string          (Neon -> Connection string, keep ?sslmode=require)
#
# Both are plain libpq URLs: postgres://... or postgresql://...  (NOT the +psycopg form —
# that prefix is only for the app's own DATABASE_URL, see NEON_MIGRATION.md).
#
# Usage (bash / Git Bash):
#   SRC='postgres://...render.com/restaurant' \
#   DST='postgresql://...neon.tech/restaurant?sslmode=require' \
#     bash scripts/migrate_to_neon.sh
set -euo pipefail

: "${SRC:?Set SRC to the Render External Database URL}"
: "${DST:?Set DST to the Neon connection string (with ?sslmode=require)}"
PGIMAGE="${PGIMAGE:-postgres:17-alpine}"   # if pg_restore complains about the version,
                                           # set PGIMAGE=postgres:16-alpine (match Neon's)
DUMP="restaurant_$(date +%Y%m%d_%H%M%S).dump"

echo "1/3  Dumping from Render  ->  ./$DUMP"
docker run --rm -v "$PWD:/backup" "$PGIMAGE" \
  pg_dump --no-owner --no-acl -Fc -f "/backup/$DUMP" "$SRC"

echo "2/3  Restoring into Neon"
docker run --rm -v "$PWD:/backup" "$PGIMAGE" \
  pg_restore --no-owner --no-acl --clean --if-exists -d "$DST" "/backup/$DUMP"

echo "3/3  Verifying (table list + a couple of counts on the target)"
docker run --rm "$PGIMAGE" psql "$DST" -c "\dt" || true
docker run --rm "$PGIMAGE" psql "$DST" -tc \
  "select 'staff='||count(*) from staff" || true

echo
echo "Done. Kept the dump at ./$DUMP — delete it AFTER the app works on Neon."
echo "Next: point the Render web service's DATABASE_URL at Neon (see NEON_MIGRATION.md)."
