from telegram.ext import Application
from dataclasses import dataclass

from settings import Settings


@dataclass
class BotContext:
    chat_id: str
    thread_id: int | None
    status_chat_id: str
    status_thread_id: int | None
    log_chat_id: str | None
    log_thread_id: int | None


def parse_chat_id(chat_id_str: str | None) -> tuple[str | None, int | None]:
    """Parse ``chat_id`` or ``chat_id/message_thread_id`` configuration.

    Telegram expects ``message_thread_id`` to be an integer. Keep the chat ID
    as a string for compatibility with numeric IDs and @usernames.
    """
    if chat_id_str is None:
        return None, None

    value = str(chat_id_str).strip()
    parts = value.split('/')

    if len(parts) > 2 or not parts[0]:
        raise ValueError(
            f"Invalid Telegram chat target {chat_id_str!r}; expected "
            "'chat_id' or 'chat_id/message_thread_id'"
        )

    if len(parts) == 1:
        return parts[0], None

    if not parts[1].isdigit():
        raise ValueError(f"Invalid Telegram message thread ID: {parts[1]!r}")

    return parts[0], int(parts[1])


def create_application(settings: Settings) -> Application:
    # Concurrent updates so a slow command (e.g. /camera waiting for a frame) doesn't block others
    return Application.builder().token(settings.telegram_bot_token).concurrent_updates(True).build()


def get_bot_context(settings: Settings) -> BotContext:
    chat_id, thread_id = parse_chat_id(settings.chat_id)
    status_chat_id, status_thread_id = parse_chat_id(settings.status_chat_id)
    log_chat_id, log_thread_id = parse_chat_id(settings.log_chat_id)

    return BotContext(
        chat_id=chat_id,
        thread_id=thread_id,
        status_chat_id=status_chat_id,
        status_thread_id=status_thread_id,
        log_chat_id=log_chat_id,
        log_thread_id=log_thread_id
    )
