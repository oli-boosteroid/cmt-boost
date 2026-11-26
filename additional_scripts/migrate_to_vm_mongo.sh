#!/usr/bin/env bash
# Copy reddit_posts (Atlas) and reddit_snapshots (Railway) into cmt-mongo.
#   migrate_to_vm_mongo.sh rehearse   # into *_rehearsal databases
#   migrate_to_vm_mongo.sh final      # with the bot and the console stopped
# Settings in ~/.cmt_migrate.env (chmod 600): SRC_MONGODB_URI, SRC_MONGODB_DATABASE, SRC_SNAPSHOT_URI,
# SRC_SNAPSHOT_DATABASE, DST_URI (admin user), DST_MONGODB_DATABASE, DST_SNAPSHOT_DATABASE.
# Remove ~/cmt-migrate once the move is checked.
set -euo pipefail

MODE="${1:-}"
[ "$MODE" = rehearse ] || [ "$MODE" = final ] || { echo "usage: $0 rehearse|final"; exit 2; }
ENV_FILE="${CMT_MIGRATE_ENV:-$HOME/.cmt_migrate.env}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (see the header of this script)"; exit 2; }
set -a; . "$ENV_FILE"; set +a
DOCKER="${CMT_DOCKER:-sudo docker}"
IMAGE="${MONGO_TOOLS_IMAGE:-mongo:7.0}"
NET="${DOCKER_NETWORK:-cmt-net}"
WORK="$HOME/cmt-migrate"; mkdir -p "$WORK"; chmod 700 "$WORK"
SUFFIX=""; [ "$MODE" = rehearse ] && SUFFIX="_rehearsal"
c() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

tool() {   # run a mongo tool in a throwaway container; URIs are passed as env, never as visible args
  $DOCKER run --rm -i --network "$NET" -v "$WORK:/work" \
    -e SRC_MONGODB_URI -e SRC_SNAPSHOT_URI -e DST_URI "$IMAGE" bash -c "$1"
}

if [ "$MODE" = final ]; then
  c "Final move: bot and console stopped, console_actions empty. Type MOVE to continue:"
  read -r answer; [ "$answer" = MOVE ] || { echo "aborted"; exit 1; }
fi

start=$(date +%s)
c "Dump main database ($SRC_MONGODB_DATABASE) from the source"
tool "mongodump --uri=\"\$SRC_MONGODB_URI\" --db=$SRC_MONGODB_DATABASE --archive=/work/main.archive --gzip"
c "Dump snapshot database ($SRC_SNAPSHOT_DATABASE) from the source"
tool "mongodump --uri=\"\$SRC_SNAPSHOT_URI\" --db=$SRC_SNAPSHOT_DATABASE --archive=/work/snap.archive --gzip"
ls -lh "$WORK"

c "Restore into ${DST_MONGODB_DATABASE}${SUFFIX} and ${DST_SNAPSHOT_DATABASE}${SUFFIX} on the VM"
tool "mongorestore --uri=\"\$DST_URI\" --archive=/work/main.archive --gzip --drop \
      --nsFrom='$SRC_MONGODB_DATABASE.*' --nsTo='${DST_MONGODB_DATABASE}${SUFFIX}.*'"
tool "mongorestore --uri=\"\$DST_URI\" --archive=/work/snap.archive --gzip --drop \
      --nsFrom='$SRC_SNAPSHOT_DATABASE.*' --nsTo='${DST_SNAPSHOT_DATABASE}${SUFFIX}.*'"

c "Done in $(( $(date +%s) - start )) s. Now compare counts (read-only), from ~/cmt-boost:"
echo "  set -a; . $ENV_FILE; set +a"
echo "  sudo -E docker compose run --rm -e SRC_MONGODB_URI -e SRC_SNAPSHOT_URI \\"
echo "    -e MONGODB_URI=\"\$DST_URI\" -e MONGODB_DATABASE=${DST_MONGODB_DATABASE}${SUFFIX} \\"
echo "    -e SNAPSHOT_MONGODB_DATABASE=${DST_SNAPSHOT_DATABASE}${SUFFIX} cmt-boost python additional_scripts/compare_counts.py"
[ "$MODE" = rehearse ] && echo "Rehearsal databases can be dropped after the check: db.getSiblingDB('<name>_rehearsal').dropDatabase()"
echo "Delete the dump when finished:  rm -rf $WORK"
