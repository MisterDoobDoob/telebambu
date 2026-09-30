"""Message texts and inline keyboards shared by handlers, notifications and the monitor."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from data import Role
from data.storage import PrintSession, Storage

HELP_TEXT = (
    "Available commands:\n"
    "/help - Show this help message\n"
    "/info [printer] - Show info about your print\n"
    "/notify [printer] <layer> - Send a one-time notification at a layer\n"
    "/notify [printer] <percent>% - Send a one-time notification at a percentage\n"
    "/notify_every [printer] layers|time|percent <number> - Send recurring camera snapshots\n"
    "/notify_every [printer] off - Disable recurring camera snapshots\n"
    "/camera [printer] - View camera image from your printer\n"
    "/light [printer] - Toggle your printer's light\n"
    "/unclaim [printer] - Unclaim your print\n\n"
    "Note: [printer] is required when you have multiple prints claimed."
)

NO_CLAIM = "You don't have an active print claimed."
SESSION_ENDED = "This print session has ended."
CLAIM_DM_PROMPT = "Where would you like to receive the finished print image?"


def _print_details(session: PrintSession) -> str:
    details = []
    if session.print_time:
        details.append(f"time: {session.print_time}")
    if session.total_layers:
        details.append(f"layers: {session.total_layers}")
    return f" ({', '.join(details)})" if details else ""


def started_text(printer_num: int, session: PrintSession) -> str:
    """Main chat text for an unclaimed print."""
    return f"Printer {printer_num} has started printing.{_print_details(session)}"


def claimed_text(printer_num: int, session: PrintSession) -> str:
    """Main chat text for a claimed print."""
    return f"Printer {printer_num} started by {session.claimed_username}{_print_details(session)}"


def claim_keyboard(printer_num: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Claim Print", callback_data=f"claim:{printer_num}")]])


def unclaim_keyboard(printer_num: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim:{printer_num}")]])


def start_dm_keyboard(bot_username: str, printer_num: int) -> InlineKeyboardMarkup:
    url = f"https://t.me/{bot_username}?start=claim-{printer_num}"
    return InlineKeyboardMarkup([[InlineKeyboardButton("Start DM with bot", url=url)]])


def _unclaim_help_row(printer_num: int) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton("Unclaim Print", callback_data=f"unclaim:{printer_num}"),
        InlineKeyboardButton("Help", callback_data="help"),
    ]


def dm_preference_keyboard(printer_num: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Main Chat (Recommended)", callback_data=f"dmpref:{printer_num}:chat"),
            InlineKeyboardButton("Send to DM only", callback_data=f"dmpref:{printer_num}:dm"),
        ],
        _unclaim_help_row(printer_num),
    ])


def settings_message(printer_num: int, session: PrintSession) -> tuple[str, InlineKeyboardMarkup]:
    destination = "main chat" if session.dm_preference == "chat" else "here privately"
    layer2_status = "ON" if session.layer2_notify else "OFF"

    text = (
        f"Settings for Printer {printer_num}:\n"
        f"- Finished image: {destination}\n"
        f"- Layer 2 notification: {layer2_status}\n\n"
        f"You can use /camera {printer_num} to check on your print while it's active."
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Layer 2 Notify: {layer2_status}", callback_data=f"layer2:{printer_num}")],
        _unclaim_help_row(printer_num),
    ])
    return text, keyboard


def staff_list_text(storage: Storage) -> str:
    staff = storage.staff()
    lines = ["Staff:"]
    for role in (Role.OWNER, Role.ADMIN, Role.MOD):
        names = ", ".join(sorted(storage.display_name(uid) for uid in staff[role])) or "none"
        lines.append(f"- {role.label}: {names}")
    return "\n".join(lines)


def config_text(settings, keys) -> str:
    lines = ["Config (change with /config set <key> <value>):"]
    lines += [f"- {key}: {getattr(settings, key)}" for key in keys]
    return "\n".join(lines)


def printer_list_text(printers: dict, printer_manager) -> str:
    """Admin view of configured printers. Access codes are never shown."""
    if not printers:
        return "No printers configured. Add one with /printer add."
    lines = ["Printers:"]
    for num, config in sorted(printers.items()):
        online = "online" if printer_manager.get_online_printer(num) else "offline"
        lines.append(f"{num}: {config.ip} | MAC {config.mac or '-'} | SN {config.serial} | {online}")
    return "\n".join(lines)


def watchers_text(printer_num: int, session: PrintSession, storage: Storage) -> str:
    names = ", ".join(storage.display_name(uid) for uid in session.watchers)
    if not names:
        return (f"Only {session.claimed_username} is notified about Printer {printer_num}. "
                f"Add others with /notify_user {printer_num} <user>.")
    return f"Printer {printer_num} notifies {session.claimed_username} and: {names}"


def session_extras(session: PrintSession) -> list[str]:
    """Short notes about a print's pending notifications and actions."""
    extras = []
    if session.notify_layer and not session.notify_layer_notified:
        if session.notify_type == "percent":
            extras.append(f"notify at {session.notify_original_value}%")
        else:
            extras.append(f"notify at L{session.notify_layer}")
    if session.notify_every_type:
        unit = {"layers": " layers", "time": " min", "percent": "%"}[session.notify_every_type]
        extras.append(f"snapshot every {session.notify_every_value}{unit}")
    if session.stop_at_layer:
        extras.append(f"stop at L{session.stop_at_layer}")
    if session.watchers:
        extras.append(f"+{len(session.watchers)} notified")
    return extras


def code_block(title: str, lines: list[str]) -> str:
    """A MarkdownV2 title and code block, like the status message. Title must not need escaping."""
    body = "\n".join(lines).replace("\\", "\\\\").replace("`", "\\`")
    return f"{title}```c\n{body}\n```"
