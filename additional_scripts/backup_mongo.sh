#!/usr/bin/env bash
#
# Daily encrypted backup of the bot + console databases on the VM Mongo. Not needed if DevOps already backs
# this Mongo up (ask them how long they keep copies: that also bounds how long deleted data survives).
#
#   ~/cmt-boost/additional_scripts/backup_mongo.sh            # one backup now
#   crontab:  15 3 * * *  $HOME/cmt-boost/additional_scripts/backup_mongo.sh >> $HOME/cmt-backups/backup.log 2>&1
#
# Settings file (chmod 600), default ~/.cmt_backup.env:
#   BACKUP_URI=mongodb://<user with the backup role>@<vm mongo host>:27017/?authSource=admin
#   BACKUP_DATABASES="reddit_posts reddit_snapshots"
#   BACKUP_PASSFILE=$HOME/.cmt_backup_pass     # a long random passphrase; keep a copy OFF the VM, or the backups are useless
#   BACKUP_DIR=$HOME/cmt-backups               # then copy the *.enc files off the VM (place given by DevOps)
#   BACKUP_KEEP_DAYS=14
#   DOCKER_NETWORK=host
#
# Restore test (do it monthly):  decrypt -> mongorestore into a throwaway container, compare_counts.py --target-only.
#   openssl enc -d -aes-256-cbc -pbkdf2 -pass file:$BACKUP_PASSFILE -in X.archive.gz.enc > X.archive.gz
set -euo pipefail
ENV_FILE="${CMT_BACKUP_ENV:-$HOME/.cmt_backup.env}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (see the header of this script)"; exit 2; }
set -a; . "$ENV_FILE"; set +a
DOCKER="${CMT_DOCKER:-sudo docker}"
DIR="${BACKUP_DIR:-$HOME/cmt-backups}"; mkdir -p "$DIR"; chmod 700 "$DIR"
STAMP="$(date -u +%Y-%m-%dT%H%MZ)"
[ -f "$BACKUP_PASSFILE" ] || { echo "missing passphrase file $BACKUP_PASSFILE"; exit 2; }

for db in ${BACKUP_DATABASES:-reddit_posts reddit_snapshots}; do
  out="$DIR/$db-$STAMP.archive.gz.enc"
  $DOCKER run --rm -i --network "${DOCKER_NETWORK:-host}" -e BACKUP_URI "${MONGO_TOOLS_IMAGE:-mongo:7.0}" \
      bash -c "mongodump --uri=\"\$BACKUP_URI\" --db=$db --archive --gzip" \
    | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$BACKUP_PASSFILE" -out "$out"
  [ -s "$out" ] || { echo "$(date -u) backup of $db is EMPTY"; exit 1; }
  echo "$(date -u) $db -> $out ($(du -h "$out" | cut -f1))"
done
find "$DIR" -name '*.archive.gz.enc' -mtime +"${BACKUP_KEEP_DAYS:-14}" -print -delete
