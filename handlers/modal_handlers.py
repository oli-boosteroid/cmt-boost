import io
import logging
import json
import re
import shutil
import tempfile
import threading
import zipfile

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from services.mongo_db_service import save_information_sync, save_reddit_user_email_sync, save_discord_user_email_sync, save_facebook_author_email_sync, get_email_by_reddit_username_sync, get_email_by_discord_user_id_sync, get_email_by_facebook_author, search_discord_tickets_by_user_sync
from services.redact import mask_email
from services.steam_service import fetch_game_data, scrape_steam_page, extract_game_info, download_screenshots
from services.boosteroid_catalog_service import catalog_loaded
from utils.general_utils import extract_steam_app_id, detect_store
from utils.session_parser import parse_session_blocks
import requests
from dotenv import load_dotenv
from pathlib import Path
import os

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

logger = logging.getLogger(__name__)

SLACK_MAINTENANCE_API_URL = os.getenv('SLACK_BOT_API_MAINTENANCE_URL')



def _saved_msg(value, target):
    """Honest confirmation for an "add user" modal: what was actually stored for `target`."""
    try:
        from services.mongo_db_service import ident_status
        uid, status = ident_status(value)
    except Exception:
        uid, status = None, "error"
    if status == "found":
        return f"User ID {uid} saved for {target}."
    if status == "not_found":
        return f"Nothing saved for {target}: no Boosteroid account matches what was entered."
    if status == "error":
        return (f"Nothing saved for {target}: the User ID could not be looked up (the cloud admin did not answer). "
                "Please add the User ID by hand.")
    return f"Nothing saved for {target}: enter a User ID, an email or a session ID."

def _dm(client, user_id, text):
    try:
        client.chat_postMessage(channel=user_id, text=text)
    except Exception as e:
        logger.error(f"[Slack] Failed to DM user {user_id}: {e}")


def _process_maintenance_submit(body, client, view, kind):
    """Game/server maintenance modal: subscribe the Discord ticket / Reddit ModMail chat to the
    maintenance-ended notice - the same function as the console (add_maintenance_notice)."""
    from services.jira_service import parse_discord_channel_id, parse_reddit_convo_id
    from services.maintenance_notify_service import add_maintenance_notice

    values = view["state"]["values"]
    user_id = body["user"]["id"]

    def _sel(block, action):
        try:
            return values[block][action]["selected_option"]["value"]
        except Exception:
            return None

    delivery_type = _sel("delivery_type_block", "notification_delivery_type")
    entity_block = "send_notification_game_block" if kind == "game" else "send_notification_server_block"
    entity_action = "notification_game_input" if kind == "game" else "notification_server_input"
    entity = _sel(entity_block, entity_action)
    language = _sel("send_notification_language_block", "notification_language_input") or "en"

    if delivery_type not in ("discord", "reddit"):
        _dm(client, user_id, "Please choose a Type (Discord / Reddit).")
        return
    if not entity or entity == "__none__":
        _dm(client, user_id, f"Please select a {kind} in maintenance.")
        return
    try:
        link = (values["send_notification_link_block"]["notification_link_input"]["value"] or "").strip()
    except Exception:
        link = ""
    ref = parse_discord_channel_id(link) if delivery_type == "discord" else parse_reddit_convo_id(link)
    if not ref:
        _dm(client, user_id, "Invalid Discord ticket URL." if delivery_type == "discord" else "Invalid Reddit ModMail URL.")
        return
    res, err = add_maintenance_notice(entity, kind, delivery_type, str(ref), language,
                                      by=body["user"].get("name") or user_id)
    _dm(client, user_id,
        f"Subscribed {delivery_type} to maintenance end for `{entity}` ({language})." if not err
        else f"Failed to subscribe `{entity}`: {err}")


def register_modal_handlers(app):
    @app.view("add_information_modal_submit")
    def handle_add_information_submission(ack, body, client, view):
        ack()

        try:
            values = view["state"]["values"]
            selected_options = values["tag_block"]["tag_input"].get("selected_options", [])
            tags = [option["value"] for option in selected_options]
            ticket_url = values["ticket_url_block"]["ticket_url_input"].get("value", "")
            post_sender = body['user']['name']

            private_metadata = view.get("private_metadata", "{}")
            post_data = json.loads(private_metadata) if private_metadata else {}

            information_data = {
                "tags": tags,
                "created_by": post_sender,
                "platform": post_data.get("platform", "Reddit"),
                "post_url": post_data.get("post_url", ""),
                "ticket_url": ticket_url,
            }

            save_information_sync(information_data)

            logger.info(f"[Slack] Information saved with post data: {information_data}")

            tags_text = ", ".join(tags) if tags else "No tags selected"

            try:
                platform = post_data.get("platform", "Reddit")
                post_url_val = post_data.get("post_url", "")
                client.chat_postMessage(
                    channel=body["user"]["id"],
                    text=f"*Information saved successfully!*\n\n"
                         f"Tags: `{tags_text}`\n"
                         f"Post ({platform}): {post_url_val or 'N/A'}\n"
                         f"Ticket: {ticket_url or 'N/A'}"
                )
            except Exception as e:
                logger.error(f"[Slack] Error sending confirmation: {e}")

        except Exception as e:
            logger.error(f"[Slack] Error handling modal submission: {e}", exc_info=True)

    @app.view("add_testing_session_submit")
    def handle_testing_session_submission(ack, body, client, view):
        values = view["state"]["values"]

        session_info_raw = (values["session_block"]["session_input"].get("value") or "").strip()

        # The same parsing and rules as the console (services/testing_sessions.py).
        from services import testing_sessions
        parsed_sessions, _perr = testing_sessions.parse(session_info_raw)
        if _perr:
            ack(response_action="errors", errors={"session_block": _perr})
            logger.warning("[Slack] Testing session rejected - bad session format")
            return

        ack()
        try:
            ticket_url = values["ticket_url_block"]["ticket_url_input"].get("value", "")
            environment = values["environment_block"]["environment_input"]["selected_option"]["value"]
            issue = values["issue_block"]["issue_input"]["selected_option"]["value"]
            result = (values.get("result_block", {}).get("result_input", {}).get("value") or "")
            selected_options = values.get("testing_tags_block", {}).get("testing_tags_input", {}).get("selected_options", [])
            testing_tags = [option["value"] for option in selected_options]

            slack_user_id = body["user"]["id"]

            try:
                user_info = client.users_info(user=slack_user_id)
                email = user_info["user"]["profile"].get("email", "")
                created_by = email.split("@")[0] if email else slack_user_id
            except Exception as e:
                logger.warning(f"[Slack] Could not fetch email for user {slack_user_id}: {e}")
                created_by = slack_user_id

            from services.mongo_db_service import sync_testing_sessions_collection
            ids, _err = testing_sessions.add(sync_testing_sessions_collection, session_info_raw, environment, issue,
                                             result, ticket_url, testing_tags, created_by)
            saved = [(mid, game, sid) for mid, (sid, game) in zip(ids, parsed_sessions)]
            logger.info(f"[Slack] {len(saved)} testing session(s) saved by {created_by}")

            try:
                count = len(saved)
                summary_lines = "\n".join(
                    f"*{i+1}.* `{game}` / `{sid}`"
                    for i, (_, game, sid) in enumerate(saved)
                )
                msg_blocks = [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*{count} testing session{'s' if count > 1 else ''} saved successfully!*\n\n"
                                f"{summary_lines}\n\n"
                                f"Environment: `{environment}` | Issue: `{issue}`"
                                + (f" | Result: `{result}`" if result else "")
                            )
                        }
                    }
                ]
                edit_buttons = [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": f"Edit {sid[:8]}"},
                        "action_id": f"edit_testing_session_button_{i}",
                        "value": mongo_id,
                    }
                    for i, (mongo_id, game, sid) in enumerate(saved)
                    if mongo_id
                ]
                for chunk_start in range(0, len(edit_buttons), 5):
                    msg_blocks.append({"type": "actions", "elements": edit_buttons[chunk_start:chunk_start + 5]})

                client.chat_postMessage(
                    channel=slack_user_id,
                    text=f"{count} testing session{'s' if count > 1 else ''} saved.",
                    blocks=msg_blocks,
                )
            except Exception as e:
                logger.error(f"[Slack] Error sending testing confirmation: {e}")
        except Exception as e:
            logger.error(f"[Slack] Error handling testing session submission: {e}")

    # Proposals and reports: the same functions as the console (services/team_forms.py).
    def _slack_email_alias(client, user_id):
        try:
            email = client.users_info(user=user_id)["user"]["profile"].get("email", "")
        except Exception as e:
            logger.warning(f"[Slack] Could not fetch email for user {user_id}: {e}")
            email = ""
        return email, (email.split("@")[0] if email else user_id)

    def _val(values, block, action):
        return (values.get(block, {}).get(action, {}).get("value") or "").strip()

    @app.view("send_game_proposal_submit")
    def handle_game_proposal_view(ack, body, client, view):
        ack()
        user_id = body["user"]["id"]
        try:
            from services.team_forms import submit_game_proposal
            values = view["state"]["values"]
            game = _val(values, "send_game_block", "game_input")
            platform = values["send_game_platform_block"]["send_game_platform_input"]["selected_option"]["value"]
            _email, author = _slack_email_alias(client, user_id)
            res, err = submit_game_proposal(game, platform, author, discord_url=view.get("private_metadata", ""))
            _dm(client, user_id, f"Your game proposal was sent: `{game}`." if not err else f"Game proposal failed: {err}")
        except Exception as e:
            logger.error(f"[Slack] Error handling game proposal submission: {e}", exc_info=True)

    @app.view("send_proposal_submit")
    def handle_proposal_view(ack, body, client, view):
        ack()
        user_id = body["user"]["id"]
        try:
            from services.team_forms import submit_user_proposal
            values = view["state"]["values"]
            proposal = _val(values, "send_proposal_text_block", "proposal_input")
            ptype = values["send_proposal_type_block"]["send_proposal_type_input"]["selected_option"]["value"]
            res, err = submit_user_proposal(proposal, ptype, body["user"].get("name") or user_id)
            _dm(client, user_id, f"Your proposal was sent: `{proposal}`." if not err else f"Proposal failed: {err}")
        except Exception as e:
            logger.error(f"[Slack] Error handling proposal submission: {e}", exc_info=True)

    def _submit_report(body, client, rtype, values_list):
        from services.team_forms import submit_report, REPORT_FIELDS
        user_id = body["user"]["id"]
        email, alias = _slack_email_alias(client, user_id)
        fields = [[label, value] for label, value in zip(REPORT_FIELDS[rtype], values_list)]
        res, err = submit_report(rtype, fields, author_email=email, author_name=body["user"].get("name") or alias)
        if res and res.get("slack"):
            _dm(client, user_id, f"*Your report has been sent!*\n\n*{res['title']}*\n{res['body']}")
        else:
            _dm(client, user_id, f"Report saved, but not posted to Slack: {err}")

    @app.view("send_community_report_submit")
    def handle_community_report_submit(ack, body, client, view):
        ack()
        try:
            v = view["state"]["values"]
            _submit_report(body, client, "daily", [
                _val(v, "send_community_issues_summary_block", "community_report_issues_summary_input"),
                _val(v, "community_tg_posts_sent_block", "community_tg_posts_sent_input"),
                _val(v, "send_community_testing_sessions_count_block", "community_testing_sessions_count_input"),
                _val(v, "send_community_testing_sessions_summary_block", "community_testing_sessions_summary_input"),
                _val(v, "send_community_crypto_block", "community_crypto_input"),
                _val(v, "send_community_summary_block", "community_summary_input")])
        except Exception as e:
            logger.error(f"[Slack] Error handling community report submission: {e}", exc_info=True)

    @app.view("send_weekly_report_submit")
    def handle_weekly_report_submit(ack, body, client, view):
        ack()
        try:
            v = view["state"]["values"]
            _submit_report(body, client, "weekly", [
                _val(v, "weekly_technical_problem_block", "weekly_technical_problem_input"),
                _val(v, "weekly_non_technical_block", "weekly_non_technical_input"),
                _val(v, "weekly_jira_tickets_block", "weekly_jira_tickets_input"),
                _val(v, "weekly_crypto_block", "weekly_crypto_input"),
                _val(v, "weekly_other_block", "weekly_other_input")])
        except Exception as e:
            logger.error(f"[Slack] Error handling weekly report submission: {e}", exc_info=True)

    @app.view("send_maintenance_notification_submit")
    def handle_maintenance_notification_submit(ack, body, client, view):
        ack()
        _process_maintenance_submit(body, client, view, kind="game")

    @app.view("send_server_maintenance_notification_submit")
    def handle_server_maintenance_notification_submit(ack, body, client, view):
        ack()
        _process_maintenance_submit(body, client, view, kind="server")

    @app.view("send_custom_user_issue_submit")
    def handle_send_custom_user_issue_submit(ack, body, client, view):
        ack()
        try:
            values = view["state"]["values"]

            issue_url = values["send_custom_user_issue_post_url_block"]["send_custom_user_issue_post_url_action"]["value"]
            issue_email = (values.get("send_custom_user_issue_email_block", {})
                           .get("send_custom_user_issue_email_action", {})
                           .get("value") or "").strip()
            selected_options = values["send_custom_user_issue_tags_block"]["send_custom_user_issue_tags_action"].get("selected_options", [])
            issue_tags = [option["value"] for option in selected_options]
            issue_ticket_url = values["send_custom_user_issue_ticket_url_block"]["send_custom_user_issue_ticket_url_action"].get("value", "")
            issue_platform = values["send_custom_user_issue_platform_block"]["send_custom_user_issue_platform_action"]["selected_option"]["value"]
            issue_sender = body['user']['name']

            issue_data = {
                "post_url": issue_url,
                "tags": issue_tags,
                "ticket_url": issue_ticket_url,
                "platform": issue_platform,
                "created_by": issue_sender
            }

            save_information_sync(issue_data)
            logger.info(f"[Slack] Custom user issue data saved with data: {issue_data}")

            # Save email to DB if provided
            facebook_name = (values.get("send_custom_user_issue_facebook_name_block", {})
                             .get("send_custom_user_issue_facebook_name_action", {})
                             .get("value") or "").strip()

            if issue_email:
                try:
                    if issue_platform == "Reddit" and issue_url:
                        m = re.search(r'reddit\.com/user/([^/?\s]+)', issue_url)
                        if m:
                            save_reddit_user_email_sync(m.group(1), issue_email)
                    elif issue_platform == "Discord" and issue_url:
                        m = re.search(r'discord\.com/users/(\d+)', issue_url)
                        if m:
                            save_discord_user_email_sync(int(m.group(1)), "", issue_email)
                    elif issue_platform == "Facebook":
                        author = facebook_name or (issue_url.rstrip("/").split("/")[-1].split("?")[0] if issue_url else "")
                        if author:
                            save_facebook_author_email_sync(author, issue_email)
                except Exception as e:
                    logger.error(f"[Slack] Failed to save email for custom issue: {e}")

            try:
                client.chat_postMessage(
                    channel=body["user"]["id"],
                    text=f"Custom user issue was sent!\n\n"
                         f"Post url: {issue_url}\n"
                         f"Selected tags: {issue_tags}\n"
                         f"Ticket: {issue_ticket_url}\n"
                         f"Platform: {issue_platform}"
                )
                logger.info("[Slack] Custom user issue notification was sent.")
            except Exception as e:
                logger.error(f"[Slack] Custom user issue wasn't sent. Error: {e}")
        except Exception as e:
            logger.error(f"[Slack] Custom issue user wasn't saved. Error: {e}", exc_info=True)

    @app.view("search_email_by_profile_url_submit")
    def handle_search_email_by_profile_url_submit(ack, body, client, view):
        ack()
        try:
            values = view["state"]["values"]
            url = (values.get("search_email_url_block", {})
                   .get("search_email_url_action", {})
                   .get("value") or "").strip()
            facebook_name = (values.get("search_email_facebook_name_block", {})
                             .get("search_email_facebook_name_action", {})
                             .get("value") or "").strip()
            channel_id = view.get("private_metadata", "")
            user_id = body["user"]["id"]

            email = None
            emails = None
            identifier = None

            if facebook_name:
                identifier = facebook_name
                emails = get_email_by_facebook_author(facebook_name)
            elif url:
                m_reddit = re.search(r'reddit\.com/user/([^/?\s]+)', url)
                m_discord = re.search(r'discord\.com/users/(\d+)', url)

                if m_reddit:
                    identifier = f"u/{m_reddit.group(1)}"
                    email = get_email_by_reddit_username_sync(m_reddit.group(1))
                elif m_discord:
                    identifier = f"Discord user {m_discord.group(1)}"
                    email = get_email_by_discord_user_id_sync(int(m_discord.group(1)))
                else:
                    if channel_id:
                        client.chat_postEphemeral(
                            channel=channel_id,
                            user=user_id,
                            text="Unrecognised URL. Use Reddit or Discord profile links, or enter a Facebook Name."
                        )
                    return
            else:
                if channel_id:
                    client.chat_postEphemeral(
                        channel=channel_id,
                        user=user_id,
                        text="Please enter a Profile URL or a Facebook Name."
                    )
                return

            if emails is not None:
                if emails:
                    email_display = '\n'.join(f'• `{e}`' for e in emails)
                    text = f"*{identifier}*:\n{email_display}"
                else:
                    text = f"No User ID found for *{identifier}*"
            elif email:
                text = f"*{identifier}*: `{email}`"
            else:
                text = f"No User ID found for *{identifier}*"

            if channel_id:
                client.chat_postEphemeral(channel=channel_id, user=user_id, text=text)
            else:
                client.chat_postMessage(channel=user_id, text=text)

            logger.info(f"[Slack] Email search result for {identifier}: {emails or email}")
        except Exception as e:
            logger.error(f"[Slack] Error in search_email_by_profile_url_submit: {e}", exc_info=True)

    @app.view("add_discord_modmail_information_submit")
    def handle_add_discord_modmail_information_submission(ack, body, client, view):
        ack()
        try:
            values = view["state"]["values"]
            selected_options = values["tag_block"]["tag_input"].get("selected_options", [])
            tags = [option["value"] for option in selected_options]
            ticket_url = values["ticket_url_block"]["ticket_url_input"].get("value", "") or ""
            post_sender = body['user']['name']
            post_url = view.get("private_metadata", "") or ""

            information_data = {
                "tags": tags,
                "created_by": post_sender,
                "platform": "Discord",
                "post_url": post_url,
                "ticket_url": ticket_url,
            }

            save_information_sync(information_data)
            logger.info(f"[Slack] Discord modmail information saved: {information_data}")

            try:
                client.chat_postMessage(
                    channel=body["user"]["id"],
                    text=f"Information saved!\nTags: `{', '.join(tags) or 'None'}`\nDiscord: {post_url or 'N/A'}\nTicket: {ticket_url or 'N/A'}"
                )
            except Exception as e:
                logger.error(f"[Slack] Error sending Discord modmail info confirmation: {e}")
        except Exception as e:
            logger.error(f"[Slack] Error handling Discord modmail info submission: {e}", exc_info=True)

    @app.view("add_email_manual_submit")
    def handle_add_email_manual_submit(ack, body, client, view):
        ack()
        try:
            values = view["state"]["values"]
            email = (values["add_email_manual_email_block"]["add_email_manual_email_action"].get("value") or "").strip()
            url = (values["add_email_manual_url_block"]["add_email_manual_url_action"].get("value") or "").strip()

            platform = None
            label = ""
            m = re.search(r'discord\.com/users/(\d+)', url)
            if m:
                platform = "Discord"
                save_discord_user_email_sync(int(m.group(1)), "", email)
                label = f"Discord user `{m.group(1)}`"
            else:
                m = re.search(r'reddit\.com/user/([^/?\s]+)', url)
                if m:
                    platform = "Reddit"
                    save_reddit_user_email_sync(m.group(1), email)
                    label = f"Reddit user u/{m.group(1)}"
                elif "facebook.com" in url or "fb.com" in url:
                    platform = "Facebook"
                    author = url.rstrip("/").split("/")[-1].split("?")[0]
                    save_facebook_author_email_sync(author, email)
                    label = f"Facebook user {author}"

            if platform:
                logger.info(f"[Slack] Manual email save — {platform} — {mask_email(email)} — {url}")
                try:
                    client.chat_postMessage(
                        channel=body["user"]["id"],
                        text=_saved_msg(email, f"{label}"),
                    )
                except Exception as e:
                    logger.error(f"[Slack] Failed to send Add Email confirmation: {e}")
            else:
                logger.warning(f"[Slack] Add Email manual — unknown platform for URL: {url}")
                try:
                    client.chat_postMessage(
                        channel=body["user"]["id"],
                        text=f"Could not detect platform from URL: `{url}`\nSupported: discord.com/users/..., reddit.com/user/..., facebook.com/...",
                    )
                except Exception as e:
                    logger.error(f"[Slack] Failed to send Add Email error message: {e}")
        except Exception as e:
            logger.error(f"[Slack] Error handling Add Email manual submission: {e}", exc_info=True)

    @app.view("add_discord_user_email_submit")
    def handle_add_discord_user_email_submit(ack, body, client, view):
        ack()
        try:
            email = view["state"]["values"]["discord_email_block"]["discord_email_input"]["value"]
            raw = view["private_metadata"]  # "{user_id}:{username}"
            user_id_str, username = raw.split(":", 1)

            save_discord_user_email_sync(int(user_id_str), username, email)
            logger.info(f"[Slack] Email saved for Discord user {username} ({user_id_str}): {mask_email(email)}")

            try:
                client.chat_postMessage(
                    channel=body["user"]["id"],
                    text=_saved_msg(email, f"Discord user *{username}*")
                )
            except Exception as e:
                logger.error(f"[Slack] Error sending Add Discord Email confirmation: {e}")
        except Exception as e:
            logger.error(f"[Slack] Error handling add Discord user email submission: {e}", exc_info=True)

    @app.view("add_reddit_user_email_submit")
    def handle_add_reddit_user_email_submit(ack, body, client, view):
        ack()
        try:
            email = view["state"]["values"]["reddit_email_block"]["reddit_email_input"]["value"]
            reddit_username = view["private_metadata"]

            save_reddit_user_email_sync(reddit_username, email)
            logger.info(f"[Slack] Email saved for Reddit user u/{reddit_username}: {mask_email(email)}")

            try:
                client.chat_postMessage(
                    channel=body["user"]["id"],
                    text=_saved_msg(email, f"Reddit user u/{reddit_username}")
                )
            except Exception as e:
                logger.error(f"[Slack] Error sending Add Reddit Email confirmation: {e}")
        except Exception as e:
            logger.error(f"[Slack] Error handling add Reddit user email submission: {e}", exc_info=True)

    @app.view("add_reddit_jira_ticket_submit")
    def handle_add_reddit_jira_ticket_submit(ack, body, client, view):
        ack()
        try:
            reddit_username, _, convo_id = view["private_metadata"].partition(":")
            raw = (view["state"]["values"]["reddit_jira_block"]["reddit_jira_input"]["value"] or "").strip()
            m = re.search(r'([A-Za-z][A-Za-z0-9]*-\d+)', raw)
            slack_user_id = body["user"]["id"]
            if not m:
                client.chat_postMessage(
                    channel=slack_user_id,
                    text=f"Invalid Jira key: `{raw}`. Expected something like `SO-12345`.",
                )
                return
            issue_key = m.group(1).upper()

            def _do_link():
                try:
                    from reddit_notifications.mod_notes import link_jira_ticket
                    ok = link_jira_ticket(reddit_username, issue_key, convo_id=convo_id or None)
                    msg = (
                        f"Linked Jira `{issue_key}` to Reddit user u/{reddit_username}."
                        if ok else
                        f"Failed to link Jira `{issue_key}` to Reddit user u/{reddit_username}."
                    )
                    client.chat_postMessage(channel=slack_user_id, text=msg)
                except Exception as e:
                    logger.error(f"[Slack] Reddit Jira link failed for u/{reddit_username} {issue_key}: {e}")

            threading.Thread(target=_do_link, daemon=True, name="reddit-jira-link").start()
            logger.info(f"[Slack] Reddit Jira link requested: u/{reddit_username} → {issue_key}")
        except Exception as e:
            logger.error(f"[Slack] Error handling add Reddit Jira ticket submission: {e}", exc_info=True)

    @app.view("add_facebook_author_email_submit")
    def handle_add_facebook_author_email_submit(ack, body, client, view):
        ack()
        try:
            email = view["state"]["values"]["facebook_email_block"]["facebook_email_input"]["value"]
            author_name = view["private_metadata"]

            save_facebook_author_email_sync(author_name, email)
            logger.info(f"[Slack] Email saved for Facebook author {author_name}: {mask_email(email)}")

            try:
                client.chat_postMessage(
                    channel=body["user"]["id"],
                    text=_saved_msg(email, f"Facebook author *{author_name}*")
                )
            except Exception as e:
                logger.error(f"[Slack] Error sending Add Facebook Email confirmation: {e}")
        except Exception as e:
            logger.error(f"[Slack] Error handling add Facebook author email submission: {e}", exc_info=True)

    @app.view("edit_testing_session_submit")
    def handle_edit_testing_session_submission(ack, body, client, view):
        values = view["state"]["values"]

        session_info_raw = (values["session_block"]["session_input"].get("value") or "").strip()
        from services import testing_sessions
        _parsed, _perr = testing_sessions.parse(session_info_raw)
        if _perr:
            ack(response_action="errors", errors={"session_block": _perr})
            logger.warning("[Slack] Edit testing session rejected - bad session format")
            return
        session_id, game = _parsed[0]

        ack()
        try:
            mongo_id = view.get("private_metadata", "")

            result = (values.get("result_block", {}).get("result_input", {}).get("value") or "")
            ticket_url = values["ticket_url_block"]["ticket_url_input"].get("value", "")
            environment = values["environment_block"]["environment_input"]["selected_option"]["value"]
            issue = values["issue_block"]["issue_input"]["selected_option"]["value"]
            selected_options = values.get("testing_tags_block", {}).get("testing_tags_input", {}).get("selected_options", [])
            testing_tags = [option["value"] for option in selected_options]

            from services.mongo_db_service import sync_testing_sessions_collection
            _doc, _err = testing_sessions.update(sync_testing_sessions_collection, mongo_id, session_info_raw, environment,
                                                 issue, result, ticket_url, testing_tags,
                                                 updated_by="slack:" + (body["user"].get("name") or body["user"]["id"]))
            if _err:
                _dm(client, body["user"]["id"], f"Testing session not updated: {_err}")
                return
            logger.info(f"[Slack] Testing session updated: {mongo_id}")

            try:
                client.chat_postMessage(
                    channel=body["user"]["id"],
                    text=f"*Testing session updated!*\n\n"
                         f"Game: `{game}`\n"
                         f"Session: `{session_id}`\n"
                         f"Environment: `{environment}`\n"
                         f"Issue: `{issue}`\n"
                         f"Result: `{result or '—'}`"
                )
            except Exception as e:
                logger.error(f"[Slack] Error sending edit confirmation: {e}")
        except Exception as e:
            logger.error(f"[Slack] Error handling edit testing session submission: {e}", exc_info=True)

    # -----------------------------------------------------------------------
    # Search Discord Tickets
    # -----------------------------------------------------------------------
    @app.view("search_discord_tickets_submit")
    def handle_search_discord_tickets_submit(ack, body, client, view):
        ack()
        try:
            query = (
                view["state"]["values"]
                .get("discord_search_block", {})
                .get("discord_search_input", {})
                .get("value") or ""
            ).strip()

            channel_id = view.get("private_metadata", "")
            slack_user_id = body["user"]["id"]

            if not query:
                client.chat_postEphemeral(
                    channel=channel_id or slack_user_id,
                    user=slack_user_id,
                    text="Please enter a Discord username or user ID.",
                )
                return

            tickets = search_discord_tickets_by_user_sync(query)

            if not tickets:
                client.chat_postEphemeral(
                    channel=channel_id or slack_user_id,
                    user=slack_user_id,
                    text=f'No Discord tickets found for *"{query}"*.',
                )
                return

            STATUS_LABEL = {"open": "[open]", "in_progress": "[in progress]", "closed": "[closed]"}

            # Group by user for clarity when query matches multiple users
            from collections import defaultdict
            grouped = defaultdict(list)
            for t in tickets:
                key = (
                    f"{t.get('user_name', 'Unknown')} (ID: {t.get('user_id', '?')})",
                    t.get('user_id'),
                )
                grouped[key].append(t)

            # Build block-kit message - each closed ticket gets a "📄 History" button
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Discord ticket search results for* `{query}`",
                    },
                }
            ]
            for (user_key, user_id), user_tickets in grouped.items():
                blocks.append({"type": "divider"})
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{user_key}*"},
                })
                for t in user_tickets:
                    status = t.get("status", "unknown")
                    label = STATUS_LABEL.get(status, status)
                    num = t.get("ticket_number", "?")
                    ttype = t.get("ticket_type", "")
                    msgs = t.get("messages_count", 0)
                    created = t.get("created_at")
                    date_str = created.strftime("%Y-%m-%d") if created else "—"
                    type_str = f" · {ttype}" if ttype else ""
                    ticket_text = f"*#{num}* · {label}{type_str} · {date_str} · {msgs} msg(s)"

                    if status == "closed" and user_id:
                        blocks.append({
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": ticket_text},
                            "accessory": {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "History"},
                                "action_id": "view_discord_ticket_history",
                                "value": f"{user_id}:{num}",
                            },
                        })
                    else:
                        blocks.append({
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": ticket_text},
                        })

            # Slack block limit per message is 50
            if len(blocks) > 50:
                blocks = blocks[:50]
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_…results truncated (max 50 blocks)_"},
                })

            client.chat_postEphemeral(
                channel=channel_id or slack_user_id,
                user=slack_user_id,
                text=f"Ticket search results for {query}",  # fallback text
                blocks=blocks,
            )
            logger.info(f"[Slack] Discord ticket search for '{query}' returned {len(tickets)} result(s)")
        except Exception as e:
            logger.error(f"[Slack] Error in search_discord_tickets_submit: {e}", exc_info=True)

    # -----------------------------------------------------------------------
    # Create Jira Ticket - Step 1: topic selected
    # -----------------------------------------------------------------------
    @app.view("create_ticket_topic_select")
    def handle_create_ticket_topic_select(ack, body, client, view):
        topic = (
            view["state"]["values"]
            .get("topic_block", {})
            .get("topic_input", {})
            .get("selected_option", {})
            .get("value", "")
        )
        if not topic:
            ack(response_action="errors", errors={"topic_block": "Please select a topic"})
            return

        # Use body["user"]["name"] - already available, no API call needed.
        # A slow client.users_info() before ack() reliably triggers Slack's 3-second timeout.
        # The full email/display_name lookup happens in handle_create_ticket_submit instead.
        author_display = body["user"].get("name") or body["user"]["id"]
        slack_user_id = body["user"]["id"]
        trigger_id = body.get("trigger_id", "")

        # Pre-build the modal before ack() to minimise the gap between ack and views_open.
        # file_input blocks are forbidden in response_action=update/push, so we must use
        # views.open with a fresh trigger_id (valid for 3s from the user's click).
        from views.modal_views import create_ticket_fields_modal
        try:
            modal = create_ticket_fields_modal(topic, author_display)
        except Exception as e:
            logger.error(f"[Slack] Failed to build create_ticket modal for topic '{topic}': {e}")
            ack(response_action="errors", errors={"topic_block": "Internal error. Please try again."})
            return

        ack()
        try:
            client.views_open(trigger_id=trigger_id, view=modal)
        except Exception as e:
            logger.error(
                f"[Slack] Failed to open create_ticket step 2 for user {slack_user_id} "
                f"(topic: {topic}, trigger: {trigger_id}): {e}"
            )
            try:
                client.chat_postMessage(
                    channel=slack_user_id,
                    text=f"Could not open the ticket form for topic *{topic}*. Please try again.",
                )
            except Exception as _e:
                logger.debug("ignored: %s", _e)

    # -----------------------------------------------------------------------
    # Create Jira Ticket - Step 2: submit
    # -----------------------------------------------------------------------
    # Slack "Create Jira Ticket" and "Create Urgent Request" build nothing themselves: they call the
    # same handlers/ticket_create functions as the console queue, so both sides make identical
    # tickets (User ID instead of email, URG dedup, own-ticket rules, auto-binds).
    def _slack_author(client, slack_user_id):
        """(email, display name) of the Slack user who submitted the form."""
        try:
            profile = client.users_info(user=slack_user_id)["user"]["profile"]
            return (profile.get("email", ""),
                    profile.get("display_name") or profile.get("real_name") or slack_user_id)
        except Exception as e:
            logger.warning(f"[Slack] users_info failed for {slack_user_id}: {e}")
            return "", slack_user_id

    def _slack_files(file_objs):
        """Download Slack file_input files -> [{"name","mimetype","content"}] (the console's shape).
        Requires the files:read OAuth scope."""
        token = os.getenv("SLACK_TOKEN", "")
        out = []
        for f in file_objs or []:
            url = f.get("url_private_download") or f.get("url_private")
            if not url:
                continue
            try:
                dl = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
                if dl.status_code == 200:
                    out.append({"name": f.get("name", "attachment"),
                                "mimetype": f.get("mimetype", "application/octet-stream"),
                                "content": dl.content})
                else:
                    logger.warning(f"[Slack->Jira] File download failed ({dl.status_code}): {f.get('name')}")
            except Exception as e:
                logger.error(f"[Slack->Jira] File download failed for {f.get('name')}: {e}")
        return out

    def _ticket_url(issue_key):
        jira_base = os.getenv("JIRA_BASE_URL", "").rstrip("/")
        return f"{jira_base}/browse/{issue_key}" if jira_base else issue_key

    @app.view("create_ticket_submit")
    def handle_create_ticket_submit(ack, body, client, view):
        ack()
        slack_user_id = body["user"]["id"]
        try:
            from handlers.ticket_create import create_so_ticket, _SO_FILE_BLOCKS
            topic = view.get("private_metadata", "")
            values = view["state"]["values"]
            author_email, author_display = _slack_author(client, slack_user_id)
            files_by_block = {
                bid: _slack_files(values.get(bid, {}).get(aid, {}).get("files", []))
                for bid, aid in _SO_FILE_BLOCKS.get(topic, [])
            }
            res = create_so_ticket(topic, values, author_email, author_display, files_by_block)
            if not res.get("ok"):
                client.chat_postMessage(
                    channel=slack_user_id,
                    text=f"Failed to create Jira ticket for topic *{topic}*: {res.get('error') or 'check bot logs'}",
                )
                return
            issue_key = res["key"]
            client.chat_postMessage(
                channel=slack_user_id,
                text=f"Jira ticket created: *<{_ticket_url(issue_key)}|{issue_key}>*\nTopic: *{topic}*",
            )
            logger.info(f"[Slack] Jira ticket {issue_key} created by {author_display} - topic: {topic}")
        except Exception as e:
            logger.error(f"[Slack] Error in create_ticket_submit: {e}", exc_info=True)
            try:
                client.chat_postMessage(channel=slack_user_id,
                                        text="Unexpected error while creating the ticket. Check bot logs.")
            except Exception as e2:
                logger.error(f"[Slack] Could not report the create_ticket_submit error: {e2}")

    # -----------------------------------------------------------------------
    # Create Urgent Request - creates a ticket on the URG Jira board
    # -----------------------------------------------------------------------
    @app.view("create_urgent_request_submit")
    def handle_create_urgent_request_submit(ack, body, client, view):
        values = view["state"]["values"]
        session_raw = (values.get("session_block", {}).get("session_input", {}).get("value") or "").strip()

        # Validate the session info before ack so Slack shows the error inside the form.
        _header, _sessions = parse_session_blocks(session_raw)
        if not [s for s in _sessions if s["game"] and s["playground"]]:
            ack(
                response_action="errors",
                errors={
                    "session_block": (
                        "Each session needs at least:\n"
                        "ID: <session_id>\n"
                        "Application: <name>\n"
                        "Playground: <city>"
                    )
                },
            )
            logger.warning("[Slack] URG request rejected - bad session info")
            return

        ack()
        slack_user_id = body["user"]["id"]
        try:
            from handlers.ticket_create import create_urgent_ticket
            author_email, author_display = _slack_author(client, slack_user_id)
            files = _slack_files(values.get("screenshot_block", {}).get("screenshot_input", {}).get("files", []))
            res = create_urgent_ticket(values, author_email, author_display,
                                       author_slack_id=slack_user_id, files=files)
            if not res.get("ok"):
                client.chat_postMessage(
                    channel=slack_user_id,
                    text=f"Failed to create the urgent request: {res.get('error') or 'check bot logs'}",
                )
                return
            issue_key = res["key"]
            if res.get("results"):
                lines = []
                for r in res["results"]:
                    if not r.get("ok"):
                        lines.append(f"Failed: {r.get('error') or 'check bot logs'}")
                    elif r.get("dup"):
                        lines.append(f"User added to existing *<{_ticket_url(r['key'])}|{r['key']}>* (duplicate)")
                    else:
                        lines.append(f"Created *<{_ticket_url(r['key'])}|{r['key']}>*  {r.get('summary', '')}")
                client.chat_postMessage(channel=slack_user_id,
                                        text="Different games, so one urgent request per game:\n" + "\n".join(lines))
                return
            if res.get("dup"):
                text = (f"Duplicate urgent request - user added to existing "
                        f"*<{_ticket_url(issue_key)}|{issue_key}>* (same game + playground within the last hour).")
            else:
                text = f"Urgent request created: *<{_ticket_url(issue_key)}|{issue_key}>*\n{res.get('summary', '')}"
            client.chat_postMessage(channel=slack_user_id, text=text)
            logger.info(f"[Slack] URG {issue_key} ({'dup' if res.get('dup') else 'new'}) by {slack_user_id}")
        except Exception as e:
            logger.error(f"[Slack] Error in create_urgent_request_submit: {e}", exc_info=True)
            try:
                client.chat_postMessage(channel=slack_user_id,
                                        text="Unexpected error while creating the urgent request. Check bot logs.")
            except Exception as e2:
                logger.error(f"[Slack] Could not report the create_urgent_request_submit error: {e2}")

    @app.view("steam_catalog_submit")
    def handle_steam_catalog_submission(ack, body, client, view):
        values = view["state"]["values"]
        urls_raw = values["steam_urls_block"]["steam_urls_input"].get("value", "") or ""
        urls = [u.strip() for u in urls_raw.splitlines() if u.strip()]
        steam_urls = [u for u in urls if detect_store(u) == "steam"]

        if not steam_urls:
            ack(
                response_action="errors",
                errors={"steam_urls_block": "Please enter at least one valid Steam URL (store.steampowered.com)."},
            )
            return

        ack()

        platform = (
            values["platform_block"]["platform_input"]
            .get("selected_option", {})
            .get("value", "Steam")
        )
        section = (
            values["section_type_block"]["section_type_input"]
            .get("selected_option", {})
            .get("value", "Library")
        )

        threading.Thread(
            target=_process_steam_games,
            args=(client, steam_urls, platform, section),
            daemon=True,
        ).start()


def _process_steam_games(client, urls: list, platform: str, section: str) -> None:
    """Background worker: fetches Steam data, posts results + screenshots to the catalog channel."""
    channel_id = os.getenv("STEAM_CATALOG_CHANNEL_ID", "")
    if not channel_id:
        logger.error("[Steam] STEAM_CATALOG_CHANNEL_ID is not set — cannot post results")
        return

    total = len(urls)
    try:
        client.chat_postMessage(
            channel=channel_id,
            text=f"Processing {total} game{'s' if total != 1 else ''}...",
        )
    except Exception as e:
        logger.error(f"[Steam] Failed to post processing message: {e}")

    processed = 0
    all_info: list[dict] = []

    for url in urls:
        app_id = extract_steam_app_id(url)
        if not app_id:
            logger.warning(f"[Steam] Could not extract app ID from URL: {url}")
            continue

        logger.info(f"[Steam] Processing app {app_id} ({url})")
        api_data = fetch_game_data(app_id)
        if not api_data:
            try:
                client.chat_postMessage(
                    channel=channel_id,
                    text=f"Failed to fetch data for <{url}|this game>. Check bot logs.",
                )
            except Exception as _e:
                logger.debug("ignored: %s", _e)
            continue

        game_name = api_data.get("name", app_id)
        youtube_url, raw_related = scrape_steam_page(app_id, game_name, api_data)

        # Related apps (max 5, only games in our catalog): franchise first, then Steam's similar games.
        from services.steam_service import pick_related
        catalog_related = pick_related(app_id, game_name, raw_related)
        logger.info(f"[Steam] Related apps for app {app_id}: {catalog_related or 'none'} "
                    f"(from {len(raw_related)} similar on Steam, catalog loaded={catalog_loaded()})")
        related_apps = ", ".join(catalog_related)

        info = extract_game_info(app_id, platform, section, api_data, youtube_url, related_apps)

        # Upload screenshots as ZIP archive (no individual uploads)
        dest_dir = tempfile.mkdtemp(prefix="steam_screenshots_")
        try:
            paths = download_screenshots(info["screenshot_urls"], dest_dir)
            if paths:
                zip_path = os.path.join(dest_dir, "screenshots.zip")
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for path in paths:
                        zf.write(path, os.path.basename(path))
                with open(zip_path, "rb") as fh:
                    client.files_upload_v2(
                        channel=channel_id,
                        file=fh.read(),
                        filename=f"{game_name}_screenshots.zip",
                        title=f"{game_name} — Screenshots (ZIP)",
                    )
                logger.info(f"[Steam] Screenshots ZIP uploaded for app {app_id}")
        except Exception as e:
            logger.warning(f"[Steam] Screenshots ZIP failed for app {app_id}: {e}")
        finally:
            shutil.rmtree(dest_dir, ignore_errors=True)

        all_info.append({k: v for k, v in info.items() if k != "screenshot_urls"})
        processed += 1
        logger.info(f"[Steam] Finished processing app {app_id} ({game_name})")

    if not all_info:
        try:
            client.chat_postMessage(
                channel=channel_id,
                text=f"Done — 0/{total} games could be processed. Check bot logs.",
            )
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        return

    # Upload JSON export to Slack
    try:
        json_content = json.dumps(all_info, ensure_ascii=False, indent=2)
        client.files_upload_v2(
            channel=channel_id,
            content=json_content,
            filename="games.json",
            title=f"Game Data Export ({processed} game{'s' if processed != 1 else ''})",
        )
        logger.info(f"[Steam] JSON export uploaded ({processed} games)")
    except Exception as e:
        logger.error(f"[Steam] JSON export upload failed: {e}")

    # Upload XLSX export to Slack
    try:
        xlsx_buf = _generate_xlsx(all_info)
        client.files_upload_v2(
            channel=channel_id,
            file=xlsx_buf,
            filename="games.xlsx",
            title=f"Game Data Export ({processed} game{'s' if processed != 1 else ''})",
        )
        logger.info(f"[Steam] XLSX export uploaded ({processed} games)")
    except Exception as e:
        logger.error(f"[Steam] XLSX export upload failed: {e}")

    try:
        client.chat_postMessage(
            channel=channel_id,
            text=f"Done — {processed}/{total} game{'s' if total != 1 else ''} processed.",
        )
    except Exception as e:
        logger.error(f"[Steam] Failed to post summary message: {e}")


_XLSX_HEADERS = [
    "Title", "Description", "Platform", "Description URL (If Install)",
    "PEGI Age Rating", "Release Date", "Publisher", "Developer",
    "App Link", "Video Link on YouTube", "Related Apps",
]
_XLSX_KEYS = [
    "title", "description", "platform", "description_url",
    "pegi_rating", "release_date", "publisher", "developer",
    "app_link", "youtube_link", "related_apps",
]


def _generate_xlsx(games: list[dict]) -> io.BytesIO:
    """Generate a formatted XLSX workbook from collected game data."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Games"

    _HEADER_FILL = PatternFill(start_color="4A6741", end_color="4A6741", fill_type="solid")
    _ROW_FILL    = PatternFill(start_color="D9EAD3", end_color="D9EAD3", fill_type="solid")
    _HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
    _DATA_FONT   = Font(size=10)

    # Column widths in characters and horizontal alignment per column (1-indexed)
    _COL_WIDTHS = [16, 55, 12, 28, 13, 14, 18, 18, 32, 32, 36]
    # Columns to centre-align (C=3, E=5, F=6, G=7, H=8)
    _CENTER_COLS = {3, 5, 6, 7, 8}

    # ── Header row ────────────────────────────────────────────────────────────
    ws.append(_XLSX_HEADERS)
    ws.row_dimensions[1].height = 42
    for col_idx, cell in enumerate(ws[1], start=1):
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )
        ws.column_dimensions[get_column_letter(col_idx)].width = _COL_WIDTHS[col_idx - 1]

    # ── Data rows ─────────────────────────────────────────────────────────────
    for game in games:
        ws.append([game.get(k, "") for k in _XLSX_KEYS])
        row_idx = ws.max_row
        for col_idx, cell in enumerate(ws[row_idx], start=1):
            cell.fill = _ROW_FILL
            cell.font = _DATA_FONT
            cell.alignment = Alignment(
                horizontal="center" if col_idx in _CENTER_COLS else "left",
                vertical="top",
                wrap_text=True,
            )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
