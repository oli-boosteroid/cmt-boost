#!/usr/bin/env bash
#
# First start of our MongoDB on the VM. Run once, from this folder:   cd ~/cmt-boost/deploy/mongo && ./init.sh
#
#   1. checks the Docker network cmt-net (created by the bot);
#   2. creates the replica-set key file and random passwords (mongo.env, chmod 600, never in git);
#   3. starts the container (MongoDB 4.4.6), sets up the single-node replica set rs0;
#   4. creates the users: admin (root, maintenance only), cmt_bot, cmt_console, cmt_backup;
#   5. prints the connection strings for the bot .env and the console deploy/console.env.
#
# It refuses to run again once mongo.env exists (the database is already set up).
# The 4.4 image has the legacy "mongo" shell (no mongosh).
set -euo pipefail
cd "$(dirname "$0")"

DOCKER="${CMT_DOCKER:-sudo docker}"
c()   { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mx %s\033[0m\n' "$*" >&2; exit 1; }
mongo_eval() { $DOCKER exec cmt-mongo mongo --quiet --eval "$1" </dev/null 2>/dev/null; }

[ -f mongo.env ] && die "mongo.env exists: the database is already set up (see docs/vm-deploy.md to start over)"
$DOCKER network inspect cmt-net >/dev/null 2>&1 || die "Docker network cmt-net not found: deploy the bot first (~/deploy.sh)"
command -v openssl >/dev/null || die "openssl is not installed"

c "Key file and passwords"
if [ ! -f keyfile ]; then
  openssl rand -base64 756 > keyfile
  sudo chown 999:999 keyfile            # the mongodb user inside the image
  sudo chmod 400 keyfile
fi
gen() { openssl rand -hex 24; }
ADMIN_PW="$(gen)"; BOT_PW="$(gen)"; CONSOLE_PW="$(gen)"; BACKUP_PW="$(gen)"
( umask 077
  cat > mongo.env <<EOF
MONGO_ADMIN_PASSWORD=$ADMIN_PW
MONGO_BOT_PASSWORD=$BOT_PW
MONGO_CONSOLE_PASSWORD=$CONSOLE_PW
MONGO_BACKUP_PASSWORD=$BACKUP_PW
EOF
)

c "Starting cmt-mongo"
$DOCKER compose up -d
for i in $(seq 1 60); do
  mongo_eval "db.adminCommand('ping').ok" | grep -q 1 && break
  [ "$i" = 60 ] && die "MongoDB did not answer in 2 minutes: sudo docker logs cmt-mongo"
  sleep 2
done

c "Replica set rs0"
mongo_eval 'var s = db.adminCommand({replSetGetStatus: 1});
  if (s.ok) { print("already initiated") }
  else { printjson(rs.initiate({_id: "rs0", members: [{_id: 0, host: "cmt-mongo:27017"}]})) }'
for i in $(seq 1 30); do
  mongo_eval "db.isMaster().ismaster" | grep -q true && break
  [ "$i" = 30 ] && die "the replica set did not become primary: sudo docker logs cmt-mongo"
  sleep 2
done

c "Users"
# The script goes in through stdin into a temporary file inside the container, so no password shows up in
# any process list. The first user is created through MongoDB's localhost exception (no users exist yet).
$DOCKER exec -i cmt-mongo sh -c 'cat > /tmp/cmt-init.js' <<EOF
var admin = db.getSiblingDB("admin");
admin.createUser({ user: "admin", pwd: "$ADMIN_PW", roles: ["root"] });
admin.auth("admin", "$ADMIN_PW");
admin.createUser({ user: "cmt_bot", pwd: "$BOT_PW",
  roles: [{ role: "readWrite", db: "reddit_posts" }, { role: "readWrite", db: "reddit_snapshots" }] });
admin.createUser({ user: "cmt_console", pwd: "$CONSOLE_PW",
  roles: [{ role: "readWrite", db: "reddit_posts" }, { role: "read", db: "reddit_snapshots" }] });
admin.createUser({ user: "cmt_backup", pwd: "$BACKUP_PW", roles: [{ role: "backup", db: "admin" }] });
print("users: " + admin.getUsers().map(function (u) { return u.user; }).join(", "));
EOF
$DOCKER exec cmt-mongo sh -c 'mongo --quiet /tmp/cmt-init.js; rc=$?; rm -f /tmp/cmt-init.js; exit $rc' </dev/null

c "Done. Connection strings (keep them private; they are also in $(pwd)/mongo.env)"
q="authSource=admin&replicaSet=rs0"
cat <<EOF

Bot  ~/cmt-boost/.env  (replace the Atlas / Railway lines ONLY at the migration step):
  MONGODB_URI=mongodb://cmt_bot:$BOT_PW@cmt-mongo:27017/?$q
  SNAPSHOT_MONGODB_URI=mongodb://cmt_bot:$BOT_PW@cmt-mongo:27017/?$q

Console  ~/cmt-console/deploy/console.env:
  MONGODB_URI=mongodb://cmt_console:$CONSOLE_PW@cmt-mongo:27017/?$q
  SNAPSHOT_MONGODB_URI=mongodb://cmt_console:$CONSOLE_PW@cmt-mongo:27017/?$q

Daily dump (as root):  sudo crontab -e   and add
  30 3 * * * $(pwd)/dump.sh >> /var/log/cmt-mongo-dump.log 2>&1
EOF
