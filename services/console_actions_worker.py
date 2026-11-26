"""
Console actions worker - the BOT side of the console_actions queue.

The CMT Console (running elsewhere, e.g. Railway) enqueues action documents into the shared
Atlas `console_actions` collection. This worker runs inside the bot process - which holds the
live Discord/Reddit connections - claims pending actions atomically, executes them via the
bot's own send functions, and writes the result back. The console and the bot never talk
directly; they meet only in Mongo.

Customer-facing sends (Discord !say to the user, Reddit public replies / post comments) reach real
users: every action is checked by services/action_policy.py before it runs, and a new handler is tried
on a test ticket/conversation before the team relies on it.
"""
import os
import logging
import threading
import time
from datetime import datetime, timedelta

from services.mongo_db_service import sync_db

logger = logging.getLogger(__name__)

actions_col = sync_db["console_actions"]
POLL_SECONDS = 3          # fallback polling interval when change streams are not available
SAFETY_POLL_SECONDS = 30  # while the change stream works: a quiet re-check of the queue now and then
DONE_RETAIN_DAYS = 30    # keep completed actions this long, then Mongo TTL auto-deletes (~1 month)
ERROR_RETAIN_DAYS = 30   # keep failed actions the same month before auto-delete


def _reddit_audit(convo_id, text, author):
    """No-op: no automatic "[Console] ..." notes are posted into Reddit modmail conversations, only notes a
    console user types by hand. Kept so callers stay valid."""
    return


def _via_devvit() -> bool:
    """Reddit actions go through the Devvit app (cmthelper) whenever the Slack relay is configured
    (DEVVIT_REPLIES_ENABLED=0 switches back to the API). The app runs each command on Reddit within about a minute; a
    failure comes back as an ack that flips the action to "error" (reddit_notifications/devvit_inbox.py)."""
    if os.getenv("DEVVIT_REPLIES_ENABLED") == "0":
        return False
    from reddit_notifications.devvit_inbox import relay_configured
    return relay_configured()


_title_cache = {}


def _game_title(aid: str) -> str:
    """Game name for an application ID that the daily Game List mirror does not have yet (a game added
    today): the Boosteroid catalog first, then the public applications API for that one ID."""
    if aid in _title_cache:
        return _title_cache[aid]
    name = ""
    try:
        from services.boosteroid_catalog_service import catalog_games, BOOSTEROID_API_BASE
        hit = next((g for g in catalog_games() if str(g.get("id")) == aid), None)
        name = (hit or {}).get("name") or ""
        if not name:
            import requests
            resp = requests.get(f"{BOOSTEROID_API_BASE}/applications/{int(aid)}", timeout=10)
            if resp.ok:
                name = ((resp.json() or {}).get("data") or {}).get("name") or ""
    except Exception as e:
        logger.warning("[ConsoleActions] game title for application %s not found: %s", aid, e)
    if name:
        _title_cache[aid] = name
    return name


def _handle(action):
    """Return (result_dict, error_str). error_str is None on success."""
    atype = action.get("type")
    payload = action.get("payload") or {}

    if atype == "ping":
        return {"pong": True}, None

    if atype == "id_topup":
        # Console "Apply migration": the bot looks up every User ID still missing (runs in its own thread,
        # progress in console_config "id_topup").
        try:
            from services.user_id_migration import start_topup
        except ImportError:
            return None, "the email -> User ID migration is finished and its code was removed"
        return {"started": start_topup()}, None

    if atype == "cloud_user_lookup":
        # Resolve a Boosteroid User ID from an email, a session ID or the ID itself (cloud Operations
        # API, read-only). The email is wiped from the stored action right away: the queue keeps finished
        # actions for a month and must not become a place where addresses pile up.
        from services import cloud_api
        email = str(payload.get("email") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        user_id = payload.get("user_id")
        if email:
            actions_col.update_one({"_id": action["_id"]}, {"$set": {"payload.email": "[removed]"}})
        if session_id and not email and user_id in (None, ""):
            from services.user_ref import resolve_session
            uid, status = resolve_session(session_id)
            if uid:
                return {"user_id": int(uid)}, None
            return None, ("session not found" if status == "not_found" else "the cloud admin did not answer - add the User ID by hand")
        if email:
            # Our own directory first; the cloud API is asked only when we do not know the ID yet.
            from services.user_ref import resolve_email
            uid, status = resolve_email(email)
            if uid:
                return {"user_id": int(uid)}, None
            return None, ("user not found" if status == "not_found" else "the cloud admin did not answer - add the User ID by hand")
        user, err = cloud_api.find_user(user_id=user_id)
        if err:
            return None, err
        return user, None

    if atype == "cloud_sessions":
        # A user's latest streaming sessions (for the Testing Session picker). Game titles come from the
        # Game List mirror; the API itself only returns the application ID.
        from services import cloud_api
        rows, err = cloud_api.recent_sessions(payload.get("user_id"), limit=payload.get("limit") or 5,
                                              force=bool(payload.get("force")))
        if err:
            return None, err
        ids = sorted({str(r["application_id"]) for r in rows if r.get("application_id") is not None})
        titles, launchers = {}, {}
        try:
            for g in sync_db["game_list"].find({"_id": {"$in": ids}}, {"title": 1, "platform": 1}):
                titles[str(g["_id"])] = g.get("title") or ""
                launchers[str(g["_id"])] = str(g.get("platform") or "").strip()
        except Exception as e:
            logger.warning("[ConsoleActions] game title lookup failed: %s", e)
        for aid in ids:
            if not titles.get(aid):
                titles[aid] = _game_title(aid)
        for r in rows:
            aid = "" if r.get("application_id") is None else str(r["application_id"])
            r["application"] = titles.get(aid) or ("Application #%s" % aid if aid else "")
            r["launcher"] = launchers.get(aid, "")   # Game List "Platform" (Steam / Epic / Xbox ...), shown next to the title
        return {"sessions": rows}, None

    if atype == "reddit_archive":
        # Archive (close) a Reddit modmail conversation - the console "Done"/Resolved action.
        from reddit_notifications.mod_notes import archive_convo
        convo_id = (payload.get("convo_id") or "").strip()
        if not convo_id:
            return None, "convo_id required"
        _reddit_audit(convo_id, "Archived", action.get("author_name"))  # note first, while still open
        if _via_devvit():
            from reddit_notifications.devvit_inbox import send_reddit_command
            ok, err = send_reddit_command(action["_id"], "modmail_archive", conversationId=convo_id)
            if not ok:
                return None, err
            try:   # no API snapshot pass while blocked: record the archived state for the console now
                from services.reddit_snapshot import modmail_col
                modmail_col.update_one({"convo_id": convo_id}, {"$set": {"state": "archived"}})
            except Exception as e:
                logger.debug("[Console] snapshot archive mark failed for %s: %s", convo_id, e)
            return {"ok": True}, None
        ok = archive_convo(convo_id)
        return ({"ok": True}, None) if ok else (None, "reddit archive failed")

    if atype == "reddit_highlight":
        # Toggle a modmail conversation's highlight on Reddit + track it in reddit_mod_notes
        # (highlighted_convos) so the console shows the "linked" star. payload.highlight = bool.
        from reddit_notifications.mod_notes import highlight_convo, unhighlight_convo
        from services.mongo_db_service import (
            add_reddit_mod_note_highlighted_convo_sync,
            remove_reddit_mod_note_highlighted_convo_sync,
        )
        convo_id = (payload.get("convo_id") or "").strip()
        reddit_user = (payload.get("reddit_user") or "").strip()
        if not convo_id:
            return None, "convo_id required"
        want = bool(payload.get("highlight", True))
        if _via_devvit():
            from reddit_notifications.devvit_inbox import send_reddit_command
            ok, err = send_reddit_command(action["_id"], "modmail_highlight", conversationId=convo_id, highlight=want)
            if not ok:
                return None, err
        else:
            ok = highlight_convo(convo_id) if want else unhighlight_convo(convo_id)
        if not ok:
            return None, "reddit highlight failed"
        if reddit_user:
            if want:
                add_reddit_mod_note_highlighted_convo_sync(reddit_user, convo_id)
            else:
                remove_reddit_mod_note_highlighted_convo_sync(reddit_user, convo_id)
        _reddit_audit(convo_id, "Highlighted" if want else "Highlight removed", action.get("author_name"))
        return {"ok": True, "highlighted": want}, None

    if atype == "reddit_post_reply":
        # Public comment reply on a subreddit post (from the community account).
        from reddit_notifications.mod_notes import reply_to_post
        post_id = (payload.get("post_id") or "").strip()
        text = (payload.get("text") or "").strip()
        if not post_id or not text:
            return None, "post_id and text are required"
        ok = reply_to_post(post_id, text)
        return ({"ok": True}, None) if ok else (None, "reddit post reply failed")

    if atype == "jira_todo":
        # Console To-Do buttons (Forwarded / Closed / Done / Dismiss): the same function as the Slack buttons.
        from services.jira_todo import run
        res = run((payload.get("op") or "").strip(), payload.get("key"), rid=(payload.get("rid") or "").strip(),
                  since=payload.get("since") or "", by=action.get("author_name") or "console")
        return (res, None) if res.get("ok") else (None, res.get("error") or "failed")

    if atype == "discord_mention_email":
        # An email was added from the console for a Discord user: reflect it into the Slack thread(s)
        # of that user's moderator-mention notifications so Slack shows the newly-known email.
        did = str(payload.get("discord_user_id") or "").strip()
        email = (payload.get("email") or "").strip()
        if not did or not email:
            return None, "discord_user_id and email are required"
        try:
            from jira_notifications.jira_notifications import slack_client
            if not slack_client:
                return ({"ok": False, "note": "no slack client"}, None)
            n = 0
            for m in sync_db["discord_mentions"].find({"author_id": did, "slack_ts": {"$nin": [None, ""]}}):
                if m.get("email") or m.get("slack_id_posted"):   # already told this thread (old rows kept a copy)
                    continue
                try:
                    from services.user_ref import label_for_email
                    _label = label_for_email(email) if "@" in email else ("User ID %s" % email if email.isdigit() else None)
                    if not _label:
                        continue                       # nothing we may post: Slack never gets the address
                    slack_client.chat_postMessage(channel=m.get("slack_channel"), thread_ts=m.get("slack_ts"),
                                                  text="%s added via console" % _label)
                    # only a "posted" mark: the User ID itself lives in the directory, not on the mention
                    sync_db["discord_mentions"].update_one({"_id": m["_id"]}, {"$set": {"slack_id_posted": True}})
                    n += 1
                except Exception as _e:
                    logger.warning("ignored: %s", _e)
            return ({"ok": True, "updated": n}, None)
        except Exception as e:
            return None, "discord mention email sync failed: %s" % e

    if atype in ("reddit_note", "reddit_reply"):
        # reddit_note / reddit_reply(kind=note) -> mods-only INTERNAL reply (not shown to user).
        # reddit_reply(kind=user) -> PUBLIC reply, visible to the Reddit user.
        from reddit_notifications.mod_notes import post_internal_reply, post_public_reply
        convo_id = (payload.get("convo_id") or "").strip()
        text = (payload.get("text") or "").strip()
        if not convo_id or not text:
            return None, "convo_id and text are required"
        internal = atype == "reddit_note" or payload.get("kind") == "note"
        from reddit_notifications.devvit_inbox import send_reddit_command
        if _via_devvit():
            # API blocked: the Devvit app sends it from Reddit's side within ~1 minute. A failure comes back
            # as an ack that flips this action to "error".
            ok, err = send_reddit_command(action["_id"], "modmail_reply", conversationId=convo_id,
                                         body=text, isInternal=internal)
            if not ok:
                return None, err
        else:
            ok = (post_internal_reply if internal else post_public_reply)(convo_id, text)
        if ok:
            # Write-through (option 1): mirror the just-sent reply into the snapshot NOW so every
            # console session + a reload see it immediately, not after the next 5-min cycle. Zero
            # extra Reddit calls; best-effort (the snapshot still catches it if this fails).
            try:
                from services.reddit_snapshot import mirror_sent_reply
                mirror_sent_reply(convo_id, action.get("author_name") or "CMT", text, is_internal=internal)
            except Exception as e:
                logger.debug("[Console] reddit write-through failed for %s: %s", convo_id, e)
        if ok and not internal:   # attribute customer-facing public replies (mods-only notes self-document)
            _reddit_audit(convo_id, "Public reply sent", action.get("author_name"))
        return ({"ok": True}, None) if ok else (None, "reddit reply failed")

    if atype == "discord_reply":
        # Relay a mod reply to a Discord ticket: kind=user -> DM the user (like !say);
        # kind=note -> internal note in the ticket channel. Executes on the bot's Discord loop.
        import asyncio
        from bson import ObjectId
        from discord_notifications.discord_notifications import get_running_discord_bot
        ticket_id = (payload.get("ticket_id") or "").strip()
        text = (payload.get("text") or "").strip()
        is_note = payload.get("kind") == "note"
        mod_name = action.get("author_name") or "Console"
        if not ticket_id or not text:
            return None, "ticket_id and text are required"
        bot = get_running_discord_bot()
        if not bot or getattr(bot, "loop", None) is None or bot.is_closed():
            return None, "discord bot not ready"
        cog = bot.get_cog("ModMail")
        if not cog:
            return None, "modmail cog not loaded"
        try:
            oid = ObjectId(ticket_id)
        except Exception:
            return None, "bad ticket_id"
        ticket = sync_db["discord_tickets"].find_one({"_id": oid})
        if not ticket:
            return None, "ticket not found"
        fut = asyncio.run_coroutine_threadsafe(
            cog.send_console_reply(ticket, text, mod_name, is_note), bot.loop
        )
        try:
            ok = fut.result(timeout=30)
        except Exception as e:
            return None, "discord send failed: %s" % e
        return ({"ok": True}, None) if ok else (None, "discord send returned false")

    if atype == "discord_jira_link":
        # Make a console-initiated Jira link behave like the in-channel !jira command: post the
        # channel note + move the primary link to the Jira category + record the transcript note.
        # The console already added the key to the ticket doc; this only runs the Discord-side effects.
        import asyncio
        from bson import ObjectId
        from discord_notifications.discord_notifications import get_running_discord_bot
        ticket_id = (payload.get("ticket_id") or "").strip()
        issue_key = (payload.get("issue_key") or "").strip().upper()
        primary = bool(payload.get("primary"))
        if not ticket_id or not issue_key:
            return None, "ticket_id and issue_key are required"
        bot = get_running_discord_bot()
        if not bot or getattr(bot, "loop", None) is None or bot.is_closed():
            return None, "discord bot not ready"
        cog = bot.get_cog("ModMail")
        if not cog:
            return None, "modmail cog not loaded"
        try:
            oid = ObjectId(ticket_id)
        except Exception:
            return None, "bad ticket_id"
        ticket = sync_db["discord_tickets"].find_one({"_id": oid})
        if not ticket:
            return None, "ticket not found"
        fut = asyncio.run_coroutine_threadsafe(
            cog.console_link_jira(ticket, issue_key, primary), bot.loop
        )
        try:
            ok = fut.result(timeout=30)
        except Exception as e:
            return None, "discord jira link failed: %s" % e
        # ok=False means the channel wasn't resolvable; the Mongo link (written by the console) still
        # stands, so report success but note the channel miss so it's visible in the action log.
        return ({"ok": True, "note": None if ok else "channel not found; console link kept, no Discord note"}, None)

    if atype in ("discord_status", "discord_postpone"):
        # Change a Discord ticket's status / postpone it - runs on the bot's Discord loop, reusing
        # the same methods the !close / !postpone commands use.
        import asyncio
        from bson import ObjectId
        from discord_notifications.discord_notifications import get_running_discord_bot
        ticket_id = (payload.get("ticket_id") or "").strip()
        mod_name = action.get("author_name") or "Console"
        if not ticket_id:
            return None, "ticket_id required"
        bot = get_running_discord_bot()
        if not bot or getattr(bot, "loop", None) is None or bot.is_closed():
            return None, "discord bot not ready"
        cog = bot.get_cog("ModMail")
        if not cog:
            return None, "modmail cog not loaded"
        try:
            ticket = sync_db["discord_tickets"].find_one({"_id": ObjectId(ticket_id)})
        except Exception:
            return None, "bad ticket_id"
        if not ticket:
            return None, "ticket not found"
        channel = bot.get_channel(int(ticket["channel_id"])) if ticket.get("channel_id") else None

        if atype == "discord_postpone":
            if channel is None:
                return None, "ticket channel not found"
            duration = (payload.get("duration") or "").strip()
            reason = (payload.get("reason") or "").strip() or None
            fut = asyncio.run_coroutine_threadsafe(
                cog.postpone_ticket(channel, None, duration, reason, mod_display=mod_name), bot.loop)
            try:
                ok = fut.result(timeout=30)
            except Exception as e:
                return None, "discord postpone failed: %s" % e
            return ({"ok": True}, None) if ok else (None, "discord postpone returned false")

        # discord_status
        status = (payload.get("status") or "").strip().lower()
        if status == "closed":
            if channel is None:
                return None, "ticket channel not found"
            # Gentle scheduled close (1h, cancels on new activity) - same as the !close command.
            fut = asyncio.run_coroutine_threadsafe(cog.schedule_close_ticket(channel, None), bot.loop)
            try:
                fut.result(timeout=30)
            except Exception as e:
                return None, "discord close failed: %s" % e
            return {"ok": True, "status": "close_scheduled"}, None
        if status == "in_progress":
            sync_db["discord_tickets"].update_one({"_id": ticket["_id"]}, {"$set": {"status": "in_progress"}})
            return {"ok": True, "status": "in_progress"}, None
        return None, "unsupported status: %s" % status

    if atype == "maintenance_sheet_add":
        # Append one row to the maintenance Google Sheet for a console-added (Brazil) game maintenance.
        # The sheet secret lives on the bot, so the console enqueues this instead of calling directly.
        from services.maintenance_sheet_sync import push_maintenance_start
        game = (payload.get("game") or "").strip()
        if not game:
            return None, "game required"
        ok, err = push_maintenance_start(game, payload.get("maintenance_in") or "", payload.get("started_at"))
        if not ok:
            return None, err or "sheet append failed"
        return {"ok": True}, None

    if atype == "maintenance_end":
        # Admin forced a maintenance-end from the console. Same flow as a Mattermost maintenance end
        # (services/mm_maintenance.py), but the notices are flagged at once: post the Slack end message, flag the Discord/Reddit subscriptions for
        # their localized completion notice, then drop the active record (so it leaves the console
        # Maintenance tab). Best-effort per step so one failure doesn't strand the others.
        import asyncio
        from services.mongo_db_service import remove_active_maintenance
        entity = (payload.get("entity") or "").strip()
        mtype = (payload.get("type") or payload.get("mtype") or "").strip()
        if not entity or mtype not in ("game", "server"):
            return None, "entity and type (game|server) required"
        try:
            from services.maintenance_service import send_maintenance_message
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(send_maintenance_message(entity, False, mtype, None, None))
            finally:
                loop.close()
        except Exception as e:
            logger.error("[Console] maintenance_end Slack post failed for %s: %s", entity, e)
        try:
            from services.maintenance_notify_service import on_maintenance_ended
            on_maintenance_ended(entity, mtype)
        except Exception as e:
            logger.error("[Console] maintenance_end subscription flag failed for %s: %s", entity, e)
        remove_active_maintenance(entity, mtype)
        return {"ok": True}, None

    if atype == "maintenance_notify":
        # Subscribe a chat to the maintenance-ended notice + internal note: the same function as Slack.
        from services.maintenance_notify_service import add_maintenance_notice
        target = payload.get("target_type") or "discord"
        ref = payload.get("convo_id") if target == "reddit" else payload.get("ticket_id")
        return add_maintenance_notice(payload.get("entity"), payload.get("mtype") or "game", target, ref,
                                      payload.get("language") or "en", by=action.get("author_name") or "")

    if atype == "notify_test":
        # Send a test notification to the requesting user across their enabled + linked channels.
        # force=True so it fires regardless of the master switch / schedule (the point is to verify
        # the channels are actually wired up).
        from services.user_notify_service import notify_user
        email = (payload.get("user_email") or action.get("author_email") or "").strip()
        if not email:
            return None, "user_email required"
        result = notify_user(email, "CMT Console - test notification",
                             "If you can read this, your notifications are set up correctly.",
                             os.getenv("CONSOLE_URL", "https://cmtconsole.xyz/console/"), force=True)
        delivered = sorted(c for c, ok in (result or {}).items() if ok)
        # Report which channels actually delivered so the audit log shows a zero-delivery test.
        return {"ok": True, "delivered": delivered,
                "note": None if delivered else "no channel delivered (none enabled/linked, or all failed)"}, None

    if atype == "admin_notify_push":
        # Console-originated admin alert (new feedback / leave request). The console already wrote the
        # bell feed entry directly; here we only fan the PUSH out to subscribed admins across their
        # linked channels via notify_event, with feed=False so we do not duplicate the bell.
        from services.user_notify_service import notify_event
        et = (payload.get("event_type") or "").strip()
        if not et:
            return None, "event_type required"
        notify_event(et, payload.get("title") or "CMT Console",
                     payload.get("body") or "", route=payload.get("route") or "", feed=False)
        return {"ok": True}, None

    if atype == "notify_user_dm":
        # Targeted DM to one console user across their linked channels (used by leave approve/reject).
        # force=True so an important account event reaches them regardless of the quiet-hours schedule.
        from services.user_notify_service import notify_user
        email = (payload.get("user_email") or "").strip()
        if not email:
            return None, "user_email required"
        result = notify_user(email, payload.get("title") or "CMT Console",
                             payload.get("body") or "", payload.get("url") or "", force=True)
        return {"ok": True, "delivered": sorted(c for c, ok in (result or {}).items() if ok)}, None

    if atype == "report" and payload.get("title") and not payload.get("fields"):
        # Queued by the previous console build (title + body text): post it as it was.
        from services.mongo_db_service import client as slack_client
        channel = os.getenv("SLACK_REPORTS_CHANNEL_ID")
        if not channel:
            return None, "SLACK_REPORTS_CHANNEL_ID not configured"
        slack_client.chat_postMessage(channel=channel, text="*%s*\n%s" % (payload.get("title"), payload.get("body") or ""))
        return {"ok": True}, None

    if atype in ("jira_slack_delete", "urg_slack_delete"):
        # Queued by the previous console build: only the Slack card(s) to remove.
        key = (payload.get("key") or "").strip()
        if atype == "urg_slack_delete":
            from services.urg_recipients import delete_slack
            delete_slack(key, (payload.get("rid") or "").strip())
        else:
            from jira_notifications.jira_notifications import delete_slack_notification
            delete_slack_notification(key)
        return {"ok": True}, None

    if atype == "report":
        # Community / Weekly report: the same function as the Slack report forms.
        from services.team_forms import submit_report
        res, err = submit_report(payload.get("type") or "daily", payload.get("fields") or [],
                                 author_email=action.get("author_email", ""), author_name=action.get("author_name", ""))
        return (res, None) if res and res.get("slack") else (None, err)

    if atype == "create_so_ticket":
        # Create a SO Jira ticket from the console, identical to the Slack "Create Jira Ticket" modal.
        import base64
        from handlers.ticket_create import create_so_ticket
        files_by_block = {}
        for bid, flist in (payload.get("files_by_block") or {}).items():
            files_by_block[bid] = [{"name": f.get("name"), "mimetype": f.get("mimetype"),
                                    "content": base64.b64decode(f["b64"]) if f.get("b64") else b""}
                                   for f in (flist or [])]
        _vals = dict(payload.get("values") or {})
        if isinstance(payload.get("user_ref"), dict):
            _vals["_user_ref"] = payload["user_ref"]      # whose chat the ticket was opened from
        res = create_so_ticket(payload.get("topic", ""), _vals,
                               payload.get("author_email", ""), payload.get("author_display", ""), files_by_block)
        return (res, None) if res.get("ok") else (None, res.get("error") or "create failed")

    if atype == "create_urgent":
        # Create a URG ticket from the console, identical to the Slack "Create Urgent Request" modal
        # (including the 60-min dedup + Discord/Reddit auto-bind).
        import base64
        from handlers.ticket_create import create_urgent_ticket
        files = [{"name": f.get("name"), "mimetype": f.get("mimetype"),
                  "content": base64.b64decode(f["b64"]) if f.get("b64") else b""}
                 for f in (payload.get("files") or [])]
        _vals = dict(payload.get("values") or {})
        if isinstance(payload.get("user_ref"), dict):
            _vals["_user_ref"] = payload["user_ref"]
        own = None
        if payload.get("own_ticket") and str(payload.get("own_user_id") or "").isdigit():
            # "My Own Ticket": the console filled own_user_id from the signed-in member's profile.
            own = {"user_id": int(payload["own_user_id"]), "display": payload.get("author_display", "")}
        res = create_urgent_ticket(_vals, payload.get("author_email", ""),
                                   payload.get("author_display", ""), payload.get("author_slack_id", ""), files, own=own)
        return (res, None) if res.get("ok") else (None, res.get("error") or "create failed")

    if atype == "add_suggestion":
        # A game for the wishlist from the console: the same function as the Slack gaming proposal.
        from services.team_forms import submit_game_proposal
        author = (action.get("author_email") or "").split("@")[0] or (action.get("author_name") or "console")
        return submit_game_proposal(payload.get("entity"), payload.get("entity_type") or "Other", author,
                                    tg_msg_id=(payload.get("tg_msg_id") or "").strip(), source="console")

    if atype == "wishlist_set_status":
        # Console Wishlist tab: open / problematic / rejected / added, with a note.
        from services import wishlist_store
        by = action.get("author_name") or (action.get("author_email") or "").split("@")[0] or "console"
        try:
            game = wishlist_store.set_status(payload.get("key") or "", payload.get("status") or "",
                                             payload.get("note") or "", by=by)
        except ValueError as e:
            return None, str(e)
        return {"ok": True, "key": game["key"], "status": game["status"], "status_note": game.get("status_note", ""),
                "status_by": game.get("status_by", "")}, None

    if atype == "add_proposal":
        # A user proposal from the console: the same function as the Slack "Send user's proposal".
        from services.team_forms import submit_user_proposal
        return submit_user_proposal(payload.get("entity"), payload.get("entity_type") or "Proposal",
                                    action.get("author_name") or "console")

    if atype == "known_issues_add":
        # Append a new known-issue row to the team's Google Sheet (console "Add issue"), then the sync
        # service mirrors it back into Mongo so it shows in the console.
        from services.known_issues_sync import append_issue
        category = (payload.get("category") or "").strip().lower()
        fields = {"entity": (payload.get("entity") or "").strip(),
                  "problem": (payload.get("problem") or "").strip(),
                  "solution": (payload.get("solution") or "").strip(),
                  "link": (payload.get("link") or "").strip(),
                  "canned": (payload.get("canned") or "").strip()}
        if not (fields["problem"] or fields["solution"] or fields["canned"]):
            return None, "problem, solution or canned message required"
        ok, msg = append_issue(category, fields)
        return ({"ok": True}, None) if ok else (None, "known-issue add failed: %s" % msg)

    if atype == "known_issues_update":
        # Approved "Suggest edit": overwrite Problem / Solution / Canned of an existing sheet row.
        from services.known_issues_sync import update_row
        p = (payload.get("problem") or "").strip()
        if not p:
            return None, "problem required"
        ok, msg = update_row((payload.get("category") or "").strip().lower(), (payload.get("entity") or "").strip(),
                             p, payload.get("set") or {})
        return ({"ok": True}, None) if ok else (None, "known issue not updated: %s" % msg)

    if atype == "known_issues_set_canned":
        # Approved public suggestion: put a Canned Message into an EXISTING row that has none.
        from services.known_issues_sync import set_canned
        canned = (payload.get("canned") or "").strip()
        if not canned or not (payload.get("problem") or "").strip():
            return None, "problem and canned message required"
        ok, msg = set_canned((payload.get("category") or "").strip().lower(), (payload.get("entity") or "").strip(),
                             (payload.get("problem") or "").strip(), canned)
        return ({"ok": True}, None) if ok else (None, "canned message not saved: %s" % msg)

    if atype == "known_issues_sync":
        # Admin "Sync now" - pull the sheet into Mongo, then FORCE the Game List link write so the sheet
        # column refreshes even if the computed payload did not change (result surfaced for visibility).
        from services.known_issues_sync import sync_once
        ok, msg = sync_once()
        try:
            from services.game_list_sync import push_known_issues_links
            lok, lmsg = push_known_issues_links(force=True)
        except Exception as e:
            lok, lmsg = True, "skipped: %s" % e   # KI sync itself succeeded; don't fail it on a link hiccup
        return ({"ok": True, "detail": "%s; links: %s" % (msg, lmsg)}, None) if ok else (None, "sync failed: %s" % msg)

    if atype == "game_list_sync":
        # Admin "Sync now" - pull the Game List (ID <-> title) into Mongo, then FORCE-write the sheet's
        # Known-Issues links (bypass the change gate so a hand-cleared sheet is always rewritten). The
        # link-write result is surfaced so a misconfig (e.g. Apps Script not re-deployed with write_links)
        # is visible in the console instead of only the logs.
        from services.game_list_sync import sync_once, push_known_issues_links
        ok, msg = sync_once()
        try:
            lok, lmsg = push_known_issues_links(force=True)
        except Exception as e:
            lok, lmsg = False, str(e)
        detail = "%s; links: %s" % (msg, lmsg)
        if not lok:
            return None, "sheet links write failed: %s" % lmsg
        return ({"ok": True, "detail": detail}, None) if ok else (None, "sync failed: %s" % msg)

    if atype == "schedule_set":
        # Admin edits one schedule cell (time / VACATION / OFFLINE / blank); writes back to the sheet.
        # member_index (the column position from the mirror) is the primary target so a write always
        # hits the same column the console read/displays; the member name is a fallback.
        from services.schedule_sync import set_cell
        ok, msg = set_cell((payload.get("month") or "").strip(), (payload.get("date") or "").strip(),
                           (payload.get("member") or "").strip(), payload.get("value") or "",
                           member_index=payload.get("member_index"))
        return ({"ok": True}, None) if ok else (None, "schedule set failed: %s" % msg)

    if atype == "schedule_create_month":
        # Admin creates a new month tab (dates + weekdays auto-filled, members carried over).
        from services.schedule_sync import create_month
        ok, msg = create_month(payload.get("month"), payload.get("year"), payload.get("members") or [])
        return ({"ok": True, "name": msg}, None) if ok else (None, "create month failed: %s" % msg)

    if atype == "schedule_sync":
        from services.schedule_sync import sync_once
        ok, msg = sync_once()
        return ({"ok": True, "detail": msg}, None) if ok else (None, "sync failed: %s" % msg)

    if atype == "game_cards_generate":
        # Generate cards for a week from pasted Steam links (reuses the Slack steam_service generator).
        from services.game_cards_sync import generate
        ok, msg = generate((payload.get("week") or "").strip(), payload.get("links") or "",
                           payload.get("platform") or "Steam", payload.get("section") or "Library",
                           items=payload.get("items") if isinstance(payload.get("items"), list) else None)
        return ({"ok": True, "detail": msg}, None) if ok else (None, msg)

    if atype == "game_cards_set":
        from services.game_cards_sync import set_cell
        ok, msg = set_cell((payload.get("week") or "").strip(), payload.get("row_index"),
                           (payload.get("col") or "").strip(), payload.get("value") or "")
        return ({"ok": True}, None) if ok else (None, "card set failed: %s" % msg)

    if atype == "game_cards_set_row":
        from services.game_cards_sync import set_row
        ok, msg = set_row((payload.get("week") or "").strip(), payload.get("row_index"), payload.get("card") or {})
        return ({"ok": True}, None) if ok else (None, "card save failed: %s" % msg)

    if atype == "game_cards_delete":
        # Delete one card row (extra game / duplicate) from the week tab, then refresh Mongo.
        from services.game_cards_sync import delete_row
        ok, msg = delete_row((payload.get("week") or "").strip(), payload.get("row_index"))
        return ({"ok": True}, None) if ok else (None, "card delete failed: %s" % msg)

    if atype == "game_cards_create_week":
        from services.game_cards_sync import create_week
        ok, msg = create_week((payload.get("week") or "").strip())
        return ({"ok": True, "name": msg}, None) if ok else (None, "create week failed: %s" % msg)

    if atype == "game_testing_set":
        from services.game_testing_sync import set_cell as gt_set
        ok, msg = gt_set((payload.get("week") or "").strip(), payload.get("row"), (payload.get("col") or "").strip(),
                         payload.get("value") or "", (payload.get("game") or "").strip())
        return ({"ok": True}, None) if ok else (None, "testing set failed: %s" % msg)

    if atype == "game_testing_sync":
        from services.game_testing_sync import sync_once as gt_sync
        ok, msg = gt_sync()
        return ({"ok": True, "detail": msg}, None) if ok else (None, "sync failed: %s" % msg)

    if atype == "game_cards_sync":
        from services.game_cards_sync import sync_once
        ok, msg = sync_once()
        return ({"ok": True, "detail": msg}, None) if ok else (None, "sync failed: %s" % msg)

    if atype == "telegram_reply":
        # Reply to a Telegram user in the linked discussion group "as the group" (anonymous admin,
        # via MTProto). The telegram_messages doc carries the group chat_id + the user's message_id
        # to thread under.
        from telegram_bot.mtproto import send_group_reply, mtproto_ready
        msg_id = (payload.get("id") or "").strip()
        text = (payload.get("text") or "").strip()
        if not msg_id or not text:
            return None, "id and text are required"
        if not mtproto_ready():
            return None, "MTProto client not ready (TG_API_ID / TG_API_HASH / TG_MTPROTO_SESSION)"
        doc = sync_db["telegram_messages"].find_one({"_id": msg_id})
        if not doc:
            return None, "telegram message not found"
        try:
            ok = send_group_reply(doc.get("chat_id"), text,
                                  reply_to=int(doc["message_id"]) if doc.get("message_id") else None)
        except Exception as e:
            return None, "mtproto send failed: %s" % e
        if ok:
            sync_db["telegram_messages"].update_one(
                {"_id": doc["_id"]},
                {"$set": {"answered": True, "reply": text, "answered_at": datetime.utcnow(),
                          "answered_by": action.get("author_name") or "console"}})
            return {"ok": True}, None
        return None, "mtproto send returned false"

    return None, "unknown action type: %s" % atype


_FORM_ACTIONS = ("create_so_ticket", "create_urgent")
_FORM_PAYLOAD_KEYS = ("payload.values", "payload.files", "payload.files_by_block", "payload.user_ref")


def _process_one():
    """Claim one pending action atomically and run it. Returns True if one was processed."""
    action = actions_col.find_one_and_update(
        {"status": "pending"},
        {"$set": {"status": "processing", "claimed_at": datetime.utcnow()}},
        sort=[("created_at", 1)],
    )
    if not action:
        return False
    try:
        from services.action_policy import check as _policy_check
        denied = _policy_check(action, sync_db["console_users"])
    except Exception as e:
        denied = "authorization check failed: %s" % e
    try:
        if denied:
            logger.warning("[ConsoleActions] %s %s refused: %s", action.get("type"), action["_id"], denied)
            result, error = None, "refused: %s" % denied
        else:
            result, error = _handle(action)
    except Exception as e:
        logger.error("[ConsoleActions] handler crashed for %s: %s", action.get("type"), e)
        result, error = None, "handler crashed: %s" % e
    finished = datetime.utcnow()
    retain = ERROR_RETAIN_DAYS if error else DONE_RETAIN_DAYS
    update = {"$set": {"status": "error" if error else "done", "result": result, "error": error,
                       "finished_at": finished, "expire_at": finished + timedelta(days=retain)}}
    if action.get("type") in _FORM_ACTIONS:
        # The ticket form (user data, base64 files) is not kept once the ticket exists or failed.
        update["$unset"] = {k: "" for k in _FORM_PAYLOAD_KEYS}
    actions_col.update_one({"_id": action["_id"]}, update)
    log = logger.error if error else logger.info
    log("[ConsoleActions] %s %s -> %s%s", action.get("type"), action["_id"],
        "error" if error else "done", (": " + str(error)) if error else "")
    return True


# New actions wake the worker through a Mongo change stream instead of a query every 3 s (~29k
# queries a day, mostly empty). The watcher sets _wake on every insert; the worker drains the queue
# and then sleeps until woken. If change streams are unavailable, it falls back to polling.
_wake = threading.Event()
_stream_ok = threading.Event()


def _watch():
    """Wake the worker whenever the console enqueues an action. Reconnects on errors."""
    failures = 0
    while True:
        try:
            with actions_col.watch([{"$match": {"operationType": "insert"}}], max_await_time_ms=60000) as stream:
                if not _stream_ok.is_set():
                    logger.info("[ConsoleActions] change stream connected: actions run as soon as they are queued")
                _stream_ok.set()
                failures = 0
                _wake.set()   # catch anything queued while the stream was down
                for _change in stream:
                    _wake.set()
        except Exception as e:
            _stream_ok.clear()
            failures += 1
            if failures == 1:
                logger.warning("[ConsoleActions] change stream unavailable (%s) - polling every %ss until it is back",
                               type(e).__name__, POLL_SECONDS)
            time.sleep(min(300, 5 * failures))   # back off: 5 s, 10 s ... up to 5 min between attempts


def _loop():
    logger.info("[ConsoleActions] worker started")
    while True:
        try:
            while _process_one():  # drain the queue, then wait for the next one
                pass
        except Exception as e:
            logger.error("[ConsoleActions] worker loop error: %s", e)
        _wake.wait(SAFETY_POLL_SECONDS if _stream_ok.is_set() else POLL_SECONDS)
        _wake.clear()


def _ensure_indexes():
    # TTL index: Mongo deletes each doc once now >= its `expire_at` (set on finish). Docs
    # without `expire_at` (pending/processing) are never auto-deleted.
    try:
        actions_col.create_index("expire_at", expireAfterSeconds=0)
    except Exception as e:
        logger.error("[ConsoleActions] failed to ensure TTL index: %s", e)


def _close_interrupted():
    """Actions still "processing" at startup were cut off by the previous bot run. Re-running them could
    send a reply or create a ticket twice, so they are closed as an error for a person to check."""
    try:
        now = datetime.utcnow()
        res = actions_col.update_many(
            {"status": "processing"},
            {"$set": {"status": "error", "finished_at": now,
                      "error": "Interrupted by a bot restart. Check whether it went through before trying again.",
                      "expire_at": now + timedelta(days=ERROR_RETAIN_DAYS)}})
        if res.modified_count:
            logger.warning("[ConsoleActions] %d action(s) interrupted by the restart were marked as error",
                           res.modified_count)
    except Exception as e:
        logger.error("[ConsoleActions] could not close interrupted actions: %s", e)


def start_console_actions_worker():
    """Start the polling worker on a daemon thread. Call once at bot startup."""
    _ensure_indexes()
    _close_interrupted()
    threading.Thread(target=_watch, daemon=True, name="console-actions-watch").start()
    t = threading.Thread(target=_loop, daemon=True, name="console-actions-worker")
    t.start()
    return t
