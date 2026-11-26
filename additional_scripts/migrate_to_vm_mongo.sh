#!/usr/bin/env bash
# Copy reddit_posts (Atlas) and reddit_snapshots (Railway) into cmt-mongo, then compare counts.
#   migrate_to_vm_mongo.sh rehearse   # into *_rehearsal databases
#   migrate_to_vm_mongo.sh final      # with the bot and the console stopped
# Settings in ~/.cmt_migrate.env (chmod 600): SRC_MONGODB_URI, SRC_MONGODB_DATABASE, SRC_SNAPSHOT_URI,
# SRC_SNAPSHOT_DATABASE, DST_URI (admin user), DST_MONGODB_DATABASE, DST_SNAPSHOT_DATABASE.
# Remove ~/cmt-migrate once the move is checked.
set -euo pipefail

MODE="${1:-}"
[ "$MODE" = rehearse ] || [ "$MODE" = final ] || { echo "usage: $0 rehearse|final"; exit 2; }
ENV_FILE="${CMT_MIGRATE_ENV:-$HOME/.cmt_migrate.env}"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE"; exit 2; }

# KEY=value lines, read as text (values contain "&" and may be quoted).
while IFS= read -r line || [ -n "$line" ]; do
  line="${line%$'\r'}"
  case "$line" in ''|\#*) continue ;; esac
  key="${line%%=*}"; val="${line#*=}"
  val="${val#\"}"; val="${val%\"}"; val="${val#\'}"; val="${val%\'}"
  export "$key=$val"
done < "$ENV_FILE"
for v in SRC_MONGODB_URI SRC_MONGODB_DATABASE SRC_SNAPSHOT_URI SRC_SNAPSHOT_DATABASE DST_URI \
         DST_MONGODB_DATABASE DST_SNAPSHOT_DATABASE; do
  [ -n "${!v:-}" ] || { echo "$v is empty in $ENV_FILE"; exit 2; }
done

# The mongo tools need a "/" before the options ("host/?a=b"); pymongo also accepts "host?a=b".
norm_uri() {
  local u="$1" base rest
  case "$u" in *\?*) ;; *) printf '%s' "$u"; return ;; esac
  base="${u%%\?*}"; rest="${base#*://}"
  case "$rest" in */*) ;; *) base="$base/" ;; esac
  printf '%s?%s' "$base" "${u#*\?}"
}
# Without authSource the tools look for the user in the --db database; pymongo (the bot) uses "admin".
with_authsource() {
  local u="$1"
  case "$u" in *authSource=*|mongodb+srv://*) printf '%s' "$u"; return ;; esac
  case "$u" in *\?*) printf '%s&authSource=admin' "$u"; return ;; esac
  case "${u#*://}" in */*) printf '%s?authSource=admin' "$u" ;; *) printf '%s/?authSource=admin' "$u" ;; esac
}
SRC_MONGODB_URI="$(with_authsource "$(norm_uri "$SRC_MONGODB_URI")")"
SRC_SNAPSHOT_URI="$(with_authsource "$(norm_uri "$SRC_SNAPSHOT_URI")")"
DST_URI="$(norm_uri "$DST_URI")"

DOCKER="${CMT_DOCKER:-sudo docker}"
IMAGE="${MONGO_TOOLS_IMAGE:-mongo:7.0}"
BOT_IMAGE="${CMT_BOT_IMAGE:-cmt-boost-cmt-boost}"
NET="${DOCKER_NETWORK:-cmt-net}"
WORK="$HOME/cmt-migrate"; mkdir -p "$WORK"; chmod 700 "$WORK"
SUFFIX=""; [ "$MODE" = rehearse ] && SUFFIX="_rehearsal"
c() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# Connection strings reach the containers through env files (sudo drops the shell environment).
TOOL_ENV="$WORK/.tool.env"; CMP_ENV="$WORK/.compare.env"
trap 'rm -f "$TOOL_ENV" "$CMP_ENV"' EXIT
( umask 077
  printf 'SRC_MONGODB_URI=%s\nSRC_SNAPSHOT_URI=%s\nDST_URI=%s\n' \
    "$SRC_MONGODB_URI" "$SRC_SNAPSHOT_URI" "$DST_URI" > "$TOOL_ENV"
  printf '%s\n' "SRC_MONGODB_URI=$SRC_MONGODB_URI" "SRC_MONGODB_DATABASE=$SRC_MONGODB_DATABASE" \
    "SRC_SNAPSHOT_URI=$SRC_SNAPSHOT_URI" "SRC_SNAPSHOT_DATABASE=$SRC_SNAPSHOT_DATABASE" \
    "MONGODB_URI=$DST_URI" "MONGODB_DATABASE=${DST_MONGODB_DATABASE}${SUFFIX}" \
    "SNAPSHOT_MONGODB_DATABASE=${DST_SNAPSHOT_DATABASE}${SUFFIX}" > "$CMP_ENV" )
tool() {   # run a mongo tool in a throwaway container
  $DOCKER run --rm -i --network "$NET" -v "$WORK:/work" --env-file "$TOOL_ENV" "$IMAGE" bash -c "$1"
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

c "Restore into ${DST_MONGODB_DATABASE}${SUFFIX} and ${DST_SNAPSHOT_DATABASE}${SUFFIX}"
tool "mongorestore --uri=\"\$DST_URI\" --archive=/work/main.archive --gzip --drop \
      --nsFrom='$SRC_MONGODB_DATABASE.*' --nsTo='${DST_MONGODB_DATABASE}${SUFFIX}.*'"
tool "mongorestore --uri=\"\$DST_URI\" --archive=/work/snap.archive --gzip --drop \
      --nsFrom='$SRC_SNAPSHOT_DATABASE.*' --nsTo='${DST_SNAPSHOT_DATABASE}${SUFFIX}.*'"
c "Copied in $(( $(date +%s) - start )) s"

c "Compare counts (read-only)"
$DOCKER run --rm --network "$NET" --env-file "$CMP_ENV" "$BOT_IMAGE" \
  python additional_scripts/compare_counts.py || echo "(differences above)"

if [ "$MODE" = rehearse ]; then
  echo
  echo "Drop the rehearsal databases when done:"
  echo "  sudo docker exec -it cmt-mongo mongo -u admin -p --authenticationDatabase admin --eval \\"
  echo "    'db.getSiblingDB(\"${DST_MONGODB_DATABASE}_rehearsal\").dropDatabase(); db.getSiblingDB(\"${DST_SNAPSHOT_DATABASE}_rehearsal\").dropDatabase()'"
fi
echo "Delete the dump when finished:  rm -rf $WORK"
