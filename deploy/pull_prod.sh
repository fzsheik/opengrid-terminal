#!/usr/bin/env bash
# Copy the production database (all recorded history) from Railway into the local database.
# The reverse of restore.sh. Production is only read; the local database is replaced.
#
#   deploy/pull_prod.sh                        # over `railway ssh` (needs an SSH key added with `railway ssh keys`)
#   deploy/pull_prod.sh "<DATABASE_PUBLIC_URL>" # or straight from the Postgres TCP proxy URL
#
# Stop the local server first: the restore refuses to run while anything else is connected.
# Local Postgres is the `opengrid-pg` Docker container (override with PG_CONTAINER).
set -euo pipefail
CONTAINER="${PG_CONTAINER:-opengrid-pg}"
DIR="$(dirname "$0")"
DUMP="$DIR/prod.dump"
BACKUP="$DIR/local-backup.dump"
lpsql() { docker exec -i "$CONTAINER" psql -U postgres -d opengrid -v ON_ERROR_STOP=1 -Atc "$1"; }
COUNTS="select (select count(*) from raw_snapshots)||' raw, '||(select count(*) from listing_observations)||' observations, '||(select count(*) from compute_listings)||' listings, since '||coalesce((select min(observed_at)::text from listing_observations), '-')"

others="$(lpsql "select count(*) from pg_stat_activity where datname = 'opengrid' and pid <> pg_backend_pid()")"
if [ "$others" != "0" ]; then
  echo "local database has $others other connection(s); stop the local server (uvicorn) and run again" >&2
  exit 1
fi

echo "dumping production ..."
if [ $# -ge 1 ]; then
  docker exec -i "$CONTAINER" pg_dump -Fc --no-owner --no-acl "$1" > "$DUMP"
else
  # base64 so the binary dump survives the ssh channel
  railway ssh --service Postgres -- sh -c '"pg_dump -Fc --no-owner --no-acl -U \$PGUSER \$PGDATABASE | base64"' < /dev/null \
    | tr -d '\r' | grep -E '^[A-Za-z0-9+/=]+$' | base64 -d > "$DUMP"
fi
docker exec -i "$CONTAINER" pg_restore --list < "$DUMP" > /dev/null || { echo "dump is not readable; nothing restored" >&2; exit 1; }
ls -lh "$DUMP" | awk '{print "dump size:", $5}'

echo "backing up local to $BACKUP ..."
docker exec -i "$CONTAINER" pg_dump -U postgres -Fc --no-owner --no-acl opengrid > "$BACKUP"

echo "restoring into local ..."
docker exec -i "$CONTAINER" pg_restore -U postgres -d opengrid --no-owner --no-acl --clean --if-exists < "$DUMP" \
  || echo "(pg_restore printed warnings; check the counts below)"
echo "local now: $(lpsql "$COUNTS")"
echo "undo with: docker exec -i $CONTAINER pg_restore -U postgres -d opengrid --clean --if-exists < $BACKUP"
