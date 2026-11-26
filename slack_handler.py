import logging
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


# Substrings that mark transient/self-healing network errors. Records whose message
# matches any of these are dropped from the Slack error channel only (the file log
# keeps everything, so `/logs` still shows them). Extend this list as new noise appears.
NOISE_PATTERNS = (
    "error with request TimeoutError",
    "Retrying in 60 sec",
    "Response payload is not completed",
    "ContentLengthError",
    "Exception happened while polling for updates",
    "NetworkError",
    "Bad Gateway",
)


class TransientNoiseFilter(logging.Filter):
    """Drops log records whose message contains a known transient-network noise pattern.

    Attach only to the Slack handler so the noisy, auto-retried network errors stop
    flooding the Slack error channel while remaining fully visible in data/app.log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not any(pattern in message for pattern in NOISE_PATTERNS)


class SlackHandler(logging.Handler):
    def __init__(self, token, channel_id):
        super().__init__()
        self.client = WebClient(token=token)
        self.channel_id = channel_id
    
    # A Slack section block holds at most 3000 characters; a long traceback went over it and Slack
    # rejected the whole message ("invalid_blocks"), so the error never reached the channel.
    _MAX_BLOCK = 2900

    @classmethod
    def _fit(cls, text: str) -> str:
        room = cls._MAX_BLOCK - 40          # the label line and the ``` fences
        if len(text) <= room:
            return text
        head, tail = room // 3, room - room // 3 - 30
        return text[:head] + "\n... (%d characters cut) ...\n" % (len(text) - head - tail) + text[-tail:]

    def emit(self, record):
        try:
            log_entry = self._fit(self.format(record))
            
            if record.levelno >= logging.ERROR:
                label = "[ERROR]"
            elif record.levelno >= logging.WARNING:
                label = "[WARNING]"
            else:
                return

            self.client.chat_postMessage(
                channel=self.channel_id,
                text=f"{label} {record.levelname}: {record.getMessage()}"[:3000],
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{label} {record.levelname}*\n```{log_entry}```"
                        }
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"`{record.filename}:{record.lineno}` | {record.asctime}"
                            }
                        ]
                    }
                ]
            )
        except SlackApiError as e:
            print(f"Failed to send log to Slack: {e.response['error']}")
        except Exception as e:
            print(f"Unexpected error in SlackHandler: {e}")
