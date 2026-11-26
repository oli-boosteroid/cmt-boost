def _opt(value: str) -> dict:
    """Shorthand for a plain_text select option where label == value."""
    return {"text": {"type": "plain_text", "text": value}, "value": value}
