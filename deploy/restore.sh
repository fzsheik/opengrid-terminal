#!/usr/bin/env bash
# Copy the local database (all recorded history) into Railway's Postgres.
#
#   deploy/restore.sh "<Postgres service > Variables > DATABASE_PUBLIC_URL>"
#
# Run it BEFORE the app service first starts, or --clean replaces whatever the app created.
set -euo pipefail
if [ $# -ge 1 ]; then
  URL="$1"
else
  # No URL given: ask for the five values from the Railway Postgres service > Variables and build it.
  # The password is typed hidden and URL-encoded, so special characters cannot break the address.
  echo "From the Postgres service in Railway (Variables tab). The host and port are the PUBLIC ones:"
  read -r -p "RAILWAY_TCP_PROXY_DOMAIN (like something.proxy.rlwy.net): " H
  read -r -p "RAILWAY_TCP_PROXY_PORT   (a number like 41234, NOT 5432): " P
  read -r -p "PGUSER     [postgres]: " U; U="${U:-postgres}"
  read -r -p "PGDATABASE [railway]:  " D; D="${D:-railway}"
  read -r -s -p "PGPASSWORD (hidden): " W; echo
  if [ "$P" = "5432" ]; then
    echo "Port 5432 is Postgres's private port inside Railway. The public port is the number before the arrow"
    echo "in Postgres > Settings > Networking (TCP Proxy), also in RAILWAY_TCP_PROXY_PORT."; exit 1
  fi
  URL="$(H="$H" P="$P" U="$U" D="$D" W="$W" python3 -c 'import os; from urllib.parse import quote as q; e=os.environ; print("postgresql://%s:%s@%s:%s/%s" % (q(e["U"],safe=""), q(e["W"],safe=""), e["H"].strip(), e["P"].strip(), q(e["D"],safe="")))')"
fi
if [ -n "${DRY_RUN:-}" ]; then echo "url would be: $(echo "$URL" | sed -E 's#(://[^:]*:)[^@]*@#\1****@#')"; exit 0; fi
case "$URL" in
  *railway.internal*) echo "That is Railway's INTERNAL address; it only works inside Railway, not from your laptop."
                      echo "Use DATABASE_PUBLIC_URL instead (Postgres service > Variables). Its host ends in .proxy.rlwy.net"; exit 1 ;;
esac
DUMP="$(dirname "$0")/opengrid.dump"
echo "dumping local database 'opengrid' ..."
pg_dump -Fc --no-owner --no-acl opengrid > "$DUMP"
ls -lh "$DUMP" | awk '{print "dump size:", $5}'
echo "restoring into Railway ..."
pg_restore --no-owner --no-acl --clean --if-exists -d "$URL" "$DUMP" || echo "(pg_restore printed warnings; check the counts below)"
psql "$URL" -Atc "select 'on Railway: '||(select count(*) from raw_snapshots)||' raw, '||(select count(*) from listing_observations)||' observations, '||(select count(*) from compute_listings)||' listings'"
psql opengrid -Atc "select 'local:      '||(select count(*) from raw_snapshots)||' raw, '||(select count(*) from listing_observations)||' observations, '||(select count(*) from compute_listings)||' listings'"
