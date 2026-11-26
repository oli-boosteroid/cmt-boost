#!/usr/bin/env bash
#
# Dump of our MongoDB onto this VM: one gzip archive per run, the newest KEEP (3) are kept.
# Protects against deleted or broken data, NOT against losing the VM disk (the copies are on the same disk).
#
#   one dump now:     sudo ~/cmt-boost/deploy/mongo/dump.sh
#   every night:      sudo crontab -e   ->   30 3 * * * /home/<you>/cmt-boost/deploy/mongo/dump.sh >> /var/log/cmt-mongo-dump.log 2>&1
#
# Restore (overwrites the databases in the dump with its content; the bot and the console should be stopped):
#   sudo docker exec -i cmt-mongo sh -c 'mongorestore --username admin --password "$P" --authenticationDatabase admin \
#       --archive --gzip --drop' < /var/backups/cmt-mongo/<file>      (with -e P=<MONGO_ADMIN_PASSWORD from mongo.env>)
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

# The password goes in through stdin into a config file inside the container (not on any command line).
printf 'password: "%s"\n' "$MONGO_BACKUP_PASSWORD" | $DOCKER exec -i cmt-mongo sh -c '
  cat > /tmp/cmt-dump.yaml
  mongodump --config /tmp/cmt-dump.yaml --username cmt_backup --authenticationDatabase admin \
            --archive --gzip --quiet
  rc=$?; rm -f /tmp/cmt-dump.yaml; exit $rc' > "$out.part"

[ -s "$out.part" ] || { rm -f "$out.part"; echo "$(date -u) dump is EMPTY, nothing kept"; exit 1; }
mv "$out.part" "$out"
echo "$(date -u) dump -> $out ($(du -h "$out" | cut -f1))"

# keep the newest KEEP dumps
ls -1t "$DIR"/cmt-mongo-*.archive.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --
