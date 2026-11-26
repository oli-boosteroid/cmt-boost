import os
import logging
from pathlib import Path
import asyncio
from dotenv import load_dotenv

env_path = Path(__file__).parent / '.env'
load_dotenv(env_path)

from waitress import serve
import flask
from flask import Flask
import threading
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from motor.motor_asyncio import AsyncIOMotorClient
import schedule
import time
from datetime import date
from logging_config import setup_logging
import ssl
import certifi

from reddit_notifications.redit_notifications import start_tracking
from reddit_notifications.mod_notes import reconcile_reddit_notes, archive_stale_in_progress, archive_inactive_convos, start_reddit_maintenance
from jira_notifications.jira_notifications import handle_jira_channel_message, check_and_notify_stale_tickets
from handlers.action_handlers import register_action_handlers
from handlers.modal_handlers import register_modal_handlers
from services.mongo_db_service import collect_and_send_reports
from discord_notifications.discord_notifications import start_discord_bot
from additional_scripts.create_button import send_buttons_to_channels
from services.boosteroid_catalog_service import load_catalog_from_file, seed_next_page
from services.facebook_pin_service import refresh_facebook_pin
from services.maintenance_notify_service import process_pending_reddit_maintenance
from telegram_bot.snapshot_bot import start_tg_bot
from reddit_notifications.devvit_inbox import ingest_slack_relay

setup_logging()
logger = logging.getLogger(__name__)

ssl._create_default_https_context = lambda: ssl.create_default_context(
    cafile=certifi.where()
)


slack_app = App(
    token=os.getenv('SLACK_TOKEN'),
    signing_secret=os.getenv('SLACK_SIGNING_SECRET'),
    ignoring_self_events_enabled=False
)

register_action_handlers(slack_app)
register_modal_handlers(slack_app)

flask_app = Flask(__name__)
handler = SlackRequestHandler(slack_app)

# The CMT Console runs separately (cmt-console). The bot serves only Slack events and the health probe;
# game / server maintenances come from the Mattermost admin events (services/mm_maintenance.py).


# Events
@slack_app.event("message")
def handle_message_events(event):
    if ingest_slack_relay(event):   # Reddit events relayed by the Devvit app (private relay channel)
        return
    handle_jira_channel_message(event)

@slack_app.event({"type": "message", "subtype": "bot_message"})
def handle_bot_message_events(event):
    if ingest_slack_relay(event):
        return
    handle_jira_channel_message(event)


# Commands

@slack_app.command("/send-report")
def send_report_command(ack, command, say, logger):
    ack()
    try:
        collect_and_send_reports()

        say(
            text="Report sent!",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Report was sent to L1"
                    }
                }
            ]
        )
    except Exception as e:
        logger.error(f"[Send Report] Exception occurred while sending report message by command: {e}")

@slack_app.command("/healthcheck")
def healthcheck(ack, say):
    ack()
    say("I'm alive!")

@slack_app.command("/resend-buttons")
def resent_buttons(ack, say):
    ack()
    try:
        result = send_buttons_to_channels()
        lines = []
        if result["sent"]:
            lines.append("*Sent:*")
            lines.extend(f"  • {label}" for label in result["sent"])
        if result["skipped"]:
            lines.append("*Already present (skipped):*")
            lines.extend(f"  • {label}" for label in result["skipped"])
        say("\n".join(lines) if lines else "Nothing to send.")
    except Exception as e:
        say(f"Error occurred while resending buttons: {e}")

@slack_app.command("/logs")
def logs_command(ack, command, client, logger):
    ack()
    try:
        raw = (command.get("text") or "").strip().lower()
        parts = raw.split()

        lines_count = 50
        filter_level = None

        for part in parts:
            if part.isdigit():
                lines_count = min(int(part), 1000)
            elif part in ("error", "warning", "info"):
                filter_level = part.upper()

        log_path = Path("data/app.log")
        if not log_path.exists():
            client.chat_postEphemeral(
                channel=command["channel_id"],
                user=command["user_id"],
                text="Log file not found."
            )
            return

        # Read only the tail (the file can be 10MB): the last 2MB always covers 1000 lines.
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 2 * 1024 * 1024))
            all_lines = f.read().decode("utf-8", errors="replace").splitlines(keepends=True)
        if size > 2 * 1024 * 1024 and all_lines:
            all_lines = all_lines[1:]   # the first line of the window is usually cut mid-line

        if filter_level:
            all_lines = [l for l in all_lines if f" - {filter_level} - " in l]

        selected = all_lines[-lines_count:]
        content = "".join(selected) or "(no log entries found)"

        title = f"app.log — last {len(selected)} lines"
        if filter_level:
            title += f" [{filter_level}]"

        client.files_upload_v2(
            channel=command["channel_id"],
            content=content,
            filename="app.log",
            title=title,
        )

    except Exception as e:
        logger.error(f"[Logs] Error uploading log file: {e}")





@flask_app.route('/slack/events', methods=['POST'])
def slack_events():
    return handler.handle(flask.request)


@flask_app.route('/healthz', methods=['GET'])
def healthz():
    # Container health probe. /slack/events only accepts POST, so probing it with a GET always
    # failed (405) and Docker reported the bot as permanently unhealthy.
    return "ok", 200

def run_async_tracker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Effective Reddit ignore list = .env IGNORED_USERS + console exclusions + linked team accounts
    # Falls back to .env if Mongo is unavailable, so startup is safe with no links yet.
    try:
        from services.identity_service import get_ignored_reddit_users
        ignored_users = ','.join(get_ignored_reddit_users())
    except Exception as e:
        logger.warning('[Startup] identity ignore list fell back to .env: %s', e)
        ignored_users = os.getenv('IGNORED_USERS', '')
    loop.run_until_complete(start_tracking(ignored_users))

def run_discord_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Creating new MongoDB client
    # TLS CA bundle only for Atlas (mongodb+srv); a Mongo on the VM itself is reached without TLS, and
    # passing tlsCAFile would force TLS on it (same rule as services/db.py).
    _uri = os.getenv('MONGODB_URI') or ''
    mongo_client = AsyncIOMotorClient(_uri, **({'tlsCAFile': certifi.where()} if _uri.startswith('mongodb+srv://') else {}))
    mongo_db = mongo_client[os.getenv('MONGODB_DATABASE', 'reddit_posts')]

    loop.run_until_complete(start_discord_bot(mongo_db))

def run_schedule():
    while True:
        schedule.run_pending()
        time.sleep(60)

def schedule_new_buttons_to_channels():
    if date.today().day != 1:
        logger.debug("Monthly buttons are sent on the 1st only; skipped today")
        return
    else:
        send_buttons_to_channels()


def schedule_task():
    schedule.every().day.at("05:00").do(collect_and_send_reports)
    schedule.every().day.at("06:00").do(schedule_new_buttons_to_channels)
    schedule.every(6).hours.do(check_and_notify_stale_tickets)
    # Jira title index for the console's "Link Jira Ticket" search (services/jira_so_index.py).
    from services.jira_so_index import sync_recent as _jira_index_recent, sync_backlog as _jira_index_backlog, ensure_indexes as _jira_index_ix
    _jira_index_ix()
    from services.jira_autolink import ensure_indexes as _autolink_ix   # Jira comment with a chat link -> auto-link
    _autolink_ix()
    schedule.every(10).minutes.do(_jira_index_recent)
    schedule.every(6).hours.do(_jira_index_backlog)
    threading.Thread(target=_jira_index_backlog, daemon=True, name="jira-index-backlog").start()
    # (removed) poll_jira_comments: the old auto-binding of emails found in Jira comments.
    schedule.every(15).minutes.do(refresh_facebook_pin)
    from services.facebook_feed_notify import notify_facebook_feed
    schedule.every(1).minutes.do(notify_facebook_feed)
    from services.reddit_user_id_notes import sync_reddit_user_id_notes
    schedule.every(10).minutes.do(sync_reddit_user_id_notes)
    from reddit_notifications.devvit_inbox import catch_up_relay
    schedule.every(5).minutes.do(catch_up_relay)          # Devvit events Slack did not deliver live
    threading.Thread(target=catch_up_relay, daemon=True, name="devvit-catch-up").start()
    schedule.every(30).minutes.do(reconcile_reddit_notes)   # email notes + Jira-Done reconcile
    schedule.every(30).days.do(archive_stale_in_progress)   # monthly In Progress cleanup
    schedule.every().day.at("04:30").do(archive_inactive_convos)  # daily: archive convos idle >5d (console History rule; skips highlighted)
    schedule.every(2).minutes.do(process_pending_reddit_maintenance)  # Reddit maintenance-end messages to users
    schedule.every(1).hours.do(seed_next_page)          # no-op once catalog file exists
    # (removed) weekly Friday catalog sync: the catalog is now updated from Mattermost game events
    # (services/mattermost_events.py -> boosteroid_catalog_service.upsert_game / remove_game).




if __name__ == '__main__':
    schedule_task()

    # Ensure the console's read-query indexes exist (idempotent, non-blocking) so growing collections
    # stay fast instead of scanning on every console hydrate/refresh.
    try:
        from services.ensure_indexes import ensure_indexes
        ensure_indexes()
    except Exception as e:
        logger.error(f"[Indexes] ensure_indexes failed at startup: {e}")

    # Load Boosteroid catalog from JSON file into memory (instant, no network I/O).
    load_catalog_from_file()

    # If no catalog file exists yet, kick off page 1 of the gradual seed immediately
    # in a background thread. Subsequent pages are fetched hourly by the scheduler.
    if not Path("data/boosteroid_catalog.json").exists():
        threading.Thread(target=seed_next_page, daemon=True, name="catalog-seed-p1").start()

    schedule_thread = threading.Thread(target=run_schedule, daemon=True)
    schedule_thread.start()

    # Mattermost admin-service events -> console Events tab + game/server maintenances (Slack, active list,
    # end-of-maintenance notices). Without MM credentials there are no automatic maintenances.
    from services import mattermost_events
    mattermost_events.start()
    from services import wishlist_store
    wishlist_store.start()            # own wishlist: legacy import, Monday summary, catalog check

    # Render the Facebook pinned message once at startup (scheduler refreshes it every 15 min).
    threading.Thread(target=refresh_facebook_pin, daemon=True, name="facebook-pin-initial").start()

    # Reddit mod-note maintenance: email backfill + In Progress cleanup, starting 10 min after boot.
    start_reddit_maintenance()

    # Console actions queue: execute actions the CMT Console enqueues into Mongo.
    try:
        from services.console_actions_worker import start_console_actions_worker
        start_console_actions_worker()
    except Exception as e:
        logger.error(f"[ConsoleActions] failed to start worker: {e}")

    # Internal Jira gateway for the console on the same VM (internal port, off without JIRA_GATEWAY_TOKEN).
    try:
        from services.jira_gateway import start as start_jira_gateway
        start_jira_gateway()
    except Exception as e:
        logger.error(f"[JiraGateway] failed to start: {e}")

    # Reddit snapshot: mirror modmail threads + posts into Mongo so the console tabs are real.
    try:
        from services.reddit_snapshot import start_reddit_snapshot
        start_reddit_snapshot()
    except Exception as e:
        logger.error(f"[RedditSnapshot] failed to start: {e}")

    # Move running "<game> [Brazil]" maintenances, saved as regular ones before the Brazil fix, to the
    # Brazil list. Idempotent: does nothing once they are moved.
    try:
        from services.maintenance_names import migrate_brazil_maintenances
        moved = migrate_brazil_maintenances()
        if moved:
            logger.info(f"[Maintenance] moved {moved} running maintenance(s) to Brazil")
    except Exception as e:
        logger.error(f"[Maintenance] Brazil migration failed: {e}")

    # Reminders / SLA nudges: pings the TG work group + on-shift CM when work sits too long (Admin config).
    try:
        from services.reminders_service import start_reminders_service
        start_reminders_service()
    except Exception as e:
        logger.error(f"[Reminders] failed to start: {e}")

    # MTProto (Telethon) client: reply to Telegram users "as the group" (anonymous admin). No-op if TG_* env is unset.
    try:
        from telegram_bot.mtproto import start_mtproto
        start_mtproto()
    except Exception as e:
        logger.error(f"[MTProto] failed to start: {e}")

    # Bans: the Telegram ban channel -> Mongo `bans` (console Bans tab). Reads via the MTProto client above.
    try:
        from services.bans_tracker import start_bans_tracker
        start_bans_tracker()
    except Exception as e:
        logger.error(f"[Bans] failed to start: {e}")

    # Known Issues sync: mirror the team Google Sheet into Mongo + append rows from the console.
    try:
        from services.known_issues_sync import start_known_issues_sync
        start_known_issues_sync()
    except Exception as e:
        logger.error(f"[KnownIssues] failed to start: {e}")

    # Team schedule sync: mirror the monthly schedule sheet + write admin edits / new months.
    try:
        from services.schedule_sync import start_schedule_sync
        start_schedule_sync()
    except Exception as e:
        logger.error(f"[Schedule] failed to start: {e}")

    # Game Cards sync: mirror weekly card tabs + generate/edit + stash screenshot ZIPs.
    try:
        from services.game_cards_sync import start_game_cards_sync
        start_game_cards_sync()
    except Exception as e:
        logger.error(f"[GameCards] failed to start: {e}")

    # Game Testing sync: weekly test sheet (Xbox/Steam), polled every minute on Thu/Fri.
    try:
        from services.game_testing_sync import start_game_testing_sync
        start_game_testing_sync()
    except Exception as e:
        logger.error(f"[GameTesting] failed to start: {e}")

    # Game List sync: mirror the game ID <-> title map (for Known Issues game IDs + deep links).
    try:
        from services.game_list_sync import start_game_list_sync
        start_game_list_sync()
    except Exception as e:
        logger.error(f"[GameList] failed to start: {e}")

    tracker_thread = threading.Thread(target=run_async_tracker, daemon=True)
    discord_bot_thread = threading.Thread(target=run_discord_bot, daemon=True)
    tg_bot_thread = threading.Thread(target=start_tg_bot, daemon=True)

    tracker_thread.start()
    discord_bot_thread.start()
    tg_bot_thread.start()

    # 4 threads (waitress default) queued Slack events under load ("Task queue depth" warnings).
    serve(flask_app, host='0.0.0.0', port=7000, threads=int(os.getenv('BOT_HTTP_THREADS', '16')))
