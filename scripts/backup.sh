#!/usr/bin/env sh
# Nightly Postgres backup. A dead disk should never cost the restaurant its
# service history — so this dumps the database and (you must finish the TODO)
# copies it OFF the box.
#
# Schedule on the host, e.g. cron at 03:00:
#   0 3 * * * cd /path/to/restaurant_app && sh scripts/backup.sh >> backups/backup.log 2>&1
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load POSTGRES_* from .env if present.
[ -f .env ] && . ./.env

DIR="$ROOT/backups"
mkdir -p "$DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="$DIR/restaurant-$STAMP.sql.gz"

docker compose exec -T db \
	pg_dump -U "${POSTGRES_USER:-rms}" "${POSTGRES_DB:-restaurant}" \
	| gzip > "$FILE"

echo "$(date -Iseconds) wrote $FILE ($(du -h "$FILE" | cut -f1))"

# Keep two weeks of local copies.
find "$DIR" -name 'restaurant-*.sql.gz' -mtime +14 -delete

# TODO — OFFSITE COPY (do not skip): a backup on the same box is not a backup.
# Copy "$FILE" to another machine or cloud, e.g.:
#   rclone copy "$FILE" remote:rms-backups/
#   scp "$FILE" user@nas:/backups/
