import logging
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from datetime import datetime


class ModMailSlackHandler(logging.Handler):
    """Custom Slack handler for ModMail events"""

    def __init__(self, token, channel_id):
        super().__init__()
        self.client = WebClient(token=token)
        self.channel_id = channel_id

        # Keywords for different types
        self.event_types = {
            'created': {'label': '[CREATED]', 'color': '#36a64f'},
            'closed': {'label': '[CLOSED]', 'color': '#ff0000'},
            'postponed': {'label': '[POSTPONED]', 'color': '#FFA500'},
            'forwarded': {'label': '[FORWARDED]', 'color': '#4A90E2'},
            'reminder': {'label': '[REMINDER]', 'color': '#FFA500'},
            'status': {'label': '[STATUS]', 'color': '#808080'},
            'initialized': {'label': '[INITIALIZED]', 'color': '#36a64f'},
            'error': {'label': '[ERROR]', 'color': '#ff0000'},
        }

    def emit(self, record):
        """Send log to Slack"""
        try:
            # Pass only ModMail and INFO logs
            if 'modmail' not in record.name.lower():
                return

            if record.levelno < logging.INFO:
                return

            message = record.getMessage()

            # Choose the type of action by message
            event_info = self._detect_event_type(message)
            label = event_info['label']
            color = event_info['color']

            if record.levelno >= logging.ERROR:
                label = '[ERROR]'
                color = '#ff0000'
            elif record.levelno >= logging.WARNING:
                label = '[WARNING]'
                color = '#FFA500'

            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # Creating a message
            self.client.chat_postMessage(
                channel=self.channel_id,
                text=f"{label} {message}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{label} ModMail Event*\n{message}"
                        }
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"{timestamp} | `{record.funcName}`"
                            }
                        ]
                    }
                ],
                attachments=[
                    {
                        "color": color,
                        "fields": [
                            {
                                "title": "Level",
                                "value": record.levelname,
                                "short": True
                            },
                            {
                                "title": "Module",
                                "value": "ModMail",
                                "short": True
                            }
                        ]
                    }
                ]
            )

        except SlackApiError as e:
            print(f"Failed to send ModMail log to Slack: {e.response['error']}")
        except Exception as e:
            print(f"Unexpected error in ModMailSlackHandler: {e}")

    def _detect_event_type(self, message):
        """Choose the type of message by content"""
        message_lower = message.lower()

        if 'created' in message_lower and 'ticket' in message_lower:
            return self.event_types['created']
        elif 'closed' in message_lower:
            return self.event_types['closed']
        elif 'postponed' in message_lower or 'postpone' in message_lower:
            return self.event_types['postponed']
        elif 'forwarded' in message_lower:
            return self.event_types['forwarded']
        elif 'reminder' in message_lower:
            return self.event_types['reminder']
        elif 'status' in message_lower:
            return self.event_types['status']
        elif 'initialized' in message_lower:
            return self.event_types['initialized']
        elif 'error' in message_lower or 'failed' in message_lower:
            return self.event_types['error']
        else:
            return {'label': '[INFO]', 'color': '#4A90E2'}
