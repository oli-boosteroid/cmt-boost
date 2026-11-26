from slack_sdk import WebClient
import os
import time
from dotenv import load_dotenv
from pathlib import Path
import logging

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

logger = logging.getLogger(__name__)

client = WebClient(token=os.environ['SLACK_TOKEN'])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _channel_has_action(channel_id: str, action_id: str) -> bool:
    """
    Returns True if any of the last 100 messages in *channel_id* already
    contains a button block element with the given *action_id*.
    Used to avoid duplicate button posts.
    """
    try:
        resp = client.conversations_history(channel=channel_id, limit=100)
        for msg in resp.get('messages', []):
            for block in msg.get('blocks', []):
                if block.get('type') == 'actions':
                    for element in block.get('elements', []):
                        if element.get('action_id') == action_id:
                            return True
        return False
    except Exception as e:
        logger.warning(f"[CreateButton] Could not check channel {channel_id} for action '{action_id}': {e}")
        return False


def _channel_has_image(channel_id: str, alt_text: str) -> bool:
    """
    Returns True if any of the last 100 messages in *channel_id* already
    contains an image block with the given *alt_text*.
    Used to avoid duplicate image posts.
    """
    try:
        resp = client.conversations_history(channel=channel_id, limit=100)
        for msg in resp.get('messages', []):
            for block in msg.get('blocks', []):
                if block.get('type') == 'image' and block.get('alt_text') == alt_text:
                    return True
        return False
    except Exception as e:
        logger.warning(f"[CreateButton] Could not check channel {channel_id} for image '{alt_text}': {e}")
        return False


# ---------------------------------------------------------------------------
# Deprecated stubs (kept so old imports don't break)
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Active button senders
# ---------------------------------------------------------------------------

def post_maintenance_notification_button_to_channel() -> bool:
    """Returns True if buttons were posted, False if already present or on error."""
    channel = os.getenv('SLACK_MAINTENANCE_NOTIFCATIONS_CHANNEL_ID')
    if _channel_has_action(channel, 'open_maintenance_notification_button'):
        logger.info(f"[CreateButton] Maintenance buttons already present in {channel}, skipping.")
        return False
    try:
        server_status_url = f"http://84.239.40.59:5000/images/server_status?ts={int(time.time())}"
        response = client.chat_postMessage(
            channel=channel,
            text="Click on the button to send the maintenance notification",
            blocks=[
                {
                    "type": "image",
                    "image_url": server_status_url,
                    "alt_text": "Servers Status"
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Add Maintenance Notification"
                            },
                            "action_id": "open_maintenance_notification_button",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Add Server Maintenance Notification"
                            },
                            "action_id": "open_server_maintenance_notification_button",
                            "style": "primary",
                        }
                    ]
                }
            ]
        )
        logger.info(f"[CreateButton] Maintenance buttons created in: {channel} ----- {response['ts']}")
        return True
    except Exception as e:
        logger.error(f"[CreateButton] Error while creating maintenance buttons: {e}")
        return False


def add_common_actions_buttons_to_channel() -> bool:
    """Returns True if buttons were posted, False if already present or on error."""
    channel = os.getenv('SLACK_COMMON_ACTIONS_CHANNEL_ID')
    if _channel_has_action(channel, 'open_add_custom_issue_button'):
        logger.info(f"[CreateButton] Common actions buttons already present in {channel}, skipping.")
        return False
    try:
        response = client.chat_postMessage(
            channel=channel,
            text="Click on the button to perform an action",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Click on the button to perform an action"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Send Issue"
                            },
                            "action_id": "open_add_custom_issue_button",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Add Gaming Proposal"
                            },
                            "action_id": "open_gaming_proposal_button",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Add Proposal"
                            },
                            "action_id": "open_proposal_button",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Search User ID"
                            },
                            "action_id": "open_search_email_button",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Search Discord Tickets"
                            },
                            "action_id": "open_search_discord_tickets_button",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Add User ID"
                            },
                            "action_id": "open_add_email_button",
                        }
                    ]
                }
            ]
        )
        logger.info(f"[CreateButton] Common actions buttons created in: {channel} ----- {response['ts']}")
        return True
    except Exception as e:
        logger.error(f"[CreateButton] Error while creating common actions buttons: {e}")
        return False


def add_common_actions_session_buttons_to_channel() -> bool:
    """Posts a second message to common_actions with session and report buttons.
    Returns True if buttons were posted, False if already present or on error."""
    channel = os.getenv('SLACK_COMMON_ACTIONS_CHANNEL_ID')
    if _channel_has_action(channel, 'open_testing_button'):
        logger.info(f"[CreateButton] Session/report buttons already present in {channel}, skipping.")
        return False
    try:
        response = client.chat_postMessage(
            channel=channel,
            text="Click on the button to perform an action",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Click on the button to perform an action"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Add Testing Session"
                            },
                            "action_id": "open_testing_button",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Send Report"
                            },
                            "action_id": "open_report_button",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Send Weekly Report"
                            },
                            "action_id": "open_weekly_report_button",
                            "style": "primary",
                        }
                    ]
                }
            ]
        )
        logger.info(f"[CreateButton] Session/report buttons created in: {channel} ----- {response['ts']}")
        return True
    except Exception as e:
        logger.error(f"[CreateButton] Error while creating session/report buttons: {e}")
        return False


def add_all_community_team_message_to_channel() -> bool:
    """Posts two wishlist images to the All Community Team channel as separate messages.
    Returns True if images were posted, False if already present or on error."""
    channel = os.getenv('SLACK_ALL_COMMUNITY_TEAM_CHANNEL_ID')
    if _channel_has_image(channel, 'Top Wishlist'):
        logger.info(f"[CreateButton] Community team images already present in {channel}, skipping.")
        return False
    try:
        ts = int(time.time())
        # Send each image as a separate message so Slack renders both reliably
        r1 = client.chat_postMessage(
            channel=channel,
            text="Top Wishlist",
            blocks=[{
                "type": "image",
                "image_url": f"http://84.239.40.59:5000/images/top_wishlist?ts={ts}",
                "alt_text": "Top Wishlist"
            }]
        )
        logger.info(f"[CreateButton] Top Wishlist image posted to: {channel} ----- {r1['ts']}")

        r2 = client.chat_postMessage(
            channel=channel,
            text="Problematic Games",
            blocks=[{
                "type": "image",
                "image_url": f"http://84.239.40.59:5000/images/problematic_games?ts={ts}",
                "alt_text": "Problematic Games"
            }]
        )
        logger.info(f"[CreateButton] Problematic Games image posted to: {channel} ----- {r2['ts']}")
        return True
    except Exception as e:
        logger.error(f"[CreateButton] Error while posting community team images: {e}")
        return False


def post_steam_catalog_button_to_channel() -> bool:
    """Posts 'Add Game to Catalog' button to the Steam Catalog channel.
    Returns True if button was posted, False if already present or on error."""
    channel = os.getenv("STEAM_CATALOG_CHANNEL_ID")
    if not channel:
        logger.warning("[CreateButton] STEAM_CATALOG_CHANNEL_ID is not set, skipping Steam catalog button.")
        return False
    if _channel_has_action(channel, "open_steam_catalog_button"):
        logger.info(f"[CreateButton] Steam catalog button already present in {channel}, skipping.")
        return False
    try:
        response = client.chat_postMessage(
            channel=channel,
            text="Click the button to collect Steam game data",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Click the button to collect Steam game data and screenshots",
                    },
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Collect Game Data"},
                            "action_id": "open_steam_catalog_button",
                            "style": "primary",
                        }
                    ],
                },
            ],
        )
        logger.info(f"[CreateButton] Steam catalog button created in: {channel} ----- {response['ts']}")
        return True
    except Exception as e:
        logger.error(f"[CreateButton] Error while creating Steam catalog button: {e}")
        return False


def add_jira_tracked_user_button_to_channel() -> bool:
    """Posts 'Create Ticket' + 'Add User to Ticket' buttons to the Jira notifications channel.
    Returns True if buttons were posted, False if already present or on error."""
    channel = os.getenv('SLACK_JIRA_CHANNEL_ID')
    if _channel_has_action(channel, 'open_create_ticket_button'):
        logger.info(f"[CreateButton] Jira buttons already present in {channel}, skipping.")
        return False
    try:
        response = client.chat_postMessage(
            channel=channel,
            text="Jira automation",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Jira automation"
                    }
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Create Ticket"
                            },
                            "action_id": "open_create_ticket_button",
                            "style": "primary",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Create Urgent Request"
                            },
                            "action_id": "open_create_urgent_request_button",
                            "style": "danger",
                        }
                    ]
                }
            ]
        )
        logger.info(f"[CreateButton] Jira buttons created in: {channel} ----- {response['ts']}")
        return True
    except Exception as e:
        logger.error(f"[CreateButton] Error while creating Jira buttons: {e}")
        return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def send_buttons_to_channels() -> dict:
    """
    Sends buttons/images to all configured channels.
    Skips each destination if the content is already present.
    Returns a summary: {"sent": [...], "skipped": [...]}.
    """
    tasks = [
        ("Maintenance notifications", post_maintenance_notification_button_to_channel),
        ("Common actions (issue/proposal)", add_common_actions_buttons_to_channel),
        ("Common actions (session/report)", add_common_actions_session_buttons_to_channel),
        ("All Community Team images", add_all_community_team_message_to_channel),
        ("Jira tracked user button", add_jira_tracked_user_button_to_channel),
        ("Steam game data button", post_steam_catalog_button_to_channel),
    ]
    sent = []
    skipped = []
    for label, fn in tasks:
        if fn():
            sent.append(label)
        else:
            skipped.append(label)
    return {"sent": sent, "skipped": skipped}


if __name__ == '__main__':
    try:
        send_buttons_to_channels()
        logger.info('Buttons were sent successfully')
    except Exception as e:
        logger.error(f'Error while sending buttons: {e}')
    logger.info('Sending new buttons to the channels...')
