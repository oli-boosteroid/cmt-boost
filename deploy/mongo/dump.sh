#!/usr/bin/env bash
# Dump cmt-mongo to /var/backups/cmt-mongo, keeping the last 3 archives.
# Usage: sudo ~/cmt-boost/deploy/mongo/dump.sh
# Restore: sudo docker exec -i -e P=<admin password> cmt-mongo sh -c \
#   'mongorestore -u admin -p "$P" --authenticationDatabase admin --archive --gzip --drop' < <archive>
set -euo pipefail
cd "$(dirname "$0")"

DIR="${CMT_DUMP_DIR:-/var/backups/cmt-mongo}"
KEEP="${CMT_DUMP_KEEP:-3}"
if [ "$(id -u)" = 0 ]; then DOCKER="${CMT_DOCKER:-docker}"; else DOCKER="${CMT_DOCKER:-sudo docker}"; fi

[ -f mongo.env ] || { echo "$(date -u) no mongo.env here: run init.sh first"; exit 2; }
set -a; . ./mongo.env; set +a
[ -n "${MONGO_BACKUP_PASSWORD:-}" ] || { echo "$(date -u) MONGO_BACKUP_PASSWORD missing in mongo.env"; exit 2; }

mkdir -p "$DIR"; chmod 700 "$DIR"
out="$DIR/cmt-mongo-$(date -u +%Y-%m-%dT%H%MZ).archive.gz"

# Password via a temporary config file, not command-line arguments.
printf 'password: "%s"\n' "$MONGO_BACKUP_PASSWORD" | $DOCKER exec -i cmt-mongo sh -c '
  cat > /tmp/cmt-dump.yaml
  mongodump --config /tmp/cmt-dump.yaml --username cmt_backup --authenticationDatabase admin \
            --archive --gzip --quiet
  rc=$?; rm -f /tmp/cmt-dump.yaml; exit $rc' > "$out.part"

[ -s "$out.part" ] || { rm -f "$out.part"; echo "$(date -u) empty dump, removed"; exit 1; }
mv "$out.part" "$out"
echo "$(date -u) dump -> $out ($(du -h "$out" | cut -f1))"

ls -1t "$DIR"/cmt-mongo-*.archive.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --
