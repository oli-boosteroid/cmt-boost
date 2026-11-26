import os
from dotenv import load_dotenv
from pathlib import Path
import logging
import asyncio
from slack_sdk import WebClient

env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

logger = logging.getLogger(__name__)

client = WebClient(token=os.getenv('SLACK_TOKEN'))


GAMES_MAINTENANCE_CHANNEL = os.getenv('SLACK_GAMES_MAINTENANCE_CHANNEL_ID')
SERVERS_MAINTENANCE_CHANNEL = os.getenv('SLACK_SERVERS_MAINTENANCE_CHANNEL_ID')


def _build_maintenance_text(entity: str, is_started: bool, type_label: str, duration: str | None, event_dt: str | None, eta: str | None = None) -> str:
    status = "started" if is_started else "finished"
    prefix = f"{event_dt} - " if event_dt else ""
    text = f"{prefix}Maintenance has been {status} for the {type_label}: {entity}"
    if not is_started and duration:
        text += f" (Duration: {duration})"
    if eta:
        text += f" (ETA: {eta})"
    return text


async def send_maintenance_message(
    entity, is_started=True, maintenance_type="game", duration=None, event_dt=None
) -> tuple[str | None, str | None]:
    """Sends a maintenance notification and returns (message_ts, channel)."""
    try:
        if maintenance_type == "game":
            channel = GAMES_MAINTENANCE_CHANNEL
            type_label = "game"
        else:
            channel = SERVERS_MAINTENANCE_CHANNEL
            type_label = "server"

        if not channel:
            logger.error(f"[Slack] SLACK_{maintenance_type.upper()}_MAINTENANCE_CHANNEL_ID is not set")
            return None, None

        message_text = _build_maintenance_text(entity, is_started, type_label, duration, event_dt)
        quoted_message = f"> {message_text}"

        response = client.chat_postMessage(
            channel=channel,
            text=message_text,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": quoted_message},
                }
            ],
        )
        status = "started" if is_started else "finished"
        logger.info(f"[Slack] Maintenance notification sent for the {type_label}: {entity} (status: {status}, duration: {duration}).")
        return response.get("ts"), channel
    except Exception as e:
        logger.error(f"[Slack] Error sending maintenance notification: {e}")
        return None, None


def update_maintenance_message_with_eta(entity: str, maintenance_type: str, eta: str) -> bool:
    """Updates the existing Slack message for a running maintenance with the ETA. Returns True on success."""
    from services.mongo_db_service import get_active_maintenance, update_maintenance_eta

    # ETA payloads use "Server: Paris" but records were stored with plain "Paris"
    lookup_entity = entity.removeprefix("Server: ").strip()

    doc = get_active_maintenance(lookup_entity, maintenance_type)
    if not doc:
        logger.error(f"[Slack] Cannot update ETA — no active maintenance found for {maintenance_type} '{lookup_entity}' (raw: '{entity}')")
        return False

    slack_ts = doc.get("slack_ts")
    slack_channel = doc.get("slack_channel")
    if not slack_ts or not slack_channel:
        logger.error(f"[Slack] Cannot update ETA — no Slack message ts stored for {maintenance_type} '{entity}'")
        return False

    try:
        type_label = "game" if maintenance_type == "game" else "server"
        started_at = doc.get("started_at")
        event_dt = started_at.strftime("%Y-%m-%d %H:%M") if started_at else None
        message_text = _build_maintenance_text(entity, True, type_label, None, event_dt, eta)
        quoted_message = f"> {message_text}"

        client.chat_update(
            channel=slack_channel,
            ts=slack_ts,
            text=message_text,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": quoted_message},
                }
            ],
        )
        update_maintenance_eta(entity, maintenance_type, eta)
        logger.info(f"[Slack] ETA updated in Slack message for {maintenance_type} '{entity}': {eta}")
        return True
    except Exception as e:
        logger.error(f"[Slack] Error updating maintenance ETA message: {e}")
        return False


# Keep old functions for backward compatibility
async def send_game_maintenance_message(entity, is_started=True, duration=None):
    await send_maintenance_message(entity, is_started, "game", duration)


if __name__ == '__main__':
    asyncio.run(send_game_maintenance_message("CS:GO"))
