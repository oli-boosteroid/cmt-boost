import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View, Modal, TextInput, Select
from datetime import datetime, timedelta
import io
import logging
import os
import re
from typing import List, Union
import asyncio
import html
import tempfile

from services.redact import mask_email

_EMAIL_REGEX = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
_SESSION_ID_REGEX = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.IGNORECASE)
_JIRA_KEY_RE = re.compile(r'\b(SO-\d+)\b')

_JIRA_URG_PROJECT_KEY = os.getenv('JIRA_URG_PROJECT_KEY', 'URG')


def _is_urg_issue_key(issue_key: str) -> bool:
    """URG (Urgent Request) tickets use a Review->Done lifecycle, NOT the CM-In-Progress
    postpone dance, so they must never become the primary Jira link on a Discord ticket
    (that would postpone the channel with no status that ever un-postpones it)."""
    return bool(issue_key) and issue_key.upper().startswith(f'{_JIRA_URG_PROJECT_KEY.upper()}-')


def _jira_keys_of(ticket: dict) -> list:
    """All Jira keys linked to a Discord ticket (new array field, with scalar fallback)."""
    keys = ticket.get('jira_ticket_keys')
    if keys:
        return list(keys)
    primary = ticket.get('jira_ticket_key')
    return [primary] if primary else []


def _content_with_attachments(message) -> str:
    """Store a message's text PLUS its attachment URLs (screenshots), so the console can render
    them inline. Discord CDN URLs are signed and expire (~24h), so old images may not load, but
    recent screenshots show up."""
    text = message.content or ""
    urls = [att.url for att in getattr(message, "attachments", []) or [] if getattr(att, "url", None)]
    if urls:
        text = (text + "\n" + "\n".join(urls)).strip()
    return text

from slack_sdk.web.async_client import AsyncWebClient

logger = logging.getLogger(__name__)

from services.mongo_db_service import _ident as _ident_fields

class TicketHistoryView(View):
    """Buttons to request PDF history for individual closed tickets."""

    def __init__(self, cog, user_id: int, closed_tickets: list):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        for t in closed_tickets[:5]:
            num = t.get('ticket_number', '?')
            created = t.get('created_at')
            date_str = created.strftime('%m/%d') if created else ''
            label = f"#{num}" + (f" · {date_str}" if date_str else '')
            btn = discord.ui.Button(
                label=label,
                custom_id=f"ticket_history:{user_id}:{num}",
                style=discord.ButtonStyle.secondary,
            )
            btn.callback = self._make_callback(num)
            self.add_item(btn)

    def _make_callback(self, ticket_number):
        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                doc = await self.cog.tickets_collection.find_one(
                    {'user_id': self.user_id, 'ticket_number': ticket_number},
                    {'conversation_html': 1, '_id': 0},
                )
                html_content = (doc or {}).get('conversation_html')
                if not html_content:
                    await interaction.followup.send('No history available for this ticket.', ephemeral=True)
                    return
                from utils.general_utils import html_to_pdf
                pdf_bytes = html_to_pdf(html_content)
                if pdf_bytes:
                    file = discord.File(io.BytesIO(pdf_bytes), filename=f'ticket_{ticket_number}_history.pdf')
                else:
                    file = discord.File(io.BytesIO(html_content.encode('utf-8')), filename=f'ticket_{ticket_number}_history.html')
                await interaction.followup.send(
                    content=f'History for ticket **#{ticket_number}**:',
                    file=file,
                    ephemeral=True,
                )
            except Exception as e:
                logger.error(f'[Discord] Failed to send ticket history for #{ticket_number}: {e}')
                await interaction.followup.send('Failed to generate history.', ephemeral=True)
        return callback


class TicketTypeView(View):
    """
        Button view for ticket type selection
    """

    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog
        self.value = None

    @discord.ui.button(label="Discord-related issue", style=discord.ButtonStyle.primary, custom_id="server_issue")
    async def server_issue(self, interaction: discord.Interaction, button: Button):
        self.value = "server"
        await interaction.response.send_modal(ServerIssueModal(self.cog))

    @discord.ui.button(label="Boosteroid Service Issue", style=discord.ButtonStyle.danger, custom_id="service_issue")
    async def service_issue(self, interaction: discord.Interaction, button: Button):
        self.value = "service"
        await interaction.response.send_modal(ServiceIssueModal(self.cog))

class ServerIssueModal(Modal, title="Report Discord-related issue"):
    """
        Discord Server Issue Modal
    """

    description = TextInput(
        label = "Describe the problem here",
        placeholder = "Please describe the issue in detail",
        style = discord.TextStyle.paragraph,
        required = True,
        max_length = 2000
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Creating ticket
        ticket_data = {
            'type': 'server',
            'description': self.description.value
        }

        success = await self.cog.create_ticket(interaction.user, ticket_data)

        if success:
            await interaction.followup.send("Your ticket has been created!", ephemeral=True)
        else:
            await interaction.followup.send("Could not create ticket. Please try again later.", ephemeral=True)

class ServiceIssueModal(Modal, title="Report Boosteroid Service Issue"):
    """
        Service Issue Modal
    """
    email_or_session = TextInput(
        label = "Email or Boosteroid Session ID",
        placeholder = "Please enter your Boosteroid email or session ID",
        style = discord.TextStyle.paragraph,
        required = True,
        max_length = 200
    )

    description = TextInput(
        label = "Describe the problem here",
        placeholder = "Please describe the issue in detail",
        style = discord.TextStyle.paragraph,
        required = True,
        max_length = 2000
    )

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        ticket_data = {
            'type': 'service',
            'email_or_session': self.email_or_session.value,
            'description': self.description.value
        }

        success = await self.cog.create_ticket(interaction.user, ticket_data)

        if success:
            await interaction.followup.send("Your ticket has been created!", ephemeral=True)
        else:
            await interaction.followup.send("Could not create ticket. Please try again later.", ephemeral=True)

class TicketSelectView(View):
    """
        Button view for ticket selection for sending a message
    """

    def __init__(self, tickets: List[dict], timeout=60):
        super().__init__(timeout=timeout)
        self.selected_ticket = None

        # Adding the button for each ticket
        for ticket in tickets[:5]:
            ticket_type = ticket.get('ticket_type', 'unknown')
            ticket_num = ticket.get('ticket_number', '?')

            button = Button(
                label = f"{ticket_num} - {ticket_type.capitalize()}",
                style = discord.ButtonStyle.primary,
                custom_id = f"ticket_{ticket['_id']}"
            )
            button.callback = self.make_callback(ticket)
            self.add_item(button)

    def make_callback(self, ticket):
        async def callback(interaction: discord.Interaction):
            self.selected_ticket = ticket
            await interaction.response.defer()
            self.stop()
        return callback

class RestoreJiraPostponeView(View):
    """
    Button view shown in "Postpone Cancelled" embed when a Jira-linked postpone
    was interrupted. Lets a moderator restore the previous Jira state.

    restore_type:
        'waiting_reply' - ticket was in "Waiting for User Reply" (after !say, 5-day postpone).
        'jira_linked'   - ticket was waiting for the Jira issue to reach CM In Progress.
    """

    def __init__(self, cog, ticket_id, restore_type: str, issue_key: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.ticket_id = ticket_id
        self.restore_type = restore_type
        self.issue_key = issue_key

        if restore_type == 'waiting_reply':
            label = 'Restore: Waiting for Reply'
            style = discord.ButtonStyle.primary
        else:
            label = 'Restore: Jira Linked'
            style = discord.ButtonStyle.secondary

        btn = Button(label=label, style=style)
        btn.callback = self._on_restore
        self.add_item(btn)

    async def _on_restore(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel

        ticket = await self.cog.tickets_collection.find_one({'_id': self.ticket_id})
        if not ticket or ticket.get('status') not in ('open', 'in_progress'):
            await interaction.followup.send('Ticket is no longer active.', ephemeral=True)
            return

        if self.restore_type == 'waiting_reply':
            await self.cog._jira_restore_waiting_reply(channel, ticket)
        else:
            # _reenter_jira_mode (not _enter_jira_mode) - resets all Jira state flags
            await self.cog._reenter_jira_mode(channel, self.issue_key, ticket)

        # Disable button after single use
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception as _e:
            logger.debug("ignored: %s", _e)

        await interaction.followup.send('Status restored.', ephemeral=True)


DISMISS_CUSTOM_ID = 'modmail:dismiss'


class DismissButton(Button):
    """Stateless Dismiss button for moderator-facing Jira status notifications.

    Shares a fixed custom_id so a single persistent DismissView (registered via
    bot.add_view) keeps it working after a restart, even when it is attached to a
    non-persistent action view (JiraTransferredView / JiraAutoCloseView).
    """

    def __init__(self):
        super().__init__(
            label='Dismiss',
            style=discord.ButtonStyle.secondary,
            custom_id=DISMISS_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        mod_role_id = int(os.getenv('DISCORD_MOD_ROLE_ID', 0) or 0)
        member = interaction.user
        if mod_role_id and (not isinstance(member, discord.Member) or member.get_role(mod_role_id) is None):
            await interaction.response.send_message(
                'Only moderators can dismiss this notification.', ephemeral=True
            )
            return
        try:
            await interaction.response.defer()
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        try:
            await interaction.message.delete()
        except discord.NotFound:
            pass
        except Exception as e:
            logger.warning(f'[Discord] Failed to dismiss message: {e}')


class DismissView(View):
    """Persistent view carrying a single Dismiss button.

    Attach to moderator-facing Jira status notifications and register once via
    bot.add_view(DismissView()) so dismissals survive bot restarts.
    """

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(DismissButton())


class MaintenanceLangView(View):
    """Language picker (final step of `!maintenance`). Clicking a language creates the Discord
    maintenance-end subscription for the chosen game/server and replaces the message with the result.
    """

    _LANGS = [('EN', 'en'), ('RU', 'ru'), ('UA', 'ua')]

    def __init__(self, entity: str, channel_id: int, mtype: str = 'game'):
        super().__init__(timeout=300)
        self.entity = entity
        self.channel_id = channel_id
        self.mtype = mtype
        for label, code in self._LANGS:
            btn = Button(label=label, style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(code)
            self.add_item(btn)

    def _make_callback(self, language: str):
        async def callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            from services.maintenance_notify_service import create_subscription
            try:
                ok = await asyncio.to_thread(
                    create_subscription, self.entity, self.mtype, 'discord', self.channel_id, language
                )
            except Exception as e:
                logger.error(f'[Discord] Failed to create maintenance subscription for {self.entity}: {e}')
                ok = False
            for item in self.children:
                item.disabled = True
            text = (
                f'Subscribed to maintenance end for **{self.entity}** ({language.upper()}).'
                if ok else f'Failed to subscribe for **{self.entity}**.'
            )
            try:
                await interaction.message.edit(content=text, view=self)
            except Exception as _e:
                logger.debug("ignored: %s", _e)
        return callback


class MaintenanceEntitySelect(Select):
    """Dropdown of active maintenances of one kind (step 2 of `!maintenance`)."""

    def __init__(self, mtype: str, entities: list, channel_id: int):
        self.mtype = mtype
        self.channel_id = channel_id
        self._entities = entities[:25]  # Discord allows at most 25 select options
        options = [
            discord.SelectOption(label=e[:100], value=str(i))
            for i, e in enumerate(self._entities)
        ]
        super().__init__(placeholder=f'Choose a {mtype} maintenance...', min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        entity = self._entities[int(self.values[0])]
        await interaction.response.edit_message(
            content=f'Select the language for the maintenance-end message for **{entity}**:',
            view=MaintenanceLangView(entity, self.channel_id, self.mtype),
        )


class MaintenanceEntityView(View):
    """Wraps the maintenance dropdown for a chosen kind."""

    def __init__(self, mtype: str, entities: list, channel_id: int):
        super().__init__(timeout=300)
        self.add_item(MaintenanceEntitySelect(mtype, entities, channel_id))


class MaintenanceTypeView(View):
    """Step 1 of `!maintenance`: choose whether to subscribe to a game or a server maintenance."""

    def __init__(self, channel_id: int):
        super().__init__(timeout=300)
        self.channel_id = channel_id
        for label, mtype in (('Game', 'game'), ('Server', 'server')):
            btn = Button(label=label, style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(mtype)
            self.add_item(btn)

    def _make_callback(self, mtype: str):
        async def callback(interaction: discord.Interaction) -> None:
            from services.mongo_db_service import get_active_maintenances
            entities = await asyncio.to_thread(get_active_maintenances, mtype)
            if not entities:
                for item in self.children:
                    item.disabled = True
                await interaction.response.edit_message(
                    content=f'No active {mtype} maintenances right now.', view=self
                )
                return
            await interaction.response.edit_message(
                content=f'Select the {mtype} maintenance:',
                view=MaintenanceEntityView(mtype, entities, self.channel_id),
            )
        return callback


class MaintenanceSendView(View):
    """Shown in a Discord ticket when a subscribed maintenance ends. `Send` relays the
    localized text to the end user and confirms in the ticket; `Dismiss` removes the notice.
    """

    def __init__(self, cog, text: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.text = text
        btn = Button(label='Send', style=discord.ButtonStyle.success)
        btn.callback = self._on_send
        self.add_item(btn)
        self.add_item(DismissButton())

    async def _on_send(self, interaction: discord.Interaction) -> None:
        mod_role_id = int(os.getenv('DISCORD_MOD_ROLE_ID', 0) or 0)
        member = interaction.user
        if mod_role_id and (not isinstance(member, discord.Member) or member.get_role(mod_role_id) is None):
            await interaction.response.send_message('Only moderators can send this.', ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        ok = await self.cog._send_maintenance_text_to_user(interaction.channel, self.text)
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        await interaction.followup.send(
            'Sent to the user.' if ok else 'Could not send to the user.', ephemeral=True
        )


class JiraTransferredView(View):
    """
    'Передано' button shown in the CM In Progress unpostpone notification.
    Moderator clicks it after relaying info to the user via !say.
    """

    def __init__(self, issue_key: str):
        super().__init__(timeout=None)
        self.issue_key = issue_key

        btn = Button(label='Forwarded', style=discord.ButtonStyle.primary)
        btn.callback = self._on_transferred
        self.add_item(btn)
        self.add_item(DismissButton())

    async def _on_transferred(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from services.jira_service import post_jira_comment, is_last_comment_from_out_user
        if not await asyncio.to_thread(is_last_comment_from_out_user, self.issue_key):
            await asyncio.to_thread(post_jira_comment, self.issue_key, 'Передано')
            logger.info(f'[Discord] Posted Jira comment "Передано" for {self.issue_key} via button')
        else:
            logger.info(f'[Discord] Skipped Jira comment "Передано" for {self.issue_key} — last comment from out-user')
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        await interaction.followup.send('Jira comment posted.', ephemeral=True)


class JiraAutoCloseView(View):
    """
    Jira action button shown before auto-closing a ticket with no user reply.
    """

    def __init__(self, issue_key: str):
        super().__init__(timeout=None)
        self.issue_key = issue_key

        btn = Button(label='No reply, closed', style=discord.ButtonStyle.danger)
        btn.callback = self._on_no_response_closed
        self.add_item(btn)
        self.add_item(DismissButton())

    async def _on_no_response_closed(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        from services.jira_service import (
            post_jira_comment, transition_to_done, is_last_comment_from_out_user,
        )
        if not await asyncio.to_thread(is_last_comment_from_out_user, self.issue_key):
            await asyncio.to_thread(post_jira_comment, self.issue_key, 'Ответа нет, закрыто')
        else:
            logger.info(f'[Discord] Skipped Jira comment for {self.issue_key} — last comment from out-user')
        await asyncio.to_thread(transition_to_done, self.issue_key)
        logger.info(f'[Discord] Posted "Ответа нет, закрыто" + Done transition for {self.issue_key} via button')
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception as _e:
            logger.debug("ignored: %s", _e)
        await interaction.followup.send('Jira updated.', ephemeral=True)


class ModMail(commands.Cog):
    """
        ModMail system for ticket-based user support
    """

    def __init__(self, bot, mongo_db):
        self.bot = bot
        self.db = mongo_db
        self.tickets_collection = self.db['discord_tickets']
        self.users_emails_collection = self.db['users_emails']
        self.jira_tickets_collection = self.db['jira_tickets']

        # Environment from .env
        self.modmail_category_id = int(os.getenv('DISCORD_MODMAIL_CATEGORY_ID'))
        self.modmail_mod_role_id = int(os.getenv('DISCORD_MOD_ROLE_ID'))
        self.guild_id = int(os.getenv('DISCORD_GUILD_ID'))
        self.closed_tickets_category_id = int(os.getenv('DISCORD_CLOSED_MODMAIL_CATEGORY_ID'))
        self.waiting_category_id = int(os.getenv('DISCORD_WAITING_MODMAIL_CATEGORY_ID', 0))
        self.jira_category_id = int(os.getenv('DISCORD_JIRA_MODMAIL_CATEGORY_ID', 0))

        self.max_open_tickets = 5
        self.ticket_creation_cooldown_hours = 24
        self.channel_edit_timestamps = {}

        logger.info('[Discord] Discord ModMail system initialized.')

        self.check_postponed_task = None
        self.cleanup_closed_channels_task = None
        self.closed_channel_lifetime_days = 3
        self.slack_client = AsyncWebClient(token=os.getenv('SLACK_TOKEN'))
        self.slack_tickets_channel = os.getenv('SLACK_DISCORD_MODMAIL_CHANNEL_ID')
        self.jira_base_url = os.getenv('JIRA_BASE_URL', '').rstrip('/')

        # Tracks pending !sclose (delayed close) tasks: {ticket_id: asyncio.Task}
        self._scheduled_close_tasks = {}
        self.scheduled_close_delay_seconds = 3600  # 1 hour

        self._message_notify_cooldown: dict[int, datetime] = {}  # user_id → last notification time

    async def _notify_slack(self, event_type: str, ticket_number, ticket_type: str,
                            user_name: str, channel_id: int, user_id: int = None,
                            jira_ticket_key: str = None, preview: str = ""):
        channel_link = f"https://discord.com/channels/{self.guild_id}/{channel_id}"

        # Per-user notifications (console prefs): fan the same event out to subscribed team members,
        # off the event loop (the sends are sync HTTP). Best-effort; never blocks the Slack alert. The
        # link points at the ticket INSIDE the console (resolved by channel_id), not at Discord, and the
        # body carries a short preview of the message (like the Slack alert).
        try:
            import asyncio as _aio
            from services.user_notify_service import notify_event
            _prev = " ".join((preview or "").split())[:140]
            _route = f"#/discord/{channel_id}"
            _spec = None
            if event_type == "created":
                _spec = ("discord_ticket", "New Discord ticket",
                         f"#{ticket_number} ({ticket_type}) - {user_name}" + (f": {_prev}" if _prev else ""), "", _route)
            elif event_type == "message":
                _spec = ("discord_msg", "New Discord message",
                         f"#{ticket_number} - {user_name}" + (f": {_prev}" if _prev else ""), "", _route)
            if _spec:
                # Keep a strong ref so the fire-and-forget task isn't GC'd before it runs.
                _tasks = getattr(self, "_notify_tasks", None)
                if _tasks is None:
                    _tasks = self._notify_tasks = set()
                _t = _aio.create_task(_aio.to_thread(notify_event, *_spec))
                _tasks.add(_t); _t.add_done_callback(_tasks.discard)
        except Exception as _e:
            logger.debug(f"[Discord] user notify failed: {_e}")

        if event_type == "created":
            try:
                email = None
                if user_id:
                    try:
                        doc = await self.users_emails_collection.find_one(
                            {'discord_user_id': {'$in': [user_id, str(user_id)]}},
                            {'cloud_user_id': 1, '_id': 0}, sort=[('cloud_user_id', -1)],
                        )
                        uid = (doc or {}).get('cloud_user_id')
                        email = f"User ID {uid}" if uid else None
                    except Exception as e:
                        logger.error(f"[Discord] Failed to get User ID for user {user_id}: {e}")
                from services.redact import link_user_ids_slack
                email_text = link_user_ids_slack(email or "No User ID on file")

                section_text = (
                    f"New ticket #{ticket_number} ({ticket_type.upper()}) created by {user_name}\n"
                    f"{user_name} | {email_text}\n"
                    f"{channel_link}"
                )

                action_buttons = [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Add Information"},
                        "action_id": "open_discord_modmail_info_button",
                        "style": "primary",
                        "value": channel_link,
                    }
                ]
                if not email and user_id:
                    action_buttons.append({
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Add Email"},
                        "action_id": "add_discord_user_email_button",
                        "value": f"{user_id}:{user_name}",
                    })

                await self.slack_client.chat_postMessage(
                    channel=self.slack_tickets_channel,
                    text=section_text,
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": section_text}
                        },
                        {
                            "type": "actions",
                            "elements": action_buttons,
                        }
                    ]
                )
                logger.info(f"[Discord] Slack notification sent: created ticket #{ticket_number}")
            except Exception as e:
                logger.error(f"[Discord] Failed to send Slack notification: {e}")
            return

        elif event_type == "message":
            text = f"New message in ticket #{ticket_number} from {user_name}\n{channel_link}"
            if jira_ticket_key:
                jira_url = f'{self.jira_base_url}/browse/{jira_ticket_key}' if self.jira_base_url else jira_ticket_key
                text += f"\nJira: {jira_url}"
        else:
            return

        try:
            await self.slack_client.chat_postMessage(
                channel=self.slack_tickets_channel,
                text=text
            )
            logger.info(f"[Discord] Slack notification sent: {event_type} ticket #{ticket_number}")
        except Exception as e:
            logger.error(f"[Discord] Failed to send Slack notification: {e}")

    async def cog_load(self):
        """
            Call it when cog is loading
        """
        self.check_postponed_task = self.bot.loop.create_task(self.check_postponed_tickets())
        self.cleanup_closed_channels_task = self.bot.loop.create_task(self.cleanup_closed_channels())
        self.bot.loop.create_task(self._restore_scheduled_closes())
        # Register the persistent Dismiss view so Dismiss buttons keep working after a restart.
        self.bot.add_view(DismissView())
        logger.info("[Discord] Discord ModMail background tasks started")

    async def _restore_scheduled_closes(self):
        """
            On cog load, resume pending !sclose tasks for tickets that had
            scheduled_close_until set before bot restart.
        """
        try:
            await self.bot.wait_until_ready()
            now = datetime.utcnow()
            pending = await self.tickets_collection.find({
                'scheduled_close_until': {'$exists': True, '$ne': None},
                'status': {'$in': ['open', 'in_progress']}
            }).to_list(length=500)

            for ticket in pending:
                ticket_id = ticket['_id']
                channel_id = ticket.get('channel_id')
                scheduled_until = ticket.get('scheduled_close_until')
                if not channel_id or not scheduled_until:
                    continue

                remaining = (scheduled_until - now).total_seconds()
                if remaining <= 0:
                    # Expired while bot was down - fire immediately
                    remaining = 0

                closer_id = ticket.get('scheduled_close_by')
                closer = None
                if closer_id:
                    try:
                        closer = await self.bot.fetch_user(closer_id)
                    except Exception:
                        closer = None

                task = self.bot.loop.create_task(
                    self._run_scheduled_close_after(ticket_id, channel_id, closer, remaining)
                )
                self._scheduled_close_tasks[ticket_id] = task
                logger.info(f"[Discord] Restored scheduled close for ticket #{ticket.get('ticket_number', '?')} in {int(remaining)}s")
        except Exception as e:
            logger.error(f"[Discord] Failed to restore scheduled closes: {e}")

    async def _run_scheduled_close_after(self, ticket_id, channel_id: int, closer, delay_seconds: float):
        """Sleep for delay_seconds then perform silent close (used on restore)."""
        try:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

            ticket = await self.tickets_collection.find_one({'_id': ticket_id})
            if not ticket or ticket.get('status') == 'closed' or not ticket.get('scheduled_close_until'):
                return

            guild = self.bot.get_guild(self.guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if not channel:
                return

            await self.close_ticket(channel, closer, notify_user=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"[Discord] Restored scheduled close task failed: {e}")
        finally:
            self._scheduled_close_tasks.pop(ticket_id, None)

    def cog_unload(self):
        """
            Canceling background tasks on cog unloading
        """
        self.check_postponed_task.cancel()
        if self.cleanup_closed_channels_task:
            self.cleanup_closed_channels_task.cancel()

    async def check_postponed_tickets(self):
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            now = datetime.utcnow()
            # Each job has its own try: one failing job must not skip the others for 5 minutes.
            try:
                postponed_tickets = await self.tickets_collection.find({
                    'status': {'$in': ['open', 'in_progress']},
                    'postponed_until': {'$lte': now},
                    'postpone_reminder_sent': False
                }).to_list(length=100)

                for ticket in postponed_tickets:
                    channel = self.bot.get_channel(ticket['channel_id'])

                    if channel:
                        # Jira ticket-mode: if postpone expired with jira_waiting_reply → auto-close
                        if ticket.get('jira_waiting_reply'):
                            await self._jira_auto_close(channel, ticket)
                            continue

                        # Build combined edit: rename + move back to main category in one call
                        edit_kwargs = {}
                        new_name = channel.name.replace("🕐-", "")
                        if new_name != channel.name:
                            edit_kwargs['name'] = new_name

                        guild = self.bot.get_guild(self.guild_id)
                        if guild and channel.category_id != self.modmail_category_id:
                            modmail_category = guild.get_channel(self.modmail_category_id)
                            if modmail_category:
                                edit_kwargs['category'] = modmail_category

                        if edit_kwargs:
                            edit_success = await self.safe_edit_channel(channel, **edit_kwargs)
                            if not edit_success:
                                logger.warning(
                                    f"[Discord] Could not rename/move channel {channel.name} "
                                    f"due to rate limit"
                                )
                            else:
                                logger.info(f"[Discord] Moved ticket #{ticket.get('ticket_number')} back to main modmail category after postpone expired")

                        embed = discord.Embed(
                            title="Postpone Expired",
                            description=(
                                f"The postpone for this ticket has ended.\n"
                                f"**Reason was:** {ticket.get('postpone_reason', 'No reason provided')}"
                            ),
                            color=discord.Color.orange(),
                            timestamp=datetime.utcnow()
                        )

                        await channel.send(
                            content=f"<@&{self.modmail_mod_role_id}>",
                            embed=embed
                        )

                        await self.tickets_collection.update_one(
                            {'_id': ticket['_id']},
                            {'$set': {
                                'postpone_reminder_sent': True,
                                'postponed_until': None,
                            }}
                        )

                        logger.info(f"[Discord] Sent postpone reminder to Discord ticket: {ticket['_id']}")
            except Exception as e:
                logger.error(f"[Discord] Postponed-check job 'postpone expired' failed: {e}", exc_info=True)

            try:
                # Check tickets waiting for Jira CM In Progress transition
                waiting_tickets = await self.tickets_collection.find({
                    'status': {'$in': ['open', 'in_progress']},
                    'waiting_for_jira': True,
                    'auto_link_postpone_pending': {'$ne': True},
                }).to_list(length=100)

                for ticket in waiting_tickets:
                    issue_key = ticket.get('jira_ticket_key')
                    if not issue_key:
                        continue
                    try:
                        jira_doc = await self.jira_tickets_collection.find_one({'issue_key': issue_key})
                        if jira_doc and jira_doc.get('status') == 'CM In Progress':
                            channel = self.bot.get_channel(ticket['channel_id'])
                            if channel:
                                await self._unpostpone_jira_ticket(channel, ticket, jira_doc)
                    except Exception as e:
                        logger.error(f'[Discord] Error checking Jira status for ticket #{ticket.get("ticket_number")}: {e}')
            except Exception as e:
                logger.error(f"[Discord] Postponed-check job 'Jira CM In Progress wait' failed: {e}", exc_info=True)

            try:
                # Auto-link Discord tickets from Jira Communication Link
                auto_link_docs = await self.jira_tickets_collection.find({
                    'discord_channel_id': {'$exists': True, '$ne': None},
                    'discord_auto_linked': {'$ne': True},
                }).to_list(length=50)

                for jira_doc in auto_link_docs:
                    discord_channel_id = jira_doc.get('discord_channel_id')
                    issue_key = jira_doc.get('issue_key')
                    if not discord_channel_id or not issue_key:
                        continue
                    try:
                        # Match the ticket tolerant of int/str channel_id storage differences, so a
                        # button-created (URG) ticket whose jira doc stored the id as one type still
                        # finds its ticket.
                        cid_candidates = [discord_channel_id, str(discord_channel_id)]
                        try:
                            cid_candidates.append(int(discord_channel_id))
                        except (TypeError, ValueError):
                            pass
                        try:
                            cid_int = int(discord_channel_id)
                        except (TypeError, ValueError):
                            cid_int = discord_channel_id
                        discord_ticket = await self.tickets_collection.find_one({
                            'channel_id': {'$in': cid_candidates},
                            'status': {'$in': ['open', 'in_progress']},
                        })
                        if discord_ticket and issue_key in _jira_keys_of(discord_ticket):
                            # Already linked - done, stop retrying.
                            await self.jira_tickets_collection.update_one(
                                {'_id': jira_doc['_id']},
                                {'$set': {'discord_auto_linked': True}, '$unset': {'discord_link_attempts': ''}},
                            )
                            continue

                        channel = self.bot.get_channel(cid_int)
                        if (not discord_ticket) or (channel is None):
                            # TRANSIENT miss: the ticket may not be open yet, or the channel isn't in the
                            # gateway cache this pass. Do NOT give up permanently - leave
                            # discord_auto_linked unset so the next loop retries. Bound it so a genuinely
                            # dead channel doesn't retry forever.
                            attempts = int(jira_doc.get('discord_link_attempts') or 0) + 1
                            upd = {'discord_link_attempts': attempts}
                            if attempts >= 60:
                                upd['discord_auto_linked'] = True
                                logger.warning(f'[Discord] Giving up auto-link for {issue_key} → channel {discord_channel_id} after {attempts} attempts (no open ticket / channel unresolved)')
                            await self.jira_tickets_collection.update_one(
                                {'_id': jira_doc['_id']}, {'$set': upd})
                            continue

                        if discord_ticket.get('jira_ticket_key') or _is_urg_issue_key(issue_key):
                            # Channel already Jira-linked, OR this is a URG ticket (which must never own
                            # the channel's postpone state) → attach as an additional ticket. This keeps
                            # it findable for the mods system-note without putting the channel on an
                            # indefinite CM-In-Progress hold.
                            await self._link_additional_jira(channel, issue_key, discord_ticket)
                            logger.info(f'[Discord] Auto-linked additional Jira {issue_key} to channel {discord_channel_id}')
                        elif jira_doc.get('status') != 'CM In Progress':
                            # Primary auto-link only at ticket creation (before CM In Progress)
                            await self._enter_jira_mode(channel, issue_key, discord_ticket)
                            logger.info(f'[Discord] Auto-linked Jira {issue_key} to Discord channel {discord_channel_id}')
                        else:
                            # Skip primary auto-link when Jira is already in CM In Progress -
                            # linking should happen at creation only (moderator handles it manually at this stage)
                            logger.info(f'[Discord] Skipped auto-link for {issue_key} — already in CM In Progress')

                        # Only mark done AFTER a successful link (or the deliberate CM-In-Progress skip).
                        await self.jira_tickets_collection.update_one(
                            {'_id': jira_doc['_id']},
                            {'$set': {'discord_auto_linked': True}, '$unset': {'discord_link_attempts': ''}},
                        )
                    except Exception as e:
                        logger.error(f'[Discord] Error auto-linking Jira {issue_key} → channel {discord_channel_id}: {e}')
            except Exception as e:
                logger.error(f"[Discord] Postponed-check job 'Jira auto-link' failed: {e}", exc_info=True)

            try:
                # Execute deferred postpone after 1h inactivity
                now_utc = datetime.utcnow()
                deferred_tickets = await self.tickets_collection.find({
                    'status': {'$in': ['open', 'in_progress']},
                    'auto_link_postpone_pending': True,
                    'auto_link_postpone_at': {'$lte': now_utc},
                }).to_list(length=50)

                for ticket in deferred_tickets:
                    issue_key = ticket.get('jira_ticket_key')
                    if not issue_key:
                        continue
                    try:
                        channel = self.bot.get_channel(ticket['channel_id'])
                        jira_doc = await self.jira_tickets_collection.find_one({'issue_key': issue_key})
                        if channel and jira_doc and jira_doc.get('status') == 'CM In Progress':
                            await self.tickets_collection.update_one(
                                {'_id': ticket['_id']},
                                {'$unset': {'auto_link_postpone_pending': '', 'auto_link_postpone_at': ''}},
                            )
                            await self._unpostpone_jira_ticket(channel, ticket, jira_doc)
                            logger.info(f'[Discord] Deferred postpone executed for ticket #{ticket.get("ticket_number")}')
                    except Exception as e:
                        logger.error(f'[Discord] Error executing deferred postpone for ticket {ticket.get("_id")}: {e}')
            except Exception as e:
                logger.error(f"[Discord] Postponed-check job 'deferred postpone' failed: {e}", exc_info=True)

            try:
                # Re-enter Jira mode for tickets whose Jira issue moved back to Backlog
                backlog_jira_docs = await self.jira_tickets_collection.find({
                    'needs_discord_postpone': True,
                }).to_list(length=50)

                for jira_doc in backlog_jira_docs:
                    issue_key = jira_doc.get('issue_key')
                    if not issue_key:
                        continue
                    try:
                        discord_ticket = await self.tickets_collection.find_one({
                            'jira_ticket_key': issue_key,
                            'status': {'$in': ['open', 'in_progress']},
                            'waiting_for_jira': {'$ne': True},
                            'jira_waiting_reply': {'$ne': True},
                        })
                        if not discord_ticket:
                            logger.info(f'[Discord] No active Discord ticket found for Jira {issue_key} (Backlog)')
                            # No active ticket - clear flag so we don't retry forever
                            await self.jira_tickets_collection.update_one(
                                {'_id': jira_doc['_id']},
                                {'$unset': {'needs_discord_postpone': ''}},
                            )
                            continue
                        channel = self.bot.get_channel(discord_ticket['channel_id'])
                        if channel:
                            await self._reenter_jira_mode(channel, issue_key, discord_ticket, jira_doc)
                        # Clear flag only after successful processing
                        await self.jira_tickets_collection.update_one(
                            {'_id': jira_doc['_id']},
                            {'$unset': {'needs_discord_postpone': ''}},
                        )
                    except Exception as e:
                        logger.error(f'[Discord] Error re-entering Jira mode for {issue_key}: {e}')
                        # Flag intentionally NOT cleared - next loop iteration will retry
            except Exception as e:
                logger.error(f"[Discord] Postponed-check job 'Jira back to Backlog' failed: {e}", exc_info=True)

            try:
                # Per-key status notifications for tickets linked to multiple Jira tickets
                multi_tickets = await self.tickets_collection.find({
                    'status': {'$in': ['open', 'in_progress']},
                    'jira_ticket_keys.1': {'$exists': True},
                }).to_list(length=100)

                for ticket in multi_tickets:
                    await self._notify_secondary_jira_status(ticket)
            except Exception as e:
                logger.error(f"[Discord] Postponed-check job 'multi-Jira status' failed: {e}", exc_info=True)

            try:
                # Deliver pending Discord maintenance-end notifications
                pending_maintenance = await self.db['maintenance_subscriptions'].find({
                    'target_type': 'discord', 'status': 'ended_pending',
                }).to_list(length=100)

                for sub in pending_maintenance:
                    await self._deliver_maintenance_notification(sub)
            except Exception as e:
                logger.error(f"[Discord] Postponed-check job 'maintenance-end delivery' failed: {e}", exc_info=True)


            await asyncio.sleep(300)

    async def _force_cleanup_oldest_closed_tickets(self, count: int = 5):
        """
        Emergency cleanup: delete the N oldest closed ticket channels.
        Called when Discord reports the category has hit the 50-channel limit (error 50035).
        """
        try:
            logger.warning(f"[Discord] Running emergency cleanup: deleting {count} oldest closed ticket channels")

            oldest_tickets = await self.tickets_collection.find(
                {'status': 'closed', 'channel_id': {'$ne': None}},
                sort=[('closed_at', 1)]
            ).to_list(length=count)

            deleted = 0
            for ticket in oldest_tickets:
                try:
                    channel = await self.bot.fetch_channel(ticket['channel_id'])
                    await channel.delete(reason="Emergency cleanup: category channel limit reached")
                    logger.info(f"[Discord] Emergency cleanup: deleted channel for ticket #{ticket.get('ticket_number')} ({channel.name})")
                    deleted += 1
                except discord.NotFound:
                    logger.warning(f"[Discord] Emergency cleanup: channel for ticket #{ticket.get('ticket_number')} already deleted")
                except Exception as e:
                    logger.error(f"[Discord] Emergency cleanup: failed to delete channel for ticket #{ticket.get('ticket_number')}: {e}")

                await self.tickets_collection.update_one(
                    {'_id': ticket['_id']},
                    {'$set': {'channel_id': None}}
                )

            logger.warning(f"[Discord] Emergency cleanup complete: {deleted}/{count} channels deleted")
        except Exception as e:
            logger.error(f"[Discord] Emergency cleanup failed: {e}")

    async def cleanup_closed_channels(self):
        """
            Background task: delete closed ticket channels older than 3 days
        """
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            try:
                guild = self.bot.get_guild(self.guild_id)
                if not guild:
                    await asyncio.sleep(3600)
                    continue

                closed_category = guild.get_channel(self.closed_tickets_category_id)
                if not closed_category:
                    await asyncio.sleep(3600)
                    continue

                cutoff_dt = datetime.utcnow() - timedelta(days=self.closed_channel_lifetime_days)

                # Find tickets that were closed more than 3 days ago and still have a channel_id
                old_tickets = await self.tickets_collection.find({
                    'status': 'closed',
                    'closed_at': {'$lte': cutoff_dt},
                    'channel_id': {'$ne': None}
                }).to_list(length=200)

                for ticket in old_tickets:
                    ticket_num = ticket.get('ticket_number', '?')
                    raw_channel_id = ticket['channel_id']

                    # Use fetch_channel so we get the channel even after a bot restart
                    # (get_channel only looks in the local cache and returns None when cold)
                    try:
                        channel = await self.bot.fetch_channel(raw_channel_id)
                    except discord.NotFound:
                        # Already deleted manually - just clear the DB reference
                        logger.info(f"[Discord] Cleanup: channel for ticket #{ticket_num} already gone, clearing DB reference")
                        await self.tickets_collection.update_one(
                            {'_id': ticket['_id']}, {'$set': {'channel_id': None}}
                        )
                        continue
                    except Exception as e:
                        logger.error(f"[Discord] Cleanup: could not fetch channel for ticket #{ticket_num}: {e}")
                        continue

                    if channel.category_id != self.closed_tickets_category_id:
                        # Not in the closed category - skip silently (might have been moved)
                        continue

                    try:
                        await channel.delete(reason=f"Closed ticket auto-cleanup after {self.closed_channel_lifetime_days} days")
                        logger.info(f"[Discord] Deleted closed ticket channel #{ticket_num} ({channel.name})")
                        # Only clear the reference AFTER successful deletion
                        await self.tickets_collection.update_one(
                            {'_id': ticket['_id']}, {'$set': {'channel_id': None}}
                        )
                    except discord.NotFound:
                        logger.warning(f"[Discord] Cleanup: channel for ticket #{ticket_num} was already deleted")
                        await self.tickets_collection.update_one(
                            {'_id': ticket['_id']}, {'$set': {'channel_id': None}}
                        )
                    except discord.HTTPException as e:
                        if e.code == 50035:
                            logger.error(f"[Discord] Category channel limit reached (50035) while deleting ticket #{ticket_num}. Triggering emergency cleanup.")
                            await self._force_cleanup_oldest_closed_tickets(count=5)
                        else:
                            logger.error(f"[Discord] Failed to delete closed ticket channel #{ticket_num}: {e}")
                    except Exception as e:
                        logger.error(f"[Discord] Failed to delete closed ticket channel #{ticket_num}: {e}")

            except Exception as e:
                logger.error(f"[Discord] Error in cleanup_closed_channels task: {e}")

            # Run every 6 hours
            await asyncio.sleep(6 * 3600)

    async def get_user_tickets(self, user_id: int, status: str = None) -> List[dict]:
        """
            Getting user's tickets
        """

        query = {'user_id': user_id}

        if status:
            query['status'] = status
        else:
            query['status'] = {'$in': ['open', 'in_progress']}

        tickets = await self.tickets_collection.find(query).sort('created_at', -1).to_list(length=100)
        return tickets

    async def can_create_ticket(self, user_id: int) -> tuple[bool, str]:
        """
            Checking if user can create a new ticket
        """

        # Count of all opened tickets
        open_tickets = await self.get_user_tickets(user_id)

        # If user doesn't have any tickets - no cooldown
        if len(open_tickets) == 0:
            return True, ""

        if len(open_tickets) >= self.max_open_tickets:
            return False, f"You have reached the maximum number of open tickets ({self.max_open_tickets}). Please close some tickets before creating new ones."

        # 24h cooldown checking
        cooldown_time = datetime.utcnow() - timedelta(hours=self.ticket_creation_cooldown_hours)

        recent_ticket = await self.tickets_collection.find_one({
            'user_id': user_id,
            'created_at': {'$gte': cooldown_time}
        }, sort=[('created_at', -1)])

        if recent_ticket:
            time_left = (recent_ticket['created_at'] + timedelta(hours=self.ticket_creation_cooldown_hours)) - datetime.utcnow()
            hours = int(time_left.total_seconds() // 3600)
            minutes = int((time_left.total_seconds() % 3600) // 60)

            return False, f"You can create only 1 new ticket per 24 hours.\nNext ticket available in: {hours}h {minutes}m"

        return True, ""

    async def get_user_ticket_number(self, user_id: int) -> int:
        """
            Get the next number of ticket for the user
        """
        last_ticket = await self.tickets_collection.find_one(
            {'user_id': user_id},
            sort=[('ticket_number', -1)]
        )

        if last_ticket and 'ticket_number' in last_ticket:
            return last_ticket['ticket_number'] + 1
        return 1

    async def safe_edit_channel(self, channel: discord.TextChannel, **kwargs) -> bool:
        """
            Safe channel editing due to rate limits
            No more than 2 times at 10 minutes
        """
        try:
            current_time = datetime.utcnow()
            channel_id = channel.id

            # Get change history of the channel
            if channel_id not in self.channel_edit_timestamps:
                self.channel_edit_timestamps[channel_id] = []

            # Delete old timestamps (older than 10 mins)
            cutoff_time = current_time - timedelta(minutes=10)
            self.channel_edit_timestamps[channel_id] = [
                ts for ts in self.channel_edit_timestamps[channel_id]
                if ts > cutoff_time
            ]

            # Check limit
            if len(self.channel_edit_timestamps[channel_id]) >= 2:
                oldest_edit = self.channel_edit_timestamps[channel_id][0]
                wait_time = (oldest_edit + timedelta(minutes=10)) - current_time
                wait_seconds = int(wait_time.total_seconds()) + 1

                logger.warning(
                    f"Rate limit: Channel {channel.name} can't be edited now. "
                    f"Next edit available in {wait_seconds}s"
                )
                return False

            # Editing the channel
            await channel.edit(**kwargs)
            self.channel_edit_timestamps[channel_id].append(current_time)

            return True
        except Exception as e:
            logger.error(f"Failed to edit channel {channel.name}: {e}")
            return False

    async def show_ticket_menu(self, user: discord.User):
        """
            Show the menu of the ticket choosing
        """

        # Checking if user can create a ticket
        can_create, error_msg = await self.can_create_ticket(user.id)

        if not can_create:
            # Showing the previous tickets
            open_tickets = await self.get_user_tickets(user.id)

            tickets_list = "\n".join([
                f"#{t['ticket_number']} - {t['ticket_type'].capitalize()} (Status: {t['status'].replace('_', ' ').title()})"
                for t in open_tickets
            ])

            embed = discord.Embed(
                title="Can't Create Ticket",
                description=f"{error_msg}\n\n**Your current tickets:**\n{tickets_list}"
            )

            try:
                await user.send(embed=embed)
            except discord.Forbidden:
                logger.info(f"[Discord] Cannot send error message to {user.name}. DMs are disabled.")
            except Exception as e:
                logger.error(f"[Discord] Failed to send Discord error message: {e}")

            return

        embed = discord.Embed(
            title="Create Support Ticket",
            description=(
                "Please select the type of issue you're experiencing:\n\n"
                "**Discord-related issue** - Problems related to the Discord server\n"
                "**Boosteroid Service Issue** - Problems with the Boosteroid service\n\n"
                "*You have 5 minutes to make a selection*"
            ),
            color=discord.Color.blue()
        )

        view = TicketTypeView(self)

        try:
            await user.send(embed=embed, view=view)
            logger.info(f"[Discord] Sent ticket menu to {user.name}")
        except discord.Forbidden:
            logger.info(f"[Discord] Cannot send Discord ticket menu to {user.name}. DMs are disabled.")
        except Exception as e:
            logger.error(f"[Discord] Failed to send Discord ticket menu: {e}")

    async def create_ticket(self, user: discord.User, ticket_data: dict) -> bool:
        """
            Creating new ticket
        """

        try:
            # Checking limits
            can_create, error_msg = await self.can_create_ticket(user.id)

            if not can_create:
                try:
                    await user.send(error_msg)
                except Exception as e:
                    logger.warning(f"[Discord] Cannot send error message to {user.name}. Error: {e}")
                    pass
                return False

            guild = self.bot.get_guild(self.guild_id)
            if not guild:
                logger.error(f"[Discord] Guild {self.guild_id} does not exist")
                return False

            category = guild.get_channel(self.modmail_category_id)
            if not category:
                logger.error(f"[Discord] Category {self.modmail_category_id} does not exist")
                return False

            # Get the number of ticket
            ticket_number = await self.get_user_ticket_number(user.id)
            ticket_type = ticket_data.get('type', 'unknown')

            # Personal-data rule: what the user typed into "Email or session" is turned into the User ID
            # before it is shown to moderators or stored with the ticket. An email that cannot be
            # resolved is not kept at all.
            _eos_raw = (ticket_data.get('email_or_session') or '').strip()
            # Users often write more than the bare value ("Name, Session ID: 01a1..."): take the email or
            # session ID out of the text.
            if _eos_raw and not (_EMAIL_REGEX.fullmatch(_eos_raw) or _SESSION_ID_REGEX.fullmatch(_eos_raw) or _eos_raw.isdigit()):
                _found = _EMAIL_REGEX.search(_eos_raw) or _SESSION_ID_REGEX.search(_eos_raw)
                if _found:
                    _eos_raw = _found.group(0)
            _eos_uid = None
            if _eos_raw:
                _eos_failed = False
                try:
                    from services.mongo_db_service import ident_status
                    _eos_uid, _eos_st = await asyncio.to_thread(ident_status, _eos_raw)
                except Exception as e:
                    # The lookup itself failed (cloud API down, DB error): say so, don't call it "not found".
                    logger.warning(f"[Discord] User ID lookup failed for ticket #{ticket_number}: {type(e).__name__}: {e}")
                    _eos_uid, _eos_failed = None, True
                if _eos_uid:
                    ticket_data = dict(ticket_data, email_or_session=f'User ID {_eos_uid}')
                elif '@' in _eos_raw:
                    ticket_data = dict(ticket_data, email_or_session=(
                        'User ID lookup failed - add the User ID by hand' if _eos_failed
                        else 'not resolved - add the User ID by hand'))

            # Creating the channel
            channel_name = f"{ticket_type}-{user.name}-{ticket_number}".lower()
            try:
                channel = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    topic=f"ModMail ticket #{ticket_number} for {user.name} ({user.id}) | Type: {ticket_type}"
                )
            except discord.HTTPException as e:
                if e.code == 50035:
                    logger.error(f"[Discord] Category channel limit reached (50035) while creating ticket for {user.name}. Triggering emergency cleanup and retrying.")
                    await self._force_cleanup_oldest_closed_tickets(count=5)
                    channel = await guild.create_text_channel(
                        name=channel_name,
                        category=category,
                        topic=f"ModMail ticket #{ticket_number} for {user.name} ({user.id}) | Type: {ticket_type}"
                    )
                else:
                    raise

            logger.info(f"[Discord] Created channel {channel.name} for ticket #{ticket_number}")

            # Get info about user on the server
            try:
                member = await guild.fetch_member(user.id)
                user_roles = [role.name for role in member.roles if role.name != "@everyone"]
                joined_at = member.joined_at.strftime("%b %d, %Y") if member.joined_at else "Unknown"
            except Exception as e:
                user_roles = ["Not a member"]
                joined_at = "N/A"
                logger.warning(f"[Discord] {user.name} isn't a member of the Discord server, but tried to contact ModMail... | {e}")

            # Ticket information for the moderators
            embed = discord.Embed(
                title=f"New {ticket_type.upper()} Ticket #{ticket_number}",
                description=f"**User:** {user.mention} ({user.name}\n**ID:** {user.id}",
                color = discord.Color.blue() if ticket_type == "server" else discord.Color.red(),
                timestamp = datetime.utcnow()
            )

            if user.avatar:
                embed.set_thumbnail(url=user.avatar.url)

            # Additional fields for service issue
            if ticket_type == "service":
                embed.add_field(
                    name="Email / Session ID",
                    value=ticket_data.get('email_or_session', 'N/A'),
                    inline=False
                )

            # Issue description
            embed.add_field(
                name="Description",
                value=ticket_data.get('description', 'No description')[:1024],
                inline=False
            )

            # Footer with roles and date of join
            footer_text = f"Roles: {', '.join(user_roles[:5]) if user_roles else 'None'} | Joined: {joined_at}"
            embed.set_footer(text=footer_text)

            # Sending embed and ping Moderators
            await channel.send(
                content=f"<@&{self.modmail_mod_role_id}>",
                embed=embed
            )

            # Saving the ticket in MongoDB

            db_ticket_data = {
                'user_id': user.id,
                'user_name': user.name,
                'channel_id': channel.id,
                'ticket_type': ticket_type,
                'ticket_number': ticket_number,
                'status': 'open',
                'created_at': datetime.utcnow(),
                'first_response_at': None,
                'closed_at': None,
                'closed_by': None,
                'postponed_until': None,
                'postpone_reason': None,
                'postpone_reminder_sent': False,
                'messages_count': 0,
                'metadata': ticket_data,
                'messages_history': [],
                'conversation_html': None,
                # Denormalized last-message fields for the console sidebar (bug #7) + status flow (bug #3).
                'last_message': (ticket_data.get('description') or '')[:200],
                'last_message_ts': datetime.utcnow(),
                'last_message_is_user': True,
            }

            result = await self.tickets_collection.insert_one(db_ticket_data)

            # Auto-save email/session for service tickets
            if ticket_type == "service":
                email_or_session = str(_eos_uid or _eos_raw)
                if email_or_session:
                    try:
                        # Validate: only accept proper email or session ID format
                        is_valid_email = bool(_EMAIL_REGEX.fullmatch(email_or_session))
                        is_valid_session = bool(_SESSION_ID_REGEX.fullmatch(email_or_session)) or email_or_session.isdigit()

                        if not is_valid_email and not is_valid_session:
                            logger.info(f"[Discord] Skipping invalid email/session format for {user.name}: '{mask_email(email_or_session)}'")
                        else:
                            # Don't overwrite if email/session already saved
                            existing = await self.users_emails_collection.find_one(
                                {'discord_user_id': user.id}, {'cloud_user_id': 1, '_id': 0}
                            )
                            # Only a stored User ID stops the lookup. A legacy email on the record does
                            # not: this is the moment it gets replaced by the ID.
                            if existing and existing.get('cloud_user_id'):
                                logger.info(f"[Discord] User ID already saved for {user.name} ({user.id}), skipping lookup")
                            else:
                                now = datetime.utcnow()
                                await self.users_emails_collection.update_one(
                                    {'discord_user_id': user.id},
                                    {
                                        '$set': {
                                            # an email is stored as the User ID it resolves to (see _ident)
                                            **(await asyncio.to_thread(_ident_fields, email_or_session)),
                                            'discord_username': user.name,
                                            'platform': 'Discord',
                                            'updated_at': now,
                                        },
                                        '$setOnInsert': {
                                            'discord_user_id': user.id,
                                            'created_at': now,
                                        },
                                    },
                                    upsert=True,
                                )
                                logger.info(f"[Discord] Auto-saved User ID for {user.name} ({user.id})")
                    except Exception as e:
                        if type(e).__name__ == "IdentNotStored":
                            # Nothing is stored (never the address). Unknown account: silent. Cloud
                            # admin unavailable: tell the moderators to add the User ID by hand.
                            if getattr(e, "status", "") == "error":
                                try:
                                    from services.user_ref import MANUAL_NOTE
                                    await channel.send(MANUAL_NOTE)
                                except Exception as _e:
                                    logger.debug("ignored: %s", _e)
                        else:
                            logger.error(f"[Discord] Failed to auto-save User ID for {user.name} ({user.id}): {type(e).__name__}")

            await self._notify_slack("created", ticket_number, ticket_type, user.name, channel.id, user_id=user.id)
            logger.info(f"[Discord] Created {ticket_type} Discord ticket #{ticket_number} for {user.name} | MongoDB ID: {result.inserted_id}")

            confirmation_embed = discord.Embed(
                title="Ticket has been created!",
                description=(
                    f"Your **{ticket_type}** support ticket #{ticket_number} has been created. \n"
                    "A moderator will respond as soon as possible.\n\n"
                    "**Commands:**\n"
                    "`!status` - Check status of your tickets\n"
                    "`!new` - Create a new ticket"
                ),
                color = discord.Color.green()
            )

            try:
                await user.send(embed=confirmation_embed)
            except discord.Forbidden:
                logger.info(f"[Discord] Cannot send Discord ticket confirmation to {user.name}. DMs are disabled.")
            except Exception as e:
                logger.error(f"[Discord] Failed to send Discord confirmation to {user.name}. Error: {e}")

            return True
        except Exception as e:
            logger.error(f"[Discord] Failed to create Discord ticket: {e}")
            try:
                await user.send("An error occurred while creating your ticket. Please try again later.")
            except Exception as e:
                logger.error(f"[Discord] Cannot send Discord ticket creating error confirmation to {user.name}. Error: {e}")
                pass
            return False

    async def generate_ticket_html(self, ticket_id) -> str:
        """
        Build the ticket transcript HTML from its message history.

        Args:
            ticket_id: the ticket's ObjectId in MongoDB

        Returns:
            the transcript as an HTML string
        """
        try:
            # Load the ticket with its message history
            ticket = await self.tickets_collection.find_one(
                {'_id': ticket_id},
                {
                    'messages_history': 1,
                    'ticket_number': 1,
                    'ticket_type': 1,
                    'user_name': 1,
                    'created_at': 1,
                    'metadata': 1,
                }
            )

            if not ticket:
                return "<html><body>No message history available</body></html>"

            # HTML шаблон
            html_template = """<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Ticket #{ticket_number} - {ticket_type}</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                line-height: 1.6;
                max-width: 900px;
                margin: 0 auto;
                padding: 20px;
                background-color: #f8f9fa;
            }}
            .ticket-header {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 25px;
                border-radius: 10px;
                margin-bottom: 25px;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            }}
            .message {{
                background-color: white;
                border-radius: 10px;
                padding: 20px;
                margin-bottom: 15px;
                box-shadow: 0 2px 5px rgba(0,0,0,0.08);
                border: 1px solid #e9ecef;
            }}
            .user-message {{
                border-left: 5px solid #3498db;
                background-color: #f0f8ff;
            }}
            .mod-message {{
                border-left: 5px solid #9b59b6;
                background-color: #f9f0ff;
            }}
            .system-message {{
                border-left: 5px solid #f39c12;
                background-color: #fff8e1;
            }}
            .message-header {{
                display: flex;
                justify-content: space-between;
                margin-bottom: 12px;
                padding-bottom: 10px;
                border-bottom: 1px solid #eee;
            }}
            .author {{
                font-weight: bold;
                font-size: 16px;
                color: #2c3e50;
            }}
            .timestamp {{
                color: #7f8c8d;
                font-size: 14px;
            }}
            .content {{
                white-space: pre-wrap;
                word-wrap: break-word;
                font-size: 15px;
                line-height: 1.5;
            }}
            .avatar {{
                width: 40px;
                height: 40px;
                border-radius: 50%;
                margin-right: 10px;
                vertical-align: middle;
            }}
            .user-badge {{
                display: inline-block;
                padding: 3px 8px;
                border-radius: 12px;
                font-size: 12px;
                font-weight: bold;
                margin-left: 10px;
            }}
            .user-badge.user {{
                background-color: #3498db;
                color: white;
            }}
            .user-badge.moderator {{
                background-color: #9b59b6;
                color: white;
            }}
            .user-badge.system {{
                background-color: #f39c12;
                color: white;
            }}
        </style>
    </head>
    <body>
        <div class="ticket-header">
            <h1>Ticket #{ticket_number}</h1>
            <p><strong>Type:</strong> {ticket_type_upper}</p>
            <p><strong>User:</strong> {user_name}</p>
            <p><strong>Created:</strong> {created_date}</p>
            <p><strong>Total Messages:</strong> {total_messages}</p>
        </div>

        {messages_html}
    </body>
    </html>"""

            # Prepare the data
            ticket_number = ticket.get('ticket_number', '?')
            ticket_type = ticket.get('ticket_type', 'unknown')
            user_name = ticket.get('user_name', 'Unknown User')

            # Format the creation date
            created_at = ticket.get('created_at', datetime.utcnow())
            if isinstance(created_at, str):
                created_date = created_at
            else:
                created_date = created_at.strftime("%Y-%m-%d %H:%M:%S UTC")

            # Build synthetic initial message from form submission stored in metadata
            metadata = ticket.get('metadata') or {}
            description = metadata.get('description', '') or ''
            if ticket_type == 'service':
                email_or_session = metadata.get('email_or_session', '') or ''
                if email_or_session:
                    description = f"Email/Session: {email_or_session}\n\n{description}"
            initial_message = {
                'author_name': user_name,
                'content': description or '(no description)',
                'timestamp': created_at,
                'is_user': True,
            }

            all_messages = [initial_message] + list(ticket.get('messages_history') or [])
            total_messages = len(all_messages)

            # Build the HTML for the messages
            messages_html = ""
            for msg in all_messages:
                # Pick the CSS class and the badge
                if msg.get('is_system', False):
                    message_class = "system-message"
                    badge = "system"
                    author_prefix = ""
                elif msg.get('is_user', False):
                    message_class = "user-message"
                    badge = "user"
                    author_prefix = ""
                else:
                    message_class = "mod-message"
                    badge = "moderator"
                    author_prefix = ""

                # Format the time
                timestamp = msg.get('timestamp', datetime.utcnow())
                if isinstance(timestamp, str):
                    try:
                        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        time_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except:
                        time_str = timestamp
                else:
                    time_str = timestamp.strftime("%Y-%m-%d %H:%M:%S")

                # Escape HTML in the message text
                content = html.escape(msg.get('content', ''))

                # Build the HTML for one message
                messages_html += f"""
        <div class="message {message_class}">
            <div class="message-header">
                <div>
                    <span class="author">{author_prefix}{html.escape(msg.get('author_name', 'Unknown'))}</span>
                    <span class="user-badge {badge}">{badge.upper()}</span>
                </div>
                <span class="timestamp">{time_str}</span>
            </div>
            <div class="content">{content}</div>
        </div>
    """

            # Fill in the template
            final_html = html_template.format(
                ticket_number=ticket_number,
                ticket_type=ticket_type,
                ticket_type_upper=ticket_type.upper(),
                user_name=html.escape(user_name),
                created_date=created_date,
                total_messages=total_messages,
                messages_html=messages_html
            )

            return final_html

        except Exception as e:
            logger.error(f"Failed to generate HTML: {e}")
            return f"<html><body>Error generating HTML: {html.escape(str(e))}</body></html>"

    async def save_pdf_to_file(self, html_content: str, ticket_number: int) -> tuple[str, str]:
        """
        Converts HTML to PDF and saves to a temp file.
        Returns (file_path, extension) - falls back to HTML if PDF conversion fails.
        """
        try:
            from utils.general_utils import html_to_pdf
            pdf_bytes = html_to_pdf(html_content)

            temp_dir = tempfile.mkdtemp(prefix="modmail_pdf_")
            if pdf_bytes:
                file_path = os.path.join(temp_dir, f"ticket_{ticket_number}_conversation.pdf")
                with open(file_path, 'wb') as f:
                    f.write(pdf_bytes)
                logger.info(f"[Discord] PDF saved to temporary file: {file_path}")
                return file_path, 'pdf'
            else:
                file_path = os.path.join(temp_dir, f"ticket_{ticket_number}_conversation.html")
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(html_content)
                logger.info(f"[Discord] PDF failed, fell back to HTML: {file_path}")
                return file_path, 'html'

        except Exception as e:
            logger.error(f"[Discord] Failed to save file: {e}")
            raise

    async def schedule_close_ticket(self, channel: discord.TextChannel, closer: discord.User):
        """
            Schedule a silent ticket close in 1 hour.
            If the user or a moderator sends a new message in the ticket during that hour,
            the scheduled close is cancelled automatically.
            The user does NOT receive a DM when the close finally fires -
            the updated status is only visible via !status.
        """
        try:
            ticket = await self.tickets_collection.find_one({
                'channel_id': channel.id,
                'status': {'$in': ['open', 'in_progress']}
            })

            if not ticket:
                await channel.send("No open ticket found in this channel.")
                return

            ticket_id = ticket['_id']

            # Cancel any previous scheduled close for this ticket
            existing_task = self._scheduled_close_tasks.pop(ticket_id, None)
            if existing_task and not existing_task.done():
                existing_task.cancel()

            scheduled_until = datetime.utcnow() + timedelta(seconds=self.scheduled_close_delay_seconds)

            await self.tickets_collection.update_one(
                {'_id': ticket_id},
                {'$set': {'scheduled_close_until': scheduled_until,
                          'scheduled_close_by': closer.id if closer else None}}
            )

            task = self.bot.loop.create_task(
                self._run_scheduled_close(ticket_id, channel.id, closer)
            )
            self._scheduled_close_tasks[ticket_id] = task

            ticket_number = ticket.get('ticket_number', '?')
            embed = discord.Embed(
                title="Scheduled Close",
                description=(
                    f"Ticket #{ticket_number} will be closed <t:{int(scheduled_until.timestamp())}:R> "
                    f"(at <t:{int(scheduled_until.timestamp())}:T>).\n\n"
                    f"Any new message from the user or a moderator in this ticket will cancel the close."
                ),
                color=discord.Color.gold(),
                timestamp=datetime.utcnow()
            )
            embed.set_footer(text=f"Scheduled by {closer.name if closer else 'System'}")
            await channel.send(embed=embed)
            logger.info(f"[Discord] Scheduled close for ticket #{ticket_number} at {scheduled_until} UTC")
        except Exception as e:
            logger.error(f"[Discord] Failed to schedule close: {e}")

    async def _run_scheduled_close(self, ticket_id, channel_id: int, closer: discord.User):
        """
            Background task: sleep for the configured delay, then silently close the ticket.
            If ticket's scheduled_close_until is cleared in DB during the wait, bail out.
        """
        try:
            await asyncio.sleep(self.scheduled_close_delay_seconds)

            ticket = await self.tickets_collection.find_one({'_id': ticket_id})
            if not ticket:
                return
            if ticket.get('status') == 'closed':
                return
            if not ticket.get('scheduled_close_until'):
                # Was cancelled via new message
                return

            guild = self.bot.get_guild(self.guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if not channel:
                logger.warning(f"[Discord] Scheduled close: channel {channel_id} not found")
                return

            await self.close_ticket(channel, closer, notify_user=False)
        except asyncio.CancelledError:
            logger.info(f"[Discord] Scheduled close task for ticket {ticket_id} cancelled")
            raise
        except Exception as e:
            logger.error(f"[Discord] Scheduled close task failed: {e}")
        finally:
            self._scheduled_close_tasks.pop(ticket_id, None)

    async def cancel_scheduled_close(self, ticket: dict, triggered_by: str = "new message") -> bool:
        """
            Cancel a pending scheduled close for a ticket.
            Called automatically when a new message arrives in a ticket that has scheduled_close_until set.
            Returns True if a pending close was cancelled.
        """
        try:
            if not ticket.get('scheduled_close_until'):
                return False

            ticket_id = ticket['_id']
            task = self._scheduled_close_tasks.pop(ticket_id, None)
            if task and not task.done():
                task.cancel()

            await self.tickets_collection.update_one(
                {'_id': ticket_id},
                {'$unset': {'scheduled_close_until': "", 'scheduled_close_by': ""}}
            )

            ticket_number = ticket.get('ticket_number', '?')
            channel_id = ticket.get('channel_id')
            guild = self.bot.get_guild(self.guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if channel:
                try:
                    embed = discord.Embed(
                        title="Scheduled Close Cancelled",
                        description=f"Scheduled close for ticket #{ticket_number} cancelled ({triggered_by}).",
                        color=discord.Color.green(),
                        timestamp=datetime.utcnow()
                    )
                    await channel.send(embed=embed)
                except Exception as e:
                    logger.warning(f"[Discord] Could not post scheduled-close cancel embed: {e}")

            logger.info(f"[Discord] Scheduled close cancelled for ticket #{ticket_number} ({triggered_by})")
            return True
        except Exception as e:
            logger.error(f"[Discord] Failed to cancel scheduled close: {e}")
            return False

    async def close_ticket(self, channel: discord.TextChannel, closer: discord.User = None,
                           notify_user: bool = True) -> bool:
        """
            Ticket closing. If notify_user=False, the user will NOT receive a DM about the close
            (used by !sclose silent scheduled-close flow).
        """
        try:
            # Find a ticket by channel_id
            ticket = await self.tickets_collection.find_one({
                'channel_id': channel.id,
                'status': {'$in': ['open', 'in_progress']}
            })

            if not ticket:
                logger.warning(f"[Discord] No open ticket found for channel {channel.id}")
                return False

            user_id = ticket['user_id']
            ticket_number = ticket.get('ticket_number', '?')

            try:
                user = await self.bot.fetch_user(user_id)
            except Exception as e:
                logger.error(f"Failed to fetch user {user_id}: {e}")
                user = None

            html_content = await self.generate_ticket_html(ticket['_id'])

            temp_file_path, file_ext = await self.save_pdf_to_file(html_content, ticket_number)
            archive_filename = f"ticket_{ticket_number}_conversation.{file_ext}"

            with open(temp_file_path, 'rb') as f:
                file = discord.File(f, filename=archive_filename)

                embed = discord.Embed(
                    title="Conversation Archive",
                    description=f"Ticket #{ticket_number} — {len(ticket.get('messages_history', []))} message(s)",
                    color=discord.Color.green()
                )
                embed.add_field(name="File", value=f"`{archive_filename}`")
                embed.set_footer(text="Open the PDF to view the full conversation")

                await channel.send(embed=embed, file=file)

            try:
                os.remove(temp_file_path)
                os.rmdir(os.path.dirname(temp_file_path))
                logger.info(f"[Discord] Cleaned up temporary files for ticket #{ticket_number}")
            except Exception as e:
                logger.warning(f"[Discord] Could not clean up temporary files: {e}")

            await self.tickets_collection.update_one(
                {'_id': ticket['_id']},
                {
                    '$set': {
                        'status': 'closed',
                        'closed_at': datetime.utcnow(),
                        'closed_by': closer.id if closer else None,
                        'conversation_html': html_content
                    }
                }
            )

            logger.info(f"Closed ticket #{ticket_number} for user {user_id} (channel: {channel.id})")

            await self._notify_slack("closed", ticket_number, ticket.get('ticket_type', ''), closer.name if closer else "System", channel.id)

            # Notify the user via DM (skipped for silent scheduled close via !sclose)
            if user and notify_user:
                try:
                    close_embed = discord.Embed(
                        title="Ticket Closed",
                        description=(
                            f"Your ticket #{ticket_number} ({ticket['ticket_type']}) has been closed.\n\n"
                            "To create a new ticket, send me a message or use `!new`."
                        ),
                        color = discord.Color.orange(),
                        timestamp = datetime.utcnow()
                    )
                    await user.send(embed=close_embed)
                except discord.Forbidden:
                    logger.info(f"Cannot send Discord ticket close notification to {user.name}. DMs are disabled")
                except Exception as e:
                    logger.error(f"Failed to send Discord ticket close notification to {user.name}. Error: {e}")

            # Statistics in channel (always shown for moderators)
            time_alive = datetime.utcnow() - ticket['created_at']
            hours_alive = int(time_alive.total_seconds() // 3600)

            embed = discord.Embed(
                title="Ticket Closed",
                description=f"Closed by {closer.mention if closer else 'System'}",
                color = discord.Color.red(),
                timestamp = datetime.utcnow()
            )
            embed.add_field(name="Ticket Number", value=f"#{ticket_number}")
            embed.add_field(name="Type", value=ticket['ticket_type'].upper())
            embed.add_field(name="Messages", value=str(ticket.get('messages_count', 0)))
            embed.add_field(name="Time Alive", value=f"{hours_alive}h")

            if ticket.get('first_response_at'):
                response_time = ticket['first_response_at'] - ticket['created_at']
                response_minutes = int(response_time.total_seconds() // 60)
                embed.add_field(name="First Response Time", value=f"{response_minutes}m")

            try:
                await channel.send(embed=embed)
            except Exception as e:
                logger.warning(f"[Discord] Cannot send Discord ticket closing statistics for ticket #{ticket_number}. Error: {e}")

            # Move channel to closed category (always)
            try:
                guild = self.bot.get_guild(self.guild_id)
                closed_category = guild.get_channel(self.closed_tickets_category_id)
                await channel.edit(category=closed_category)
                logger.info(f"[Discord] Moved channel to closed category for ticket #{ticket_number}")
            except Exception as e:
                logger.error(f"[Discord] Failed to move closed ticket channel {channel.name}. Error: {e}")

            return True
        except Exception as e:
            logger.error(f"[Discord] Failed to close ticket: {e}")
            return False

    async def forward_to_channel(self, user: discord.User, message: discord.Message, ticket: dict) -> bool:
        """
            Forwarding the user's message to the ticket channel
        """
        try:
            channel = self.bot.get_channel(ticket['channel_id'])

            if not channel:
                logger.error(f"[Discord] Channel {ticket['channel_id']} not found for ticket #{ticket.get('ticket_number')}")
                return False

            # Embed with the user's message
            embed = discord.Embed(
                description = message.content if message.content else "*No text provided*",
                color = discord.Color.green(),
                timestamp = datetime.utcnow()
            )
            embed.set_author(
                name=user.name,
                icon_url=user.avatar.url if user.avatar else user.default_avatar.url,
            )

            # Attachments
            if message.attachments:
                files_text = "\n".join([f"[{att.filename}]({att.url})" for att in message.attachments])
                embed.add_field(name="Attachments", value=files_text, inline=False)

            # Footer with roles and the data of join
            try:
                guild = self.bot.get_guild(self.guild_id)
                member = await guild.fetch_member(user.id)
                user_roles = [role.name for role in member.roles if role.name != "@everyone"]
                joined_at = member.joined_at.strftime("%b %d, %Y") if member.joined_at else "Unknown"
                footer_text = f"Roles: {', '.join(user_roles[:5]) if user_roles else 'None'} | Joined: {joined_at}"
                embed.set_footer(text=footer_text)
            except Exception as e:
                logger.warning(f"[Discord] Failed to attach the footer to the Discord ticket message. Error: {e}")
                pass

            await channel.send(embed=embed)
            if ticket.get('auto_link_postpone_pending'):
                await self.tickets_collection.update_one(
                    {'_id': ticket['_id'], 'auto_link_postpone_pending': True},
                    {'$set': {'auto_link_postpone_at': datetime.utcnow() + timedelta(hours=1)}},
                )
            _now = datetime.utcnow()
            _last = self._message_notify_cooldown.get(user.id)
            if _last is None or (_now - _last).total_seconds() >= 300:
                self._message_notify_cooldown[user.id] = _now
                await self._notify_slack(
                    "message",
                    ticket.get('ticket_number'),
                    ticket.get('ticket_type', ''),
                    user.name,
                    channel.id,
                    jira_ticket_key=ticket.get('jira_ticket_key'),
                    preview=message.content or "",
                )

            # If ticket is postponed - cancel postpone due to user activity
            if ticket.get('postponed_until'):
                await self._cancel_postpone(
                    channel, ticket,
                    triggered_by=f"message from user {user.name}"
                )
                ticket = await self.tickets_collection.find_one({'_id': ticket['_id']})
                # jira_waiting_reply is cleared inside _cancel_postpone

            # If ticket has a scheduled !sclose - cancel it due to user activity
            if ticket.get('scheduled_close_until'):
                await self.cancel_scheduled_close(
                    ticket,
                    triggered_by=f"message from user {user.name}"
                )
                ticket = await self.tickets_collection.find_one({'_id': ticket['_id']})

            # Add message to the database
            message_data = {
                'message_id': str(message.id),
                'author_name': f"{user.name}",
                'content': _content_with_attachments(message),
                'timestamp': datetime.utcnow(),
                'is_user': True,
            }

            try:
                await self.tickets_collection.update_one(
                    {'_id': ticket['_id']},
                    {
                        '$push': {'messages_history': message_data},
                        '$inc': {'messages_count': 1},
                        '$set': {
                            'last_message': (message_data['content'] or '')[:200],
                            'last_message_ts': message_data['timestamp'],
                            'last_message_is_user': True,
                            'last_user_message_at': message_data['timestamp'],
                        },
                    }
                )
                logger.info(f"[Discord] Added message to the database for Discord ticket #{ticket.get('ticket_number')}")
            except Exception as e:
                logger.error(f"[Discord] Failed to add message to the database for Discord ticket #{ticket.get('ticket_number')}. Error: {e}")

            logger.info(f"[Discord] Forwarded message from {user.name} to ticket #{ticket.get('ticket_number')} channel")
            return True
        except Exception as e:
            logger.error(f"[Discord] Failed to forward Discord ticket message to channel: {e}")
            return False

    async def forward_to_user(self, moderator: discord.User, message: discord.Message, ticket: dict) -> bool:
        """
            Forward the moderator's message to the user
        """
        user_id = ticket['user_id']
        ticket_number = ticket.get('ticket_number', '?')

        try:
            user = await self.bot.fetch_user(user_id)
        except Exception as e:
            logger.error(f"[Discord] Failed to fetch user {user_id}: {e}")
            await message.add_reaction("❌")
            return False

        try:
            # Embed with moderator's answer
            embed = discord.Embed(
                description=message.content if message.content else "*No text provided*",
                color = discord.Color.blue(),
                timestamp=datetime.utcnow()
            )

            # Header - Boosteroid
            embed.set_author(
                name="Boosteroid Moderation",
                icon_url=message.guild.icon.url if message.guild and message.guild.icon else None
            )

            # Footer with Boosteroid Cloud Gaming
            embed.set_footer(
                text="Boosteroid Cloud Gaming",
            )

            await user.send(embed=embed)
            await message.add_reaction("✅")

            # If ticket has a scheduled !sclose - cancel it due to moderator activity
            if ticket.get('scheduled_close_until'):
                await self.cancel_scheduled_close(
                    ticket,
                    triggered_by=f"reply from moderator {moderator.name}"
                )
                ticket = await self.tickets_collection.find_one({'_id': ticket['_id']})

            # Add a message to the database
            message_data = {
                'message_id': str(message.id),
                'author_name': f"{moderator.name}",
                'content': _content_with_attachments(message),
                'timestamp': datetime.utcnow(),
                'is_user': False,
            }

            try:
                await self.tickets_collection.update_one(
                    {'_id': ticket['_id']},
                    {
                        '$push': {'messages_history': message_data},
                        '$inc': {'messages_count': 1},
                        '$set': {
                            'last_message': (message_data['content'] or '')[:200],
                            'last_message_ts': message_data['timestamp'],
                            'last_message_is_user': False,
                        },
                    }
                )
                logger.info(f"[Discord] Added moderator's message to the database for Discord ticket #{ticket_number}")
            except Exception as e:
                logger.error(f"[Discord] Failed to add moderator's message to the database for Discord ticket #{ticket_number}. Error: {e}")

            # Update the status to "in_progress" if it was a first answer
            if ticket['status'] == 'open' and not ticket.get('first_response_at'):
                await self.tickets_collection.update_one(
                    {'_id': ticket['_id']},
                    {
                        '$set': {
                            'status': 'in_progress',
                            'first_response_at': datetime.utcnow()
                        }
                    }
                )
                logger.info(f"[Discord] Discord ticket #{ticket_number} status changed to 'in_progress'")
            logger.info(f"[Discord] Forwarded moderator's response to {user.name} for ticket #{ticket_number}")
            return True

        except discord.Forbidden:
            await message.add_reaction("❌")
            await message.channel.send("Cannot send DM to user. They may have DMs disabled.")
            logger.warning(f"[Discord] Cannot send DM to {user.name} for Discord ticket #{ticket_number}")
            return False
        except Exception as e:
            await message.add_reaction("❌")
            await message.channel.send(f"Error sending message: {str(e)}")
            logger.error(f"[Discord] Failed to forward Discord ticket message to {user.name}. Error: {e}")
            return False

    async def send_console_reply(self, ticket: dict, text: str, mod_name: str, is_note: bool = False) -> bool:
        """Send a reply on a ticket on behalf of a CMT Console user (via the console_actions queue).

        is_note=False -> relay to the user's DM like `!say` (records + marks in_progress).
        is_note=True  -> post an internal note to the ticket channel (mods only, not sent to user).
        Mirrors forward_to_user so console replies behave exactly like in-Discord ones.
        """
        text = (text or "").strip()
        if not text:
            return False
        now = datetime.utcnow()
        message_data = {
            'message_id': f"console-{int(now.timestamp() * 1000)}",
            'author_name': mod_name or "Console",
            'content': text,
            'timestamp': now,
            'is_user': False,
        }

        if is_note:
            message_data['note'] = True
            channel = self.bot.get_channel(ticket.get('channel_id'))
            if channel:
                await channel.send(text)   # plain channel message, like a moderator just typed it
            await self.tickets_collection.update_one(
                {'_id': ticket['_id']},
                {'$push': {'messages_history': message_data}, '$inc': {'messages_count': 1}},
            )
            logger.info(f"[Discord] Console internal note on ticket #{ticket.get('ticket_number')}")
            return True

        # Relay to the user (like !say) - use the server icon so it matches in-Discord replies
        guild = self.bot.get_guild(self.guild_id)
        icon_url = guild.icon.url if guild and guild.icon else None
        user = await self.bot.fetch_user(ticket['user_id'])
        embed = discord.Embed(description=text, color=discord.Color.blue(), timestamp=now)
        embed.set_author(name="Boosteroid Moderation", icon_url=icon_url)
        embed.set_footer(text="Boosteroid Cloud Gaming")
        await user.send(embed=embed)

        # Mirror the reply into the ticket channel so mods see it (console has no !say message there)
        ch = self.bot.get_channel(ticket.get('channel_id'))
        if ch:
            try:
                ch_embed = discord.Embed(description=text, color=discord.Color.blue(), timestamp=now)
                ch_embed.set_author(name=f"Reply to user - {mod_name} (via console)", icon_url=icon_url)
                await ch.send(embed=ch_embed)
            except Exception as e:
                logger.error(f"[Discord] Failed to mirror console reply into channel: {e}")

        if ticket.get('scheduled_close_until'):
            try:
                await self.cancel_scheduled_close(ticket, triggered_by=f"console reply from {mod_name}")
                ticket = await self.tickets_collection.find_one({'_id': ticket['_id']})
            except Exception as e:
                logger.error(f"[Discord] cancel_scheduled_close failed on console reply: {e}")

        # Denormalize the last message so the console sidebar/status update in real time after a reply
        # sent from the console, not just from Discord (bugs #6/#7), and flip the flow to Waiting on user.
        set_fields = {
            'last_message': text[:200],
            'last_message_ts': now,
            'last_message_is_user': False,
        }
        if ticket.get('status') == 'open' and not ticket.get('first_response_at'):
            set_fields['status'] = 'in_progress'
            set_fields['first_response_at'] = now
        await self.tickets_collection.update_one(
            {'_id': ticket['_id']},
            {'$push': {'messages_history': message_data}, '$inc': {'messages_count': 1},
             '$set': set_fields},
        )
        logger.info(f"[Discord] Console reply relayed to user for ticket #{ticket.get('ticket_number')}")
        # Same as !say: in Jira ticket-mode a reply to the user starts the 5-day wait for the user's answer.
        if ticket.get('jira_mode_unpostponed') and ch:
            try:
                await self._jira_mod_replied(ch, ticket)
            except Exception as e:
                logger.error(f"[Discord] Jira-mode follow-up after console reply failed: {e}")
        return True

    async def _send_maintenance_text_to_user(self, channel: discord.TextChannel, text: str) -> bool:
        """Relays a maintenance-end message to the ticket's end user (DM) and confirms in the channel."""
        try:
            ticket = await self.tickets_collection.find_one({
                'channel_id': channel.id, 'status': {'$in': ['open', 'in_progress']}
            }) or await self.tickets_collection.find_one({'channel_id': channel.id})
            if not ticket:
                await channel.send('No ticket found for this channel.')
                return False

            user = await self.bot.fetch_user(ticket['user_id'])
            guild = self.bot.get_guild(self.guild_id)
            embed = discord.Embed(description=text, color=discord.Color.blue(), timestamp=datetime.utcnow())
            embed.set_author(
                name='Boosteroid Moderation',
                icon_url=guild.icon.url if guild and guild.icon else None,
            )
            embed.set_footer(text='Boosteroid Cloud Gaming')
            await user.send(embed=embed)

            await channel.send(embed=discord.Embed(
                description='Maintenance-end message sent to the user.',
                color=discord.Color.green(), timestamp=datetime.utcnow(),
            ))
            logger.info(f"[Discord] Sent maintenance-end message to user {ticket['user_id']} for channel {channel.id}")
            return True
        except discord.Forbidden:
            await channel.send('Cannot send DM to the user. They may have DMs disabled.')
            return False
        except Exception as e:
            logger.error(f'[Discord] Failed to send maintenance text to user for channel {channel.id}: {e}')
            return False

    async def _deliver_maintenance_notification(self, sub: dict) -> None:
        """Sends the maintenance-end message to the ticket's user and moves the chat to Waiting on user.
        If the DM can't be delivered (DMs closed, ticket missing), falls back to the Send/Dismiss
        embed so a moderator can handle it."""
        subs = self.db['maintenance_subscriptions']
        try:
            from services.maintenance_notify_service import render_message, set_console_waiting_on_user
            channel_id = int(sub.get('target_id'))
            channel = self.bot.get_channel(channel_id)
            if not channel:
                await subs.update_one({'_id': sub['_id']}, {'$set': {'status': 'notified'}})
                logger.warning(f'[Discord] Maintenance target channel {channel_id} not found — marked notified')
                return
            text = render_message(sub.get('entity', ''), sub.get('language', 'en'), sub.get('type', 'game'))
            ticket = await self.tickets_collection.find_one({
                'channel_id': channel_id, 'status': {'$in': ['open', 'in_progress']}
            }) or await self.tickets_collection.find_one({'channel_id': channel_id})
            if ticket:
                try:
                    if await self.send_console_reply(ticket, text, 'Maintenance notice'):
                        await subs.update_one(
                            {'_id': sub['_id']},
                            {'$set': {'status': 'notified', 'notified_at': datetime.utcnow()}},
                        )
                        await asyncio.to_thread(set_console_waiting_on_user, ticket['_id'])
                        logger.info(f'[Discord] Sent maintenance-end message to the user for "{sub.get("entity")}" (channel {channel_id})')
                        return
                except Exception as e:
                    logger.warning(f'[Discord] Auto-send of maintenance-end message failed for channel {channel_id}: {e}')
            embed = discord.Embed(
                title='Maintenance Completed — Action Required',
                description=(
                    f'Maintenance for **{sub.get("entity")}** has ended.\n\n'
                    f'Press **Send** to relay this message to the user:\n\n{text}'
                ),
                color=discord.Color.green(),
                timestamp=datetime.utcnow(),
            )
            await channel.send(
                content=f'<@&{self.modmail_mod_role_id}>',
                embed=embed,
                view=MaintenanceSendView(self, text),
            )
            await subs.update_one(
                {'_id': sub['_id']},
                {'$set': {'status': 'notified', 'notified_at': datetime.utcnow()}},
            )
            logger.info(f'[Discord] Posted maintenance-end notification for "{sub.get("entity")}" in channel {channel_id}')
        except Exception as e:
            logger.error(f'[Discord] Failed to deliver maintenance notification {sub.get("_id")}: {e}')

    async def _cancel_postpone(self, channel: discord.TextChannel, ticket: dict, triggered_by: str = "new activity") -> None:
        """
            Cancel active postpone: move channel back to main modmail category,
            clear DB fields, notify in channel.
            Uses a single channel.edit() call to avoid hitting the 2-edits-per-10-min rate limit.

            If the ticket was in a Jira-linked postpone state (waiting_reply / jira_linked),
            the embed includes which Jira ticket was involved and a button to restore the state.
        """
        # Capture Jira state BEFORE clearing DB fields
        jira_waiting = bool(ticket.get('jira_waiting_reply'))
        jira_linked = bool(ticket.get('waiting_for_jira') and ticket.get('jira_ticket_key'))
        issue_key = ticket.get('jira_ticket_key', '')

        await self.tickets_collection.update_one(
            {'_id': ticket['_id']},
            {'$set': {
                'postponed_until': None,
                'postpone_reminder_sent': True,
                # Always clear jira_waiting_reply so the auto-close check can't
                # fire on a future postpone that has nothing to do with Jira.
                # The value is captured above (jira_waiting) before this update.
                'jira_waiting_reply': False,
            }}
        )

        # Build kwargs for a single combined edit (name + category)
        edit_kwargs = {}
        new_name = channel.name.replace("🕐-", "")
        if new_name != channel.name:
            edit_kwargs['name'] = new_name

        guild = self.bot.get_guild(self.guild_id)
        if guild and channel.category_id != self.modmail_category_id:
            modmail_category = guild.get_channel(self.modmail_category_id)
            if modmail_category:
                edit_kwargs['category'] = modmail_category

        if edit_kwargs:
            edit_success = await self.safe_edit_channel(channel, **edit_kwargs)
            if not edit_success:
                logger.warning(f"[Discord] Could not rename/move channel for ticket #{ticket.get('ticket_number')} due to rate limit")
            else:
                logger.info(f"[Discord] Moved ticket #{ticket.get('ticket_number')} back to main modmail category")

        # Build embed description and optional restore view based on Jira state
        jira_url = f'{self.jira_base_url}/browse/{issue_key}' if (issue_key and self.jira_base_url) else issue_key
        restore_view = None

        if jira_waiting:
            description = (
                f'Postpone was cancelled due to {triggered_by}.\n\n'
                f'This ticket was in **Waiting for User Reply** state, '
                f'linked to Jira ticket **{issue_key}**.\n'
                f'{jira_url}'
            )
            restore_view = RestoreJiraPostponeView(self, ticket['_id'], 'waiting_reply', issue_key)
        elif jira_linked:
            description = (
                f'Postpone was cancelled due to {triggered_by}.\n\n'
                f'This ticket was in **Jira Ticket Linked** state, '
                f'waiting for **{issue_key}** to move to CM In Progress.\n'
                f'{jira_url}'
            )
            restore_view = RestoreJiraPostponeView(self, ticket['_id'], 'jira_linked', issue_key)
        else:
            description = f'Postpone was cancelled due to {triggered_by}.'

        embed = discord.Embed(
            title="Postpone Cancelled",
            description=description,
            color=discord.Color.yellow(),
            timestamp=datetime.utcnow()
        )
        if restore_view:
            await channel.send(content=f"<@&{self.modmail_mod_role_id}>", embed=embed, view=restore_view)
        else:
            await channel.send(content=f"<@&{self.modmail_mod_role_id}>", embed=embed)
        logger.info(f"[Discord] Postpone cancelled for ticket #{ticket.get('ticket_number')} — {triggered_by}")

    async def _has_open_jira(self, ticket: dict) -> bool:
        """True if the ticket has at least one linked Jira ticket that is not yet Done."""
        keys = _jira_keys_of(ticket)
        if not keys:
            return False
        from services.jira_service import is_issue_done
        for key in keys:
            try:
                done = await asyncio.to_thread(is_issue_done, key)
            except Exception as e:
                logger.error(f'[Discord] Failed to check Jira done state for {key}: {e}')
                done = False
            if not done:
                return True
        return False

    async def postpone_ticket(self, channel: discord.TextChannel, moderator: discord.User, time_str: str, reason: str = None, mod_display: str = None) -> bool:
        """
            Postpone the ticket on the exact time.
            `mod_display` lets a non-Discord caller (e.g. the console worker) supply a display name
            when there is no `moderator` object; command callers leave it None and keep the mention.
        """
        try:
            # Find a ticket
            ticket = await self.tickets_collection.find_one({
                'channel_id': channel.id,
                'status': {'$in': ['open', 'in_progress']}
            })

            if not ticket:
                await channel.send("No active ticket found for this channel.")
                return False

            # Parsing a time
            time_units = {
                'm': 'minutes',
                'h': 'hours',
                'd': 'days',
                'w': 'weeks'
            }

            try:
                unit = time_str[-1].lower()
                amount = int(time_str[:-1])

                if unit not in time_units:
                    await channel.send("Invalid time format. Use: 30m, 2h, 1d, 1w.")
                    return False

                # Max 30 days
                if unit == 'w' and amount > 4:
                    await channel.send("Maximum postpone time is 30 days (4 weeks)")
                    return False
                if unit == 'd' and amount > 30:
                    await channel.send("Maximum postpone time is 30 days.")
                    return False

                # Counting a time
                delta_kwargs = {time_units[unit]: amount}
                postpone_until = datetime.utcnow() + timedelta(**delta_kwargs)

            except (ValueError, KeyError):
                await channel.send("Invalid time format. Use: 30m, 2h, 1d, 1w")
                return False

            # Updating the ticket
            await self.tickets_collection.update_one(
                {'_id': ticket['_id']},
                {
                    '$set': {
                        'postponed_until': postpone_until,
                        'postpone_reason': reason or 'No reason provided',
                        'postpone_reminder_sent': False
                    }
                }
            )

            # Rename + move to the right category in a single edit to save rate-limit slots.
            # Tickets with a still-open linked Jira ticket go to the Jira ("postponed modmail")
            # category; everything else goes to the regular waiting/postponed category.
            new_name = f"🕐-{channel.name.replace('🕐-', '')}"
            edit_kwargs = {'name': new_name}

            guild = self.bot.get_guild(self.guild_id)
            if guild:
                target_cat_id = self.waiting_category_id
                if self.jira_category_id and await self._has_open_jira(ticket):
                    target_cat_id = self.jira_category_id
                if target_cat_id:
                    target_category = guild.get_channel(target_cat_id)
                    if target_category:
                        edit_kwargs['category'] = target_category

            edit_success = await self.safe_edit_channel(channel, **edit_kwargs)

            if not edit_success:
                logger.warning(
                    f"Could not rename/move channel for ticket #{ticket.get('ticket_number')} "
                    f"due to rate limit, but postpone was saved"
                )
            else:
                logger.info(f"[Discord] Renamed and moved ticket #{ticket.get('ticket_number')} to waiting category")

            # Channel notification
            embed = discord.Embed(
                title = "Ticket Postponed",
                description = (
                    f"This ticket has been postponed by {mod_display or (moderator.mention if moderator else 'a moderator')}\n"
                    f"**Until:** <t:{int(postpone_until.timestamp())}:F>\n"
                    f"**Reason:** {reason or 'No reason provided'}"
                ),
                color = discord.Color.orange(),
                timestamp = datetime.utcnow()
            )

            await channel.send(embed=embed)
            logger.info(f"[Discord] Postponed ticket #{ticket.get('ticket_number')} until {postpone_until}")

            return True
        except Exception as e:
            logger.error(f"[Discord] Failed to postpone Discord ticket: {e}")
            await channel.send(f"Failed to postpone ticket: {str(e)}")
            return False

    async def get_ticket_status_embed(self, user_id: int, detailed: bool = False) -> discord.Embed:
        """
            Get embed with statuses of tickets of user
        """

        # Get all user's ticket
        all_tickets = await self.tickets_collection.find(
            {'user_id': user_id}
        ).sort('created_at', -1).to_list(length=100)

        if not all_tickets:
            embed = discord.Embed(
                title = "Your Tickets",
                description = "You don't have any tickets yet. \nSend me a message to create one!",
                color = discord.Color.blue()
            )
            return embed

        open_tickets = [t for t in all_tickets if t['status'] == 'open']
        in_progress_tickets = [t for t in all_tickets if t['status'] == 'in_progress']
        closed_tickets = [t for t in all_tickets if t['status'] == 'closed']

        embed = discord.Embed(
            title = "Your Tickets",
            color = discord.Color.blue(),
            timestamp = datetime.utcnow()
        )

        # Open tickets
        if open_tickets:
            open_text = "\n".join([
                f"**#{t['ticket_number']}** - {t['ticket_type'].capitalize()} | Created: <t:{int(t['created_at'].timestamp())}:R>"
                for t in open_tickets[:5]
            ])
            embed.add_field(name="Open Tickets", value=open_text, inline=False)

        # In Progress tickets
        if in_progress_tickets:
            progress_text = "\n".join([
                f"**#{t['ticket_number']}** - {t['ticket_type'].capitalize()} | Created: <t:{int(t['created_at'].timestamp())}:R>"
                for t in in_progress_tickets[:5]
            ])
            embed.add_field(name="In Progress", value=progress_text, inline=False)

        # Recently closed (last 3)
        if closed_tickets:
            closed_text = "\n".join([
                f"**#{t['ticket_number']}** - {t['ticket_type'].capitalize()} | Closed: <t:{int(t['closed_at'].timestamp())}:R>"
                for t in closed_tickets[:3]
            ])
            embed.add_field(name="Recently Closed", value=closed_text, inline=False)

        embed.set_footer(text=f"Total tickets: {len(all_tickets)}")

        return embed

    # ------------------------------------------------------------------
    # Jira ticket-mode helpers
    # ------------------------------------------------------------------

    async def _enter_jira_mode(self, channel: discord.TextChannel, issue_key: str, ticket: dict) -> None:
        try:
            await self.tickets_collection.update_one(
                {'_id': ticket['_id']},
                {'$set': {'jira_ticket_key': issue_key, 'waiting_for_jira': True},
                 '$addToSet': {'jira_ticket_keys': issue_key}}
            )

            new_name = f'🕐-{channel.name.replace("🕐-", "")}'
            edit_kwargs = {'name': new_name}
            guild = self.bot.get_guild(self.guild_id)
            target_cat_id = self.jira_category_id or self.waiting_category_id
            if guild and target_cat_id:
                jira_category = guild.get_channel(target_cat_id)
                if jira_category:
                    edit_kwargs['category'] = jira_category
            await self.safe_edit_channel(channel, **edit_kwargs)

            jira_url = f'{self.jira_base_url}/browse/{issue_key}' if self.jira_base_url else issue_key
            embed = discord.Embed(
                title='Jira Ticket Linked',
                description=(
                    f'This ticket has been linked to Jira issue **{issue_key}**.\n'
                    f'The channel is on hold until **{issue_key}** moves to **CM In Progress** in Jira.\n\n'
                    f'{jira_url}'
                ),
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            msg = await channel.send(embed=embed)
            try:
                await msg.pin()
            except Exception as _e:
                logger.debug("ignored: %s", _e)
            # Record the linkage in the console transcript too: a mods-only note so the
            # console shows "Jira Ticket Linked" the same way the Discord channel does.
            try:
                _now = datetime.utcnow()
                _note = f'Jira Ticket Linked: {issue_key}. On hold until {issue_key} moves to CM In Progress in Jira. {jira_url}'
                await self.tickets_collection.update_one(
                    {'_id': ticket['_id']},
                    {'$push': {'messages_history': {
                        'message_id': f'jira-linked-{issue_key}-{int(_now.timestamp())}',
                        'author_name': 'ModMail', 'content': _note, 'timestamp': _now,
                        'is_user': False, 'note': True,
                    }},
                     '$inc': {'messages_count': 1}})
            except Exception as _e:
                logger.warning(f'[Discord] could not record Jira-linked note in transcript: {_e}')
            logger.info(f'[Discord] Entered Jira mode for ticket #{ticket.get("ticket_number")} - {issue_key}')
        except Exception as e:
            logger.error(f'[Discord] Failed to enter Jira mode for channel {channel.id}: {e}')

    async def _link_additional_jira(self, channel: discord.TextChannel, issue_key: str, ticket: dict, force: bool = False) -> None:
        """Links an extra Jira ticket to an already-Jira-linked Discord ticket.

        The channel's category / name / postpone / auto-close stay driven by the primary
        jira_ticket_key; this only records the extra key and posts a dismissible notice.
        Per-key status changes are surfaced later by _notify_secondary_jira_status.

        force=True skips the "already linked" guard: the console link path adds the key to the ticket
        doc itself before the bot runs, so the note must still post even though the key is present.
        """
        try:
            if not force and issue_key in _jira_keys_of(ticket):
                return
            jira_doc = await self.jira_tickets_collection.find_one({'issue_key': issue_key}, {'status': 1})
            init_status = jira_doc.get('status') if jira_doc else None
            await self.tickets_collection.update_one(
                {'_id': ticket['_id']},
                {'$addToSet': {'jira_ticket_keys': issue_key},
                 '$set': {f'jira_states.{issue_key}.last_status': init_status}}
            )
            jira_url = f'{self.jira_base_url}/browse/{issue_key}' if self.jira_base_url else issue_key
            embed = discord.Embed(
                title='Additional Jira Ticket Linked',
                description=f'This ticket is now also linked to Jira issue **{issue_key}**.\n\n{jira_url}',
                color=discord.Color.blue(),
                timestamp=datetime.utcnow(),
            )
            await channel.send(content=f'<@&{self.modmail_mod_role_id}>', embed=embed, view=DismissView())
            logger.info(f'[Discord] Linked additional Jira {issue_key} to ticket #{ticket.get("ticket_number")}')
        except Exception as e:
            logger.error(f'[Discord] Failed to link additional Jira {issue_key} for channel {channel.id}: {e}')

    async def console_link_jira(self, ticket: dict, issue_key: str, primary: bool) -> bool:
        """Link a Jira issue to a Discord ticket ON BEHALF OF THE CONSOLE, so a console link behaves
        exactly like the in-channel `!jira` command: post the channel note, move the primary link to
        the Jira category / on-hold state, and record the console transcript note. The console already
        wrote the key to the ticket doc, so the primary path reuses _enter_jira_mode (idempotent
        writes) and the additional path forces the note past the "already linked" guard.
        Returns True when the Discord-side effects ran, False when the channel could not be resolved."""
        ticket = await self.tickets_collection.find_one({'_id': ticket['_id']}) or ticket
        channel = None
        cid = ticket.get('channel_id')
        if cid is not None:
            try:
                channel = self.bot.get_channel(int(cid))
            except (TypeError, ValueError):
                channel = None
        if channel is None:
            logger.warning(f'[Discord] console_link_jira: channel {cid} not found for ticket '
                           f'#{ticket.get("ticket_number")} ({issue_key})')
            return False
        if primary:
            await self._enter_jira_mode(channel, issue_key, ticket)
        else:
            await self._link_additional_jira(channel, issue_key, ticket, force=True)
        return True

    async def _notify_secondary_jira_status(self, ticket: dict) -> None:
        """Posts a dismissible notification when a non-primary linked Jira ticket changes status."""
        try:
            primary = ticket.get('jira_ticket_key')
            secondary = [k for k in _jira_keys_of(ticket) if k != primary]
            if not secondary:
                return
            channel = self.bot.get_channel(ticket['channel_id'])
            if not channel:
                return
            states = ticket.get('jira_states') or {}
            for key in secondary:
                jira_doc = await self.jira_tickets_collection.find_one({'issue_key': key}, {'status': 1, 'url': 1})
                if not jira_doc:
                    continue
                current = jira_doc.get('status')
                last = (states.get(key) or {}).get('last_status')
                if not current or current == last:
                    continue
                jira_url = jira_doc.get('url') or ''
                if f'/browse/{key}' not in jira_url:   # old docs may hold a wrong link (e.g. battle.net from the summary)
                    jira_url = f'{self.jira_base_url}/browse/{key}' if self.jira_base_url else key
                embed = discord.Embed(
                    title='Jira Status Update',
                    description=f'Linked Jira **{key}** is now **{current}**.\n\n{jira_url}',
                    color=discord.Color.blurple(),
                    timestamp=datetime.utcnow(),
                )
                await channel.send(content=f'<@&{self.modmail_mod_role_id}>', embed=embed, view=DismissView())
                await self.tickets_collection.update_one(
                    {'_id': ticket['_id']},
                    {'$set': {f'jira_states.{key}.last_status': current}}
                )
                logger.info(f'[Discord] Secondary Jira {key} → {current} for ticket #{ticket.get("ticket_number")}')
        except Exception as e:
            logger.error(f'[Discord] Error notifying secondary Jira status for ticket {ticket.get("_id")}: {e}')

    async def _reenter_jira_mode(self, channel: discord.TextChannel, issue_key: str, ticket: dict, jira_doc: dict = None) -> None:
        """
        Re-enter Jira waiting mode when a linked Jira ticket moves back to Backlog.
        Resets all Jira state flags and moves the channel back to the jira category.
        Flow is identical to the initial _enter_jira_mode.
        """
        try:
            await self.tickets_collection.update_one(
                {'_id': ticket['_id']},
                {'$set': {
                    'waiting_for_jira': True,
                    'jira_mode_unpostponed': False,
                    'jira_waiting_reply': False,
                    'postponed_until': None,
                    'postpone_reminder_sent': True,
                }}
            )

            new_name = f'🕐-{channel.name.replace("🕐-", "")}'
            edit_kwargs = {'name': new_name}
            guild = self.bot.get_guild(self.guild_id)
            target_cat_id = self.jira_category_id or self.waiting_category_id
            if guild and target_cat_id:
                jira_category = guild.get_channel(target_cat_id)
                if jira_category:
                    edit_kwargs['category'] = jira_category
            await self.safe_edit_channel(channel, **edit_kwargs)

            jira_url = (
                (jira_doc.get('url') if jira_doc else None)
                or (f'{self.jira_base_url}/browse/{issue_key}' if self.jira_base_url else issue_key)
            )
            embed = discord.Embed(
                title='Jira Ticket — Back to Backlog',
                description=(
                    f'Jira ticket **{issue_key}** has moved back to **Backlog**.\n'
                    f'The channel is on hold until **{issue_key}** moves to **CM In Progress** again.\n\n'
                    f'{jira_url}'
                ),
                color=discord.Color.blue(),
                timestamp=datetime.utcnow()
            )
            msg = await channel.send(content=f'<@&{self.modmail_mod_role_id}>', embed=embed, view=DismissView())
            try:
                await msg.pin()
            except Exception as _e:
                logger.debug("ignored: %s", _e)
            logger.info(f'[Discord] Re-entered Jira mode for ticket #{ticket.get("ticket_number")} — {issue_key} back to Backlog')
        except Exception as e:
            logger.error(f'[Discord] Failed to re-enter Jira mode for channel {channel.id}: {e}')

    async def _unpostpone_jira_ticket(self, channel: discord.TextChannel, ticket: dict, jira_doc: dict) -> None:
        try:
            await self.tickets_collection.update_one(
                {'_id': ticket['_id']},
                {'$set': {
                    'waiting_for_jira': False,
                    'jira_mode_unpostponed': True,
                    'postpone_reminder_sent': True,
                }}
            )

            edit_kwargs = {}
            new_name = channel.name.replace('🕐-', '')
            if new_name != channel.name:
                edit_kwargs['name'] = new_name
            guild = self.bot.get_guild(self.guild_id)
            if guild and channel.category_id != self.modmail_category_id:
                modmail_category = guild.get_channel(self.modmail_category_id)
                if modmail_category:
                    edit_kwargs['category'] = modmail_category
            if edit_kwargs:
                await self.safe_edit_channel(channel, **edit_kwargs)

            issue_key = ticket.get('jira_ticket_key', '')
            jira_url = jira_doc.get('url') or (f'{self.jira_base_url}/browse/{issue_key}' if self.jira_base_url else '')

            embed = discord.Embed(
                title='Jira Ticket Ready — Action Required',
                description=(
                    f'Jira ticket **{issue_key}** has been moved to **CM In Progress**.\n\n'
                    f'**Please relay the information to the user** using `!say <message>`.\n\n'
                    f'After your reply the ticket will be auto-postponed for **5 days**.\n'
                    f'If the user does not respond within 5 days, the ticket will be closed automatically.\n\n'
                    f'{jira_url}'
                ),
                color=discord.Color.green(),
                timestamp=datetime.utcnow()
            )
            await channel.send(
                content=f'<@&{self.modmail_mod_role_id}>',
                embed=embed,
                view=JiraTransferredView(issue_key),
            )
            logger.info(f'[Discord] Unpostponed Jira ticket #{ticket.get("ticket_number")} — {issue_key}')
        except Exception as e:
            logger.error(f'[Discord] Failed to unpostpone Jira ticket for channel {channel.id}: {e}')

    async def _jira_mod_replied(self, channel: discord.TextChannel, ticket: dict) -> None:
        try:
            issue_key = ticket.get('jira_ticket_key', '')
            now = datetime.utcnow()
            postpone_until = now + timedelta(days=5)

            await self.tickets_collection.update_one(
                {'_id': ticket['_id']},
                {'$set': {
                    'postponed_until': postpone_until,
                    'postpone_reason': f'Waiting for user reply after Jira {issue_key}',
                    'postpone_reminder_sent': False,
                    'jira_mode_unpostponed': False,
                    'jira_waiting_reply': True,
                    'jira_postpone_set_at': now,
                }}
            )

            new_name = f'🕐-{channel.name.replace("🕐-", "")}'
            edit_kwargs = {'name': new_name}
            guild = self.bot.get_guild(self.guild_id)
            target_cat_id = self.jira_category_id or self.waiting_category_id
            if guild and target_cat_id:
                jira_category = guild.get_channel(target_cat_id)
                if jira_category:
                    edit_kwargs['category'] = jira_category
            await self.safe_edit_channel(channel, **edit_kwargs)

            # Delete the CM In Progress Slack notification
            if issue_key:
                jira_doc = await self.jira_tickets_collection.find_one(
                    {'issue_key': issue_key}, {'slack_channel': 1, 'slack_ts': 1}
                )
                if jira_doc and jira_doc.get('slack_channel') and jira_doc.get('slack_ts'):
                    try:
                        await self.slack_client.chat_delete(
                            channel=jira_doc['slack_channel'],
                            ts=jira_doc['slack_ts']
                        )
                        logger.info(f'[Discord] Deleted Slack CM In Progress notification for {issue_key}')
                    except Exception as e:
                        logger.warning(f'[Discord] Failed to delete Slack notification for {issue_key}: {e}')

            embed = discord.Embed(
                title='Waiting for User Reply',
                description=(
                    f'Information has been relayed to the user.\n'
                    f'The ticket is postponed for **5 days** (until <t:{int(postpone_until.timestamp())}:F>).\n'
                    f'If no response is received, the ticket will be closed automatically.\n'
                    f'You will be asked to update Jira **{issue_key}** via a button when that happens.'
                ),
                color=discord.Color.orange(),
                timestamp=now
            )
            await channel.send(embed=embed, view=DismissView())
            logger.info(f'[Discord] Jira mod replied for ticket #{ticket.get("ticket_number")} - postponed 5 days')
        except Exception as e:
            logger.error(f'[Discord] Failed to handle Jira mod reply for channel {channel.id}: {e}')

    async def _jira_restore_waiting_reply(self, channel: discord.TextChannel, ticket: dict) -> None:
        """
        Restore 'Waiting for User Reply' postpone state after it was cancelled.
        Called from RestoreJiraPostponeView. Behaves like _jira_mod_replied but
        does NOT re-post Jira comment or delete Slack notification (already done earlier).
        The 5-day countdown restarts from now, so the auto-close flow stays intact.
        """
        try:
            issue_key = ticket.get('jira_ticket_key', '')
            now = datetime.utcnow()
            postpone_until = now + timedelta(days=5)

            await self.tickets_collection.update_one(
                {'_id': ticket['_id']},
                {'$set': {
                    'postponed_until': postpone_until,
                    'postpone_reason': f'Waiting for user reply after Jira {issue_key}',
                    'postpone_reminder_sent': False,
                    'jira_mode_unpostponed': False,
                    'jira_waiting_reply': True,
                    'jira_postpone_set_at': now,
                }}
            )

            new_name = f'🕐-{channel.name.replace("🕐-", "")}'
            edit_kwargs = {'name': new_name}
            guild = self.bot.get_guild(self.guild_id)
            target_cat_id = self.jira_category_id or self.waiting_category_id
            if guild and target_cat_id:
                jira_category = guild.get_channel(target_cat_id)
                if jira_category:
                    edit_kwargs['category'] = jira_category
            await self.safe_edit_channel(channel, **edit_kwargs)

            embed = discord.Embed(
                title='Waiting for User Reply — Restored',
                description=(
                    f'The "Waiting for User Reply" status has been restored.\n'
                    f'The ticket is postponed for **5 days** (until <t:{int(postpone_until.timestamp())}:F>).\n'
                    f'If no response is received, the ticket will be closed automatically.\n'
                    f'You will be asked to update Jira **{issue_key}** via a button when that happens.'
                ),
                color=discord.Color.orange(),
                timestamp=now
            )
            await channel.send(embed=embed, view=DismissView())
            logger.info(f'[Discord] Restored Jira waiting-reply postpone for ticket #{ticket.get("ticket_number")} — {issue_key}')
        except Exception as e:
            logger.error(f'[Discord] Failed to restore Jira waiting-reply postpone for channel {channel.id}: {e}')

    async def _jira_auto_close(self, channel: discord.TextChannel, ticket: dict) -> None:
        try:
            issue_key = ticket.get('jira_ticket_key', '')
            ticket_number = ticket.get('ticket_number', '?')
            logger.info(f'[Discord] Auto-closing ticket #{ticket_number} — no user reply in 5 days (Jira: {issue_key})')

            # Send custom DM to user before closing
            try:
                user = await self.bot.fetch_user(ticket['user_id'])
                close_embed = discord.Embed(
                    description=(
                        f'Hello!\n\n'
                        f'Unfortunately, we have not received a response from you regarding your issue. '
                        f'As a result, we are unable to proceed with the investigation at this time.\n\n'
                        f'If you have any new information in the future, please open a new ticket and '
                        f'reference your previous case number: **{issue_key}**.'
                    ),
                    color=discord.Color.orange(),
                    timestamp=datetime.utcnow()
                )
                await user.send(embed=close_embed)
            except discord.Forbidden:
                logger.info(f'[Discord] Cannot DM user {ticket["user_id"]} — DMs disabled')
            except Exception as e:
                logger.warning(f'[Discord] Failed to send auto-close DM to user {ticket["user_id"]}: {e}')

            # Post Jira action button for moderators before closing
            if issue_key:
                action_embed = discord.Embed(
                    title='Ticket auto-closed — action required',
                    description=(
                        f'Ticket **#{ticket_number}** was closed due to no user response (5 days).\n\n'
                        f'Please update Jira **{issue_key}** manually using the button below.'
                    ),
                    color=discord.Color.red(),
                    timestamp=datetime.utcnow(),
                )
                await channel.send(
                    content=f'<@&{self.modmail_mod_role_id}>',
                    embed=action_embed,
                    view=JiraAutoCloseView(issue_key),
                )

            # Close the ticket without the default DM (we sent our own above)
            await self.close_ticket(channel, closer=None, notify_user=False)
        except Exception as e:
            logger.error(f'[Discord] Failed to auto-close Jira ticket for channel {channel.id}: {e}')

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
            Message handling for ModMail
        """

        #Ignore the bots
        if message.author.bot:
            return

        # ===== DM from user =====
        if isinstance(message.channel, discord.DMChannel):
            user = message.author
            content = message.content.strip()

            # !status command
            if content.lower() == '!status':
                embed = await self.get_ticket_status_embed(user.id)
                try:
                    await user.send(embed=embed)
                except Exception as e:
                    logger.error(f"Failed to send statuses to {user.name}. Error: {e}")
                return

            # !new command
            if content.lower() == '!new':
                await self.show_ticket_menu(user)
                return

            # Default message
            open_tickets = await self.get_user_tickets(user.id)

            if not open_tickets:
                # No opened tickets - showing the create menu
                await self.show_ticket_menu(user)
            elif len(open_tickets) == 1:
                # One ticket - send it
                await self.forward_to_channel(user, message, open_tickets[0])
            else:
                # If more than 1 ticket - showing the choosing buttons
                embed = discord.Embed(
                    title = "Select a Ticket",
                    description = "You have multiple open tickets. Please select which one to send this message to:",
                    color = discord.Color.blue()
                )

                view = TicketSelectView(open_tickets)
                msg = await user.send(embed=embed, view=view)

                # Wait for choosing
                await view.wait()

                if view.selected_ticket:
                    await self.forward_to_channel(user, message, view.selected_ticket)
                    await msg.edit(content = "Message sent!", embed = None, view = None)
                else:
                    await msg.edit(content = "Selection timed out.", embed = None, view = None)

            return

        # ===== Message in the ticket channel (main modmail OR waiting/postponed category) =====
        active_category_ids = {self.modmail_category_id}
        if self.waiting_category_id:
            active_category_ids.add(self.waiting_category_id)
        if self.jira_category_id:
            active_category_ids.add(self.jira_category_id)

        if message.guild and message.channel.category_id in active_category_ids:
            # Looking for the ticket by channel_id
            ticket = await self.tickets_collection.find_one({
                'channel_id': message.channel.id,
                'status': {'$in': ['open', 'in_progress']}
            })

            if not ticket:
                return

            # If message arrives in a postponed category (waiting or jira) - cancel postpone
            postponed_category_ids = set()
            if self.waiting_category_id:
                postponed_category_ids.add(self.waiting_category_id)
            if self.jira_category_id:
                postponed_category_ids.add(self.jira_category_id)

            if message.channel.category_id in postponed_category_ids:
                if ticket.get('postponed_until'):
                    await self._cancel_postpone(
                        message.channel, ticket,
                        triggered_by=f"message from {message.author.mention}"
                    )
                    # Re-fetch ticket after update
                    ticket = await self.tickets_collection.find_one({'_id': ticket['_id']})

            content = message.content.strip()

            # Jira ticket key detection - only in main modmail category, before Jira mode.
            # Multiple keys in one message: the first enters Jira mode, the rest are linked
            # as additional tickets (dismissible notifications, no state-machine change).
            if (message.channel.category_id == self.modmail_category_id
                    and not ticket.get('jira_ticket_key')
                    and not ticket.get('waiting_for_jira')):
                keys = []
                for k in _JIRA_KEY_RE.findall(content):
                    if k not in keys:
                        keys.append(k)
                if keys:
                    await self._enter_jira_mode(message.channel, keys[0], ticket)
                    if len(keys) > 1:
                        ticket = await self.tickets_collection.find_one({'_id': ticket['_id']})
                        for extra in keys[1:]:
                            await self._link_additional_jira(message.channel, extra, ticket)
                    return

            # !jira command - explicitly link one or more Jira tickets at any time
            if content.lower().startswith('!jira'):
                found = []
                for k in _JIRA_KEY_RE.findall(content):
                    if k not in found:
                        found.append(k)
                if not found:
                    await message.channel.send("Usage: `!jira SO-12345 [SO-...]`")
                    return
                for k in found:
                    # A URG ticket is never the primary link (same rule as the console Link Jira Ticket).
                    if not _jira_keys_of(ticket) and not k.upper().startswith('URG-'):
                        await self._enter_jira_mode(message.channel, k, ticket)
                    else:
                        await self._link_additional_jira(message.channel, k, ticket)
                    ticket = await self.tickets_collection.find_one({'_id': ticket['_id']})
                return

            # !maintenance command - subscribe this ticket to a game/server maintenance-end notice.
            #   !maintenance          -> interactive: pick type -> pick maintenance -> pick language
            #   !maintenance <name>   -> matches an active game OR server maintenance, then language
            if content.lower().startswith('!maintenance'):
                parts = content.split(maxsplit=1)
                arg = parts[1].strip() if len(parts) > 1 else ''
                from services.mongo_db_service import get_active_maintenances
                from services.maintenance_notify_service import normalize

                if not arg:
                    await message.channel.send(
                        content='Which maintenance do you want to subscribe this ticket to?',
                        view=MaintenanceTypeView(message.channel.id),
                    )
                    return

                arg_norm = normalize(arg)
                matched = matched_type = None
                for mtype in ('game', 'server', 'brazil'):     # the same three types as the console
                    active = await asyncio.to_thread(get_active_maintenances, mtype)
                    hit = next((e for e in active if normalize(e) == arg_norm), None)
                    if hit:
                        matched, matched_type = hit, mtype
                        break

                if not matched:
                    games = await asyncio.to_thread(get_active_maintenances, 'game')
                    servers = await asyncio.to_thread(get_active_maintenances, 'server')
                    brazil = await asyncio.to_thread(get_active_maintenances, 'brazil')
                    g = '\n'.join(f'• {e}' for e in games) if games else '(none)'
                    s = '\n'.join(f'• {e}' for e in servers) if servers else '(none)'
                    b = '\n'.join(f'• {e}' for e in brazil) if brazil else '(none)'
                    await message.channel.send(
                        f'No active maintenance matches `{arg}`.\n'
                        f'Active game maintenances:\n{g}\n'
                        f'Active server maintenances:\n{s}\n'
                        f'Active Brazil maintenances:\n{b}'
                    )
                    return

                await message.channel.send(
                    content=f'Select the language for the maintenance-end message for **{matched}**:',
                    view=MaintenanceLangView(matched, message.channel.id, matched_type),
                )
                return

            # !close command
            if content.lower() == '!close':
                await self.close_ticket(message.channel, message.author)
                return

            # !sclose command - schedule silent close in 1h (cancelled by new messages)
            if content.lower() == '!sclose':
                await self.schedule_close_ticket(message.channel, message.author)
                return

            # !postpone command
            if content.lower().startswith('!postpone'):
                parts = content.split(maxsplit=2)
                if len(parts) < 2:
                    await message.channel.send(
                        "Usage: `!postpone <time> [reason]`\nExample: `!postpone 2h Waiting for user response`")
                    return

                time_str = parts[1]
                reason = parts[2] if len(parts) > 2 else None

                await self.postpone_ticket(message.channel, message.author, time_str, reason)
                return

            # !status command (for moderators)
            if content.lower() == '!status':
                ticket_number = ticket.get('ticket_number', '?')

                embed = discord.Embed(
                    title=f"Ticket #{ticket_number} Information",
                    color=discord.Color.blue(),
                    timestamp=datetime.utcnow()
                )

                embed.add_field(name="Type", value=ticket['ticket_type'].upper())
                embed.add_field(name="Status", value=ticket['status'].replace('_', ' ').title())
                embed.add_field(name="Messages", value=str(ticket.get('messages_count', 0)))

                created = f"<t:{int(ticket['created_at'].timestamp())}:F>"
                embed.add_field(name="Created", value=created, inline=False)

                if ticket.get('first_response_at'):
                    responded = f"<t:{int(ticket['first_response_at'].timestamp())}:F>"
                    embed.add_field(name="First Response", value=responded, inline=False)

                if ticket.get('postponed_until'):
                    postponed = f"<t:{int(ticket['postponed_until'].timestamp())}:F>"
                    embed.add_field(name="Postponed Until", value=postponed, inline=False)
                    embed.add_field(name="Postpone Reason", value=ticket.get('postpone_reason', 'N/A'))

                if ticket.get('scheduled_close_until'):
                    sclose_ts = int(ticket['scheduled_close_until'].timestamp())
                    embed.add_field(
                        name="Scheduled Close",
                        value=f"<t:{sclose_ts}:F> (<t:{sclose_ts}:R>)",
                        inline=False
                    )

                # Metadata
                if 'metadata' in ticket:
                    meta = ticket['metadata']
                    if meta.get('email_or_session'):
                        embed.add_field(name="Email/Session", value=meta['email_or_session'], inline=False)
                    if meta.get('description'):
                        embed.add_field(name="Description", value=meta['description'][:1024], inline=False)

                await message.channel.send(embed=embed)
                return

            # !say - Send a message to the user
            if content.lower().startswith('!say'):
                message_text = content[4:].strip()
                if not message_text:
                    await message.channel.send("Usage: `!say <your text>`")
                    return
                original_content = message.content
                message.content = message_text

                try:
                    await self.forward_to_user(message.author, message, ticket)
                finally:
                    message.content = original_content

                # Jira ticket-mode: after moderator relays info to user, auto-postpone 5 days
                if ticket.get('jira_mode_unpostponed'):
                    await self._jira_mod_replied(message.channel, ticket)
                return

            # Plain (non-command) moderator message = an internal note. Record it so the CMT
            # Console shows it too (Discord already shows it in the channel; bot messages were
            # filtered out at the top of on_message, so console posts aren't double-recorded).
            if content and not content.startswith('!'):
                await self.tickets_collection.update_one(
                    {'_id': ticket['_id']},
                    {'$push': {'messages_history': {
                        'message_id': str(message.id),
                        'author_name': message.author.display_name,
                        'content': content,
                        'timestamp': datetime.utcnow(),
                        'is_user': False,
                        'note': True,
                    }}, '$inc': {'messages_count': 1}},
                )
            return

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    async def _get_email_handler(self, interaction: discord.Interaction, target: Union[discord.Member, discord.User]):
        """
        Handler for the 'Get User ID' user context menu.
        Right-click any username/avatar → Apps → Get User ID.
        Shows the user's Boosteroid User ID (never an email). Only the invoking moderator sees it.
        """
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        mod_role = interaction.guild.get_role(self.modmail_mod_role_id)
        if not mod_role or mod_role not in interaction.user.roles:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        try:
            doc, ticket_count = await asyncio.gather(
                self.users_emails_collection.find_one(
                    # discord_user_id is stored as a number by the bot, as a string by some console paths
                    {'discord_user_id': {'$in': [target.id, str(target.id)]}},
                    {'cloud_user_id': 1, '_id': 0},
                    sort=[('cloud_user_id', -1)],
                ),
                self.tickets_collection.count_documents({'user_id': target.id}),
            )
            user_id = (doc or {}).get('cloud_user_id')

            header = f"**{target.display_name}** (`{target.name}` · `{target.id}`)"
            from services.redact import ADMIN_SESSIONS_URL
            email_line = (f"User ID: [`{user_id}`](<{ADMIN_SESSIONS_URL}{user_id}>)" if user_id else
                          "No User ID on file.")
            tickets_line = f"Tickets: {ticket_count}" if ticket_count else "Tickets: none"
            text = f"{header}\n{email_line}\n{tickets_line}"

            await interaction.response.send_message(text, ephemeral=True)
            logger.info(f"[Discord] Get User ID used by {interaction.user.name} for {target.name} ({target.id})")
        except Exception as e:
            logger.error(f"[Discord] Get User ID failed for {target.name} ({target.id}): {e}")
            await interaction.response.send_message(
                "An error occurred while fetching the User ID.", ephemeral=True
            )


    async def _get_tickets_handler(self, interaction: discord.Interaction, target: Union[discord.Member, discord.User]):
        """
        Handler for the 'Get Tickets' user/message context menu.
        Right-click any username/avatar → Apps → Get Tickets.
        Shows the target user's ticket history (ephemeral, mod-only).
        """
        if not interaction.guild:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return

        mod_role = interaction.guild.get_role(self.modmail_mod_role_id)
        if not mod_role or mod_role not in interaction.user.roles:
            await interaction.response.send_message(
                "You don't have permission to use this command.", ephemeral=True
            )
            return

        try:
            tickets = await self.tickets_collection.find(
                {'user_id': target.id},
                {
                    'ticket_number': 1, 'status': 1, 'ticket_type': 1,
                    'created_at': 1, 'messages_count': 1, 'conversation_html': 1, '_id': 0,
                }
            ).sort('created_at', -1).limit(25).to_list(length=25)

            if not tickets:
                await interaction.response.send_message(
                    f"**{target.display_name}** (`{target.name}` · `{target.id}`)\n"
                    "No tickets found.",
                    ephemeral=True,
                )
                return

            STATUS_LABEL = {"open": "[open]", "in_progress": "[in progress]", "closed": "[closed]"}
            lines = [
                f"**{target.display_name}** (`{target.name}` · `{target.id}`) — {len(tickets)} ticket(s):",
                "",
            ]
            for t in tickets:
                status = t.get("status", "unknown")
                label = STATUS_LABEL.get(status, status)
                num = t.get("ticket_number", "?")
                ttype = t.get("ticket_type", "")
                msgs = t.get("messages_count", 0)
                created = t.get("created_at")
                date_str = created.strftime("%Y-%m-%d") if created else "—"
                type_str = f" · {ttype}" if ttype else ""
                html_marker = " [PDF]" if (status == "closed" and t.get("conversation_html")) else ""
                lines.append(f"**#{num}** · {label}{type_str} · {date_str} · {msgs} msg(s){html_marker}")

            text = "\n".join(lines)
            if len(text) > 1900:
                text = text[:1900] + "\n…*(truncated)*"

            await interaction.response.send_message(text, ephemeral=True)

            closed_with_html = [
                t for t in tickets if t.get('status') == 'closed' and t.get('conversation_html')
            ]
            if closed_with_html:
                view = TicketHistoryView(self, target.id, closed_with_html)
                await interaction.followup.send(
                    content='**Ticket history** — click to get PDF:',
                    view=view,
                    ephemeral=True,
                )

            logger.info(
                f"[Discord] Get Tickets used by {interaction.user.name} "
                f"for {target.name} ({target.id}) — {len(tickets)} ticket(s)"
            )
        except Exception as e:
            logger.error(f"[Discord] Get Tickets failed for {target.name} ({target.id}): {e}")
            await interaction.response.send_message(
                "An error occurred while fetching tickets.", ephemeral=True
            )




async def setup(bot, mongodb):
    """
        Setup function for cog's loading
    """
    cog = ModMail(bot, mongodb)
    await bot.add_cog(cog)

    # Context menus cannot be defined inside a Cog class.
    # Registered as guild-scoped so tree.sync(guild=...) picks them up instantly.
    guild_id = int(os.getenv('DISCORD_GUILD_ID', 0))
    guild_obj = discord.Object(id=guild_id) if guild_id else None

    # User context menu - right-click on username/avatar → Apps → Get User ID
    async def get_email_from_user(interaction: discord.Interaction, member: discord.Member):
        await cog._get_email_handler(interaction, member)

    # Message context menu - right-click on message text → Apps → Get User ID
    async def get_email_from_message(interaction: discord.Interaction, message: discord.Message):
        await cog._get_email_handler(interaction, message.author)

    # discord.py infers User vs Message type from the callback's second parameter annotation
    for callback in (get_email_from_user, get_email_from_message):
        cmd = app_commands.ContextMenu(name="Get User ID", callback=callback)
        if guild_obj:
            bot.tree.add_command(cmd, guild=guild_obj)
        else:
            bot.tree.add_command(cmd)

    # User context menu - right-click on username/avatar → Apps → Get Tickets
    async def get_tickets_from_user(interaction: discord.Interaction, member: discord.Member):
        await cog._get_tickets_handler(interaction, member)

    # Message context menu - right-click on message → Apps → Get Tickets
    async def get_tickets_from_message(interaction: discord.Interaction, message: discord.Message):
        await cog._get_tickets_handler(interaction, message.author)

    for callback in (get_tickets_from_user, get_tickets_from_message):
        cmd = app_commands.ContextMenu(name="Get Tickets", callback=callback)
        if guild_obj:
            bot.tree.add_command(cmd, guild=guild_obj)
        else:
            bot.tree.add_command(cmd)





