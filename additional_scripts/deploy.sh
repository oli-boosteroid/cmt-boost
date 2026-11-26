#!/usr/bin/env bash
#
# CMT bot - one-shot VM deploy.
#
#   stop → pull latest from GitHub (token from a file, no prompt) → rebuild → start → tail logs
#   Every Nth run it also runs `docker builder prune -af` so the disk doesn't fill up.
#
# FIRST-TIME SETUP on the VM:
#   1) Put ONLY your GitHub token in the token file and lock it down:
#        printf '%s' 'ghp_xxxxxxxxxxxx' > ~/.cmt_deploy_token && chmod 600 ~/.cmt_deploy_token
#   2) Copy this script OUT of the repo so a mid-deploy `git reset` can't rewrite it while it runs:
#        cp additional_scripts/deploy.sh ~/deploy.sh && chmod +x ~/deploy.sh
#   3) Deploy any time with:   ~/deploy.sh
#
# Override any setting via env, e.g.:  CMT_PRE_STOP=0 CMT_PRUNE_EVERY=3 ~/deploy.sh
#
set -euo pipefail

# ─── config ─────────────────────────────────────────────────────────────────
REPO_DIR="${CMT_REPO_DIR:-$HOME/cmt-boost}"              # git + docker-compose directory
SERVICE="${CMT_SERVICE:-cmt-boost}"                      # docker compose service name
BRANCH="${CMT_BRANCH:-main}"
TOKEN_FILE="${CMT_TOKEN_FILE:-$HOME/.cmt_deploy_token}"  # a file containing ONLY your GitHub token
STATE_DIR="${CMT_STATE_DIR:-$HOME/.cmt-deploy}"          # holds the deploy counter
PRUNE_EVERY="${CMT_PRUNE_EVERY:-5}"                      # run `builder prune -af` every N deploys
PRE_STOP="${CMT_PRE_STOP:-1}"                            # 1 = stop before build (your flow);
                                                         # 0 = build new image first, then recreate (less downtime)
FOLLOW="${CMT_FOLLOW:-1}"                                # 1 = tail -f logs at the end (manual use);
                                                         # 0 = print last lines and exit (used by the deploy agent)
DOCKER="${CMT_DOCKER:-sudo docker}"
DC="$DOCKER compose"

# ─── helpers ────────────────────────────────────────────────────────────────
c()   { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ -d "$REPO_DIR/.git" ] || die "no git repo at $REPO_DIR (set CMT_REPO_DIR)"
[ -f "$TOKEN_FILE" ]    || die "token file $TOKEN_FILE missing — create it with your GitHub token (chmod 600)"
GIT_TOKEN="$(tr -d ' \t\r\n' < "$TOKEN_FILE")"; export GIT_TOKEN
[ -n "$GIT_TOKEN" ]     || die "token file $TOKEN_FILE is empty"
mkdir -p "$STATE_DIR"
cd "$REPO_DIR"

# Rescue net: if we stop the bot but never finish a good rebuild, bring the old container back up.
STOPPED=0; OK=0
trap 'ec=$?; if [ "$STOPPED" = 1 ] && [ "$OK" = 0 ]; then
        c "deploy aborted (exit $ec) — restarting the existing image so the bot is not left down";
        $DC up -d "$SERVICE" || true; fi' EXIT

# ─── 1. stop ────────────────────────────────────────────────────────────────
if [ "$PRE_STOP" = 1 ]; then
  c "Stopping $SERVICE"
  $DC stop "$SERVICE"
  STOPPED=1
fi

# ─── 2. pull (token via a one-shot credential helper: never in argv/history/config) ─
c "Pulling $BRANCH"
before="$(git rev-parse --short HEAD 2>/dev/null || echo none)"
git -c credential.helper='!f(){ echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f' \
    fetch --prune origin "$BRANCH"
git reset --hard FETCH_HEAD          # working tree = remote exactly; untracked files (.env, token) are kept
unset GIT_TOKEN
after="$(git rev-parse --short HEAD)"
if [ "$before" = "$after" ]; then
  c "Already at $after — rebuilding anyway"
else
  c "Updated $before → $after:"
  git --no-pager log --oneline "$before..$after" | sed 's/^/    /'
fi

# ─── 3. every Nth deploy: free docker disk space ────────────────────────────
n=$(( $(cat "$STATE_DIR/count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STATE_DIR/count"
if [ $(( n % PRUNE_EVERY )) -eq 0 ]; then
  c "Deploy #$n - pruning docker build cache + dangling images (every $PRUNE_EVERY)"
  $DOCKER builder prune -af || true
  $DOCKER image prune -f    || true
else
  c "Deploy #$n - next cache prune at #$(( (n / PRUNE_EVERY + 1) * PRUNE_EVERY ))"
fi

# ─── 4. rebuild + start ─────────────────────────────────────────────────────
c "Building & starting $SERVICE"
$DC up -d --build "$SERVICE"
OK=1

# ─── 5. verify + follow logs ────────────────────────────────────────────────
$DC ps "$SERVICE"
c "Following logs — press Ctrl+C to stop watching (the bot keeps running)"
$DC logs -f --tail=80 "$SERVICE"
