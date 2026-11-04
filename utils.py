"""Small shared helpers for BBbot."""


def get_thread_id(message):
    """Normalize the forum topic id. The General topic is thread 1."""
    if message is None:
        return 1
    return message.message_thread_id or 1


def humanize_duration(seconds: float) -> str:
    """3600 -> '1h', 90000 -> '1d 1h'."""
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)
