#!/usr/bin/env bash
# Copy the local database (all recorded history) into Railway's Postgres.
#
#   deploy/restore.sh "<Postgres service > Variables > DATABASE_PUBLIC_URL>"
#
# Run it BEFORE the app service first starts, or --clean replaces whatever the app created.
set -euo pipefail
URL="${1:?usage: deploy/restore.sh <DATABASE_PUBLIC_URL from the Railway Postgres service>}"
DUMP="$(dirname "$0")/opengrid.dump"
echo "dumping local database 'opengrid' ..."
pg_dump -Fc --no-owner --no-acl opengrid > "$DUMP"
ls -lh "$DUMP" | awk '{print "dump size:", $5}'
echo "restoring into Railway ..."
pg_restore --no-owner --no-acl --clean --if-exists -d "$URL" "$DUMP" || echo "(pg_restore printed warnings; check the counts below)"
psql "$URL" -Atc "select 'on Railway: '||(select count(*) from raw_snapshots)||' raw, '||(select count(*) from listing_observations)||' observations, '||(select count(*) from compute_listings)||' listings'"
psql opengrid -Atc "select 'local:      '||(select count(*) from raw_snapshots)||' raw, '||(select count(*) from listing_observations)||' observations, '||(select count(*) from compute_listings)||' listings'"
