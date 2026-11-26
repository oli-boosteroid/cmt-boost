import logging
import re
from views.modal_views import add_information_modal, add_testing_session, send_game_proposal_view, send_proposal_view, send_maintenance_notification, send_server_maintenance_notification, send_community_report_view, send_custom_user_issue, send_weekly_report, add_discord_modmail_information_modal, edit_testing_session_modal, add_reddit_user_email_modal, add_discord_user_email_modal, add_facebook_author_email_modal, search_email_by_profile_url_modal, create_ticket_topic_modal, create_urgent_request_modal, search_discord_tickets_modal, add_email_manual_modal
from views.steam_catalog_views import steam_catalog_modal
from views.email_views import add_reddit_jira_ticket_modal
from views.maintenance_views import build_maintenance_modal
from services.mongo_db_service import get_testing_session_by_id, get_active_maintenances, get_discord_ticket_html_sync
import json

logger = logging.getLogger(__name__)

def register_action_handlers(app):
    @app.action("add_information_button")
    def handle_add_information_button(ack, body, client):
        ack()

        try:
            message = body.get("message", {})
            message_text = message.get("text", "")
            message_ts = message.get("ts", "")
            channel_id = body.get("channel", {}).get("id", "")

            # Taking the url from post (Reddit) or from button value (Discord)
            post_url = ""
            button_value = ""
            actions = body.get("actions", [])
            if actions:
                button_value = actions[0].get("value", "")

            # URL comes from button value (Reddit permalink, Discord URL, or Facebook URL)
            if button_value.startswith("https://www.reddit.com") or button_value.startswith("https://discord.com") or button_value.startswith("https://www.facebook.com"):
                post_url = button_value
            elif "Link:" in message_text:
                post_url = message_text.split("Link:")[-1].strip()

            # Taking the title from post
            post_title = ""
            if "New post:" in message_text:
                title_part = message_text.split("New post:")[1].split("\n")[0].strip()
                post_title = title_part

            # Detect platform from button value or channel
            import os
            discord_channel_id = os.getenv("SLACK_DISCORD_CHANNEL_ID", "")
            facebook_channel_id = os.getenv("SLACK_FACEBOOK_POSTS_CHANNEL_ID", "")
            if button_value.startswith("https://discord.com") or channel_id == discord_channel_id:
                platform = "Discord"
            elif button_value.startswith("https://www.reddit.com"):
                platform = "Reddit"
            elif button_value.startswith("https://www.facebook.com") or channel_id == facebook_channel_id:
                platform = "Facebook"
            else:
                platform = "Reddit"

            # Creating a metadata
            post_metadata = {
                "post_url": post_url,
                "post_title": post_title,
                "message_ts": message_ts,
                "channel_id": channel_id,
                "message_text": message_text,
                "platform": platform,
            }

            client.views_open(
                trigger_id=body["trigger_id"],
                view=add_information_modal(private_metadata=json.dumps(post_metadata))
            )
            logger.info("[Slack] Information modal opened successfully")
        except Exception as e:
            logger.error(f"[Slack] Information modal failed: {e}")

    @app.action("open_testing_button")
    def handle_open_testing_button(ack, body, client):
        ack()
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=add_testing_session(private_metadata=json.dumps(body))
            )
            logger.info("[Slack] Testing session modal opened successfully")
        except Exception as e:
            logger.error(f"[Slack] Testing session modal failed: {e}")

    @app.action("open_gaming_proposal_button")
    def handle_open_gaming_proposal_button(ack, body, client):
        ack()
        try:
            # If the button carried a Discord URL (from the wishlist "unrecognized store" alert),
            # pass it as private_metadata so the modal handler can save the learned mapping.
            actions = body.get("actions") or []
            discord_url = actions[0].get("value", "") if actions else ""
            client.views_open(
                trigger_id=body["trigger_id"],
                view=send_game_proposal_view(private_metadata=discord_url)
            )
            logger.info("[Slack] Game proposal view opened successfully")
        except Exception as e:
            logger.error(f"[Slack] Game proposal view failed: {e}")

    @app.action("open_proposal_button")
    def handle_proposal_button(ack, body, client):
        ack()
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=send_proposal_view(private_metadata=json.dumps(body))
            )
            logger.info("[Slack] Proposal view opened successfully")
        except Exception as e:
            logger.error(f"[Slack] Proposal view failed: {e}")

    @app.action("open_maintenance_notification_button")
    def handle_open_maintenance_notification_button(ack, body, client):
        ack()
        try:
            games = get_active_maintenances("game")
            client.views_open(
                trigger_id=body["trigger_id"],
                view=send_maintenance_notification(games_list=games, private_metadata=json.dumps(body))
            )
            logger.info("[Slack] Maintenance notification view opened successfully")
        except Exception as e:
            logger.error(f"[Slack] Maintenance notification view failed: {e}")

    @app.action("open_server_maintenance_notification_button")
    def handle_open_server_maintenance_notification_button(ack, body, client):
        ack()
        try:
            servers = get_active_maintenances("server")
            client.views_open(
                trigger_id=body["trigger_id"],
                view=send_server_maintenance_notification(servers_list=servers, private_metadata=json.dumps(body))
            )
            logger.info("[Slack] Server maintenance notification view opened successfully")
        except Exception as e:
            logger.error(f"[Slack] Server maintenance notification view failed: {e}")

    @app.action("notification_delivery_type")
    def handle_maintenance_delivery_type(ack, body, client):
        # Re-render the maintenance modal to show the Email or Discord/Reddit-link field
        # once a delivery Type is chosen (fields are hidden until then).
        ack()
        try:
            view = body["view"]
            state = view["state"]["values"]

            def _sel(block, action):
                try:
                    return state[block][action]["selected_option"]["value"]
                except Exception:
                    return None

            delivery_type = _sel("delivery_type_block", "notification_delivery_type")
            callback_id = view.get("callback_id", "")
            kind = "server" if "server" in callback_id else "game"
            entity_block = "send_notification_game_block" if kind == "game" else "send_notification_server_block"
            entity_action = "notification_game_input" if kind == "game" else "notification_server_input"

            selected_entity = _sel(entity_block, entity_action)
            if selected_entity == "__none__":
                selected_entity = None
            selected_language = _sel("send_notification_language_block", "notification_language_input")
            entity_options = get_active_maintenances(kind)

            new_view = build_maintenance_modal(
                kind,
                entity_options=entity_options,
                private_metadata=view.get("private_metadata", ""),
                delivery_type=delivery_type,
                selected_entity=selected_entity,
                selected_language=selected_language,
            )
            client.views_update(view_id=view["id"], view=new_view)
            logger.info(f"[Slack] Maintenance modal updated for delivery type '{delivery_type}'")
        except Exception as e:
            logger.error(f"[Slack] Failed to update maintenance modal on type change: {e}")

    @app.action("open_report_button")
    def handle_open_community_report_button(ack, body, client):
        ack()
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=send_community_report_view(private_metadata=json.dumps(body))
            )
            logger.info("[Slack] Community report view opened successfully")
        except Exception as e:
            logger.error(f"[Slack] Community report view failed: {e}")

    @app.action("open_search_email_button")
    def handle_open_search_email_button(ack, body, client):
        ack()
        try:
            channel_id = body.get("channel", {}).get("id", "")
            client.views_open(
                trigger_id=body["trigger_id"],
                view=search_email_by_profile_url_modal(channel_id=channel_id)
            )
            logger.info("[Slack] Search email modal opened successfully")
        except Exception as e:
            logger.error(f"[Slack] Search email modal failed: {e}")

    @app.action("open_add_custom_issue_button")
    def handle_open_add_custom_issue_button(ack, body, client):
        ack()
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=send_custom_user_issue(private_metadata=json.dumps(body))
            )
            logger.info("[Slack] Add custom issue view opened successfully")
        except Exception as e:
            logger.error(f"[Slack] Add custom issue view failed: {e}")

    @app.action("open_weekly_report_button")
    def handle_weekly_report_button(ack, body, client):
        ack()
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=send_weekly_report(private_metadata=json.dumps(body))
            )
            logger.info("[Slack] Weekly report view opened successfully")
        except Exception as e:
            logger.error(f"[Slack] Weekly report view failed: {e}")

    @app.action("open_discord_modmail_info_button")
    def handle_open_discord_modmail_info_button(ack, body, client):
        ack()
        try:
            import re as _re
            # Primary: read channel_link from button value
            channel_link = body.get("actions", [{}])[0].get("value", "")

            # Fallback: extract from message text (for old notifications without value)
            if not channel_link:
                message_text = body.get("message", {}).get("text", "")
                m = _re.search(r'https://discord\.com/channels/\S+', message_text)
                if m:
                    channel_link = m.group(0).rstrip(")")
                logger.info(f"[Slack] ModMail button value empty, fallback URL: {channel_link!r}")

            client.views_open(
                trigger_id=body["trigger_id"],
                view=add_discord_modmail_information_modal(private_metadata=channel_link)
            )
            logger.info(f"[Slack] Discord modmail info modal opened, channel_link={channel_link!r}")
        except Exception as e:
            logger.error(f"[Slack] Discord modmail info modal failed: {e}")

    @app.action("open_add_email_button")
    def handle_open_add_email_button(ack, body, client):
        ack()
        try:
            channel_id = body.get("channel", {}).get("id", "")
            client.views_open(
                trigger_id=body["trigger_id"],
                view=add_email_manual_modal(channel_id),
            )
            logger.info("[Slack] Add Email manual modal opened")
        except Exception as e:
            logger.error(f"[Slack] Add Email manual modal failed: {e}")

    @app.action("add_discord_user_email_button")
    def handle_add_discord_user_email_button(ack, body, client):
        ack()
        try:
            raw = body["actions"][0]["value"]  # "{user_id}:{username}"
            user_id_str, username = raw.split(":", 1)
            client.views_open(
                trigger_id=body["trigger_id"],
                view=add_discord_user_email_modal(user_id_str, username)
            )
            logger.info(f"[Slack] Add Discord user email modal opened for {username} ({user_id_str})")
        except Exception as e:
            logger.error(f"[Slack] Add Discord user email modal failed: {e}")

    @app.action("add_reddit_user_email_button")
    def handle_add_reddit_user_email_button(ack, body, client):
        ack()
        try:
            reddit_username = body["actions"][0]["value"]
            client.views_open(
                trigger_id=body["trigger_id"],
                view=add_reddit_user_email_modal(reddit_username)
            )
            logger.info(f"[Slack] Add Reddit user email modal opened for u/{reddit_username}")
        except Exception as e:
            logger.error(f"[Slack] Add Reddit user email modal failed: {e}")

    @app.action("add_reddit_jira_ticket_button")
    def handle_add_reddit_jira_ticket_button(ack, body, client):
        ack()
        try:
            value = body["actions"][0]["value"]  # "author:convo_id"
            author, _, convo_id = value.partition(":")
            client.views_open(
                trigger_id=body["trigger_id"],
                view=add_reddit_jira_ticket_modal(author, convo_id),
            )
            logger.info(f"[Slack] Add Reddit Jira ticket modal opened for u/{author} (convo {convo_id})")
        except Exception as e:
            logger.error(f"[Slack] Add Reddit Jira ticket modal failed: {e}")

    @app.action("add_facebook_author_email_button")
    def handle_add_facebook_author_email_button(ack, body, client):
        ack()
        try:
            author_name = body["actions"][0]["value"]
            client.views_open(
                trigger_id=body["trigger_id"],
                view=add_facebook_author_email_modal(author_name)
            )
            logger.info(f"[Slack] Add Facebook author email modal opened for {author_name}")
        except Exception as e:
            logger.error(f"[Slack] Add Facebook author email modal failed: {e}")

    @app.action(re.compile(r"^edit_testing_session_button"))
    def handle_edit_testing_session_button(ack, body, client):
        ack()
        try:
            mongo_id = body["actions"][0]["value"]
            session = get_testing_session_by_id(mongo_id)
            if not session:
                client.chat_postEphemeral(
                    channel=body["channel"]["id"],
                    user=body["user"]["id"],
                    text="Session not found in database."
                )
                return
            client.views_open(
                trigger_id=body["trigger_id"],
                view=edit_testing_session_modal(session, mongo_id)
            )
            logger.info(f"[Slack] Edit testing session modal opened for {mongo_id}")
        except Exception as e:
            logger.error(f"[Slack] Edit testing session modal failed: {e}")

    @app.action("issue_input")
    def handle_issue_toggle(ack, body, client):
        ack()
        try:
            from views.modal_views import add_testing_session
            selected = body["actions"][0]["selected_option"]["value"]
            show = selected == "Yes"
            client.views_update(
                view_id=body["view"]["id"],
                view=add_testing_session(
                    private_metadata=body["view"]["private_metadata"],
                    show_issue_fields=show,
                    state=body["view"]["state"]["values"],
                )
            )
        except Exception as e:
            logger.error(f"[Slack] Failed to toggle issue fields: {e}")

    # Jira To-Do buttons: one shared implementation with the console (services/jira_todo.py).
    def _slack_who(body):
        u = body.get("user") or {}
        return "slack:" + (u.get("name") or u.get("username") or u.get("id", ""))

    def _clicked(body):
        return ((body.get("channel") or {}).get("id"), (body.get("message") or {}).get("ts"))

    def _jira_todo(op, body, key, rid=""):
        from services.jira_todo import run
        res = run(op, key, rid=rid, by=_slack_who(body), clicked=_clicked(body))
        if res.get("already"):
            logger.info(f"[Jira] {op} {key}: already done by someone else, nothing posted")
        return res

    @app.action("jira_transferred")
    def handle_jira_transferred(ack, body, client):
        ack()
        try:
            _jira_todo("forward", body, body["actions"][0]["value"].split(":", 1)[0])
        except Exception as e:
            logger.error(f"[Jira] Forwarded button failed: {e}", exc_info=True)

    @app.action("jira_transferred_closed")
    def handle_jira_transferred_closed(ack, body, client):
        ack()
        try:
            _jira_todo("close", body, body["actions"][0]["value"].split(":", 1)[0])
        except Exception as e:
            logger.error(f"[Jira] Closed button failed: {e}", exc_info=True)

    @app.action("jira_stale_closed")
    def handle_jira_stale_closed(ack, body, client):
        ack()
        try:
            _jira_todo("stale_close", body, body["actions"][0]["value"].split(":", 1)[0])
        except Exception as e:
            logger.error(f"[Jira] No-reply-closed button failed: {e}", exc_info=True)

    @app.action("urg_transferred")
    def handle_urg_transferred(ack, body, client):
        ack()
        try:
            key, _, rid = body["actions"][0]["value"].partition("|")
            _jira_todo("urg_done", body, key, rid)
        except Exception as e:
            logger.error(f"[Jira] URG Done button failed: {e}", exc_info=True)

    @app.action("urg_dismiss")
    def handle_urg_dismiss(ack, body, client):
        ack()
        try:
            _jira_todo("dismiss", body, body["actions"][0]["value"].partition("|")[0])
        except Exception as e:
            logger.error(f"[Jira] URG Dismiss button failed: {e}", exc_info=True)

    @app.action("jira_dismiss")
    def handle_jira_dismiss(ack, body, client):
        ack()
        try:
            # value is "channel:ts" of the card; the ticket is found by that stored card
            channel, ts = body["actions"][0]["value"].split(":", 1)
            from services.mongo_db_service import sync_db
            doc = sync_db["jira_tickets"].find_one({"$or": [{"slack_channel": channel, "slack_ts": ts},
                                                            {"slack_stale_channel": channel, "slack_stale_ts": ts}]},
                                                   {"issue_key": 1}) or {}
            if doc.get("issue_key"):
                _jira_todo("dismiss", body, doc["issue_key"])
            else:
                client.chat_delete(channel=channel, ts=ts)   # an old card we can't tie to a ticket: just remove it
        except Exception as e:
            logger.error(f"[Jira] Dismiss button failed: {e}", exc_info=True)

    @app.action("open_add_user_to_ticket_button")
    def handle_open_add_user_to_ticket_button(ack, body, client):
        ack()
        # "Add user to ticket" (Jira tracked users) was retired: users are tied to a ticket by linking
        # their chat in the console (Link Jira Ticket); the L1 In Progress To-Do picks them up from there.
        try:
            client.chat_postEphemeral(channel=body['channel']['id'], user=body['user']['id'],
                                      text="Add user to ticket was removed. Link the user's chat to the Jira ticket "
                                           "in the console (Link Jira Ticket) instead.")
        except Exception as e:
            logger.error(f'[Slack] add_user_to_ticket notice failed: {e}')

    @app.action("check_linked_tickets_button")
    def handle_check_linked_tickets_button(ack, body, client):
        # The old list came from Jira tracked users (retired). Ticket links now live on the chats
        # themselves (Link Jira Ticket in the console), so the console is where to see them.
        ack()
        try:
            client.chat_postEphemeral(channel=body.get('channel', {}).get('id', ''), user=body['user']['id'],
                                      text="Linked tickets are in the console now: each chat's Linked tickets panel "
                                           "and the Jira section.")
        except Exception as e:
            logger.error(f'[Slack] check_linked_tickets notice failed: {e}')

    @app.action("open_create_ticket_button")
    def handle_open_create_ticket_button(ack, body, client):
        ack()
        try:
            client.views_open(
                trigger_id=body['trigger_id'],
                view=create_ticket_topic_modal(),
            )
        except Exception as e:
            logger.error(f'[Slack] Failed to open create_ticket modal: {e}')

    @app.action("open_create_urgent_request_button")
    def handle_open_create_urgent_request_button(ack, body, client):
        ack()
        try:
            author_display = body["user"].get("name") or body["user"]["id"]
            client.views_open(
                trigger_id=body['trigger_id'],
                view=create_urgent_request_modal(author_display),
            )
        except Exception as e:
            logger.error(f'[Slack] Failed to open create_urgent_request modal: {e}')

    @app.action("open_search_discord_tickets_button")
    def handle_open_search_discord_tickets_button(ack, body, client):
        ack()
        try:
            channel_id = body.get("channel", {}).get("id", "")
            client.views_open(
                trigger_id=body["trigger_id"],
                view=search_discord_tickets_modal(channel_id=channel_id),
            )
        except Exception as e:
            logger.error(f'[Slack] Failed to open search_discord_tickets modal: {e}')

    @app.action("view_discord_ticket_history")
    def handle_view_discord_ticket_history(ack, body, client):
        """Upload HTML transcript of a specific Discord ticket to the requester's DMs."""
        ack()
        try:
            value = (body.get("actions") or [{}])[0].get("value", "")
            user_id_str, ticket_num_str = value.split(":", 1)
            user_id = int(user_id_str)
            ticket_number = int(ticket_num_str)

            slack_user_id = body["user"]["id"]
            channel_id = body.get("channel", {}).get("id", "") or slack_user_id

            html_content = get_discord_ticket_html_sync(user_id, ticket_number)
            if not html_content:
                client.chat_postEphemeral(
                    channel=channel_id,
                    user=slack_user_id,
                    text=f"No transcript available for ticket #{ticket_number}.",
                )
                return

            from utils.general_utils import html_to_pdf
            pdf_bytes = html_to_pdf(html_content)

            # files_upload_v2 requires a channel ID (C/D/G/Z prefix), not a user ID (U prefix).
            # Open (or reuse) the DM channel with the user to get a valid D-prefixed channel ID.
            dm_resp = client.conversations_open(users=slack_user_id)
            dm_channel = dm_resp["channel"]["id"]

            if pdf_bytes:
                client.files_upload_v2(
                    channel=dm_channel,
                    content=pdf_bytes,
                    filename=f"ticket_{ticket_number}_history.pdf",
                    title=f"Discord Ticket #{ticket_number} — History",
                )
            else:
                client.files_upload_v2(
                    channel=dm_channel,
                    content=html_content,
                    filename=f"ticket_{ticket_number}_history.html",
                    title=f"Discord Ticket #{ticket_number} — History",
                )
            client.chat_postEphemeral(
                channel=channel_id,
                user=slack_user_id,
                text=f"📄 Ticket #{ticket_number} transcript sent to your DMs.",
            )
        except Exception as e:
            logger.error(f"[Slack] Error in view_discord_ticket_history: {e}")

    @app.action("open_steam_catalog_button")
    def handle_open_steam_catalog_button(ack, body, client):
        ack()
        try:
            client.views_open(
                trigger_id=body["trigger_id"],
                view=steam_catalog_modal(),
            )
            logger.info("[Steam] Catalog modal opened successfully")
        except Exception as e:
            logger.error(f"[Steam] Failed to open catalog modal: {e}")