import re

from telegram import Update, CallbackQuery, InputFile
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from data import Storage
from printers import PrinterManager
from . import ui
from .messages import MessageService
import config as cfg

NOTIFY_USAGE = "Usage: /notify [printer] <layer> or /notify [printer] <percent>%"
NOTIFY_EVERY_USAGE = "Usage: /notify_every [printer] layers <number>, time <minutes>, percent <number>, or off"
NOTIFY_EVERY_UNITS = {"layers": "layers", "time": "minutes", "percent": "%"}


# Buttons sent before printers were 1-based, e.g. ``claim_0`` or ``dm_pref_0_chat``
_LEGACY_CALLBACK = re.compile(r"^[a-z_]+?_(\d+)(?:_(chat|dm))?$")


def _parse_callback(query: CallbackQuery) -> tuple[int, str | None]:
    """Return ``(printer_num, option)`` from callback data such as ``claim:1`` or ``dmpref:1:chat``.

    Buttons sent before printers were 1-based used ``claim_0`` / ``dm_pref_0_chat``
    with a 0-based index; those are renumbered so old messages keep working.
    """
    legacy = _LEGACY_CALLBACK.match(query.data)
    if legacy:
        return int(legacy[1]) + 1, legacy[2]
    parts = query.data.split(":")
    return int(parts[1]), parts[2] if len(parts) > 2 else None


def _callback_pattern(action: str, legacy_action: str, option: str = "") -> str:
    legacy_option = option.replace(":", "_")
    return rf"^({action}:\d+{option}|{legacy_action}_\d+{legacy_option})$"


class BotHandlers:
    def __init__(self, storage: Storage, message_service: MessageService, printer_manager: PrinterManager):
        self.storage = storage
        self.ms = message_service
        self.pm = printer_manager

    def register(self, app: Application):
        commands = {
            "start": self.cmd_start,
            "help": self.cmd_help,
            "info": self.cmd_info,
            "notify": self.cmd_notify,
            "notify_every": self.cmd_notify_every,
            "camera": self.cmd_camera,
            "light": self.cmd_light,
            "unclaim": self.cmd_unclaim,
            "restart": self.cmd_restart,
        }
        for name, callback in commands.items():
            app.add_handler(CommandHandler(name, callback))

        callbacks = {
            _callback_pattern("claim", "claim"): self.cb_claim,
            _callback_pattern("dmpref", "dm_pref", ":(chat|dm)"): self.cb_dm_preference,
            _callback_pattern("layer2", "layer2_toggle"): self.cb_layer2_toggle,
            _callback_pattern("unclaim", "unclaim"): self.cb_unclaim,
            r"^help$": self.cb_help,
        }
        for pattern, callback in callbacks.items():
            app.add_handler(CallbackQueryHandler(callback, pattern=pattern))

    # --- Shared logic ---

    async def _resolve_printer(self, update: Update, args: list[str], *, takes_value: bool = False,
                               owner_any: bool = False, usage: str = "") -> tuple[int, list[str]] | None:
        """Work out which printer a command targets, replying with an error if it can't.

        A leading number is read as the printer when the command has no value
        argument, or when a value follows it (``/notify 2 50`` vs ``/notify 50``).
        Without one, the user's single claimed printer is used. With
        ``owner_any``, the bot owner may target any printer.

        Returns ``(printer_num, remaining_args)``, or None if a reply was sent.
        """
        reply = update.effective_message.reply_text
        user_id = update.effective_user.id
        is_owner = owner_any and user_id == cfg.OWNER_ID
        claimed = self.storage.claimed_printers(user_id)

        if not claimed and not is_owner:
            await reply(ui.NO_CLAIM)
            return None

        if args and args[0].isdigit() and (not takes_value or len(args) >= 2):
            printer_num = int(args[0])
            if printer_num not in self.pm:
                await reply(f"Invalid printer number. Use {self.pm.numbers_text()}")
                return None
            if not is_owner and printer_num not in claimed:
                claimed_list = ", ".join(str(num) for num in claimed)
                await reply(f"You haven't claimed Printer {printer_num}. Your printer(s): {claimed_list}")
                return None
            return printer_num, args[1:]

        if args and not takes_value:
            await reply("Please provide a valid printer number.")
            return None

        if len(claimed) == 1:
            return claimed[0], args

        if claimed:
            printer_list = ", ".join(str(num) for num in claimed)
            await reply(f"You have multiple prints claimed ({printer_list}). {usage}".strip())
        else:
            await reply(f"{usage}\nAvailable printers: {self.pm.numbers_text()}".strip())
        return None

    def _print_status(self, printer_num: int) -> str:
        snap = self.pm.snapshot(printer_num)
        if not snap:
            return ""
        return (
            f"\n\nCurrent status:\n- Progress: {snap.progress}%\n"
            f"- Time remaining: {snap.time_left}\n- Layer: {snap.layer}/{snap.total_layers}"
        )

    async def _send_claim_dm(self, context: ContextTypes.DEFAULT_TYPE, user_id: int, printer_num: int):
        """DM the claimer asking where they want the finished print image. Raises if the DM fails."""
        await context.bot.send_message(
            chat_id=user_id,
            text=f"You claimed Printer {printer_num}!{self._print_status(printer_num)}\n\n{ui.CLAIM_DM_PROMPT}",
            reply_markup=ui.dm_preference_keyboard(printer_num),
        )

    async def _unclaim(self, context: ContextTypes.DEFAULT_TYPE, printer_num: int) -> str:
        """Unclaim a print, restore the Claim button in the main chat and return a reply text."""
        session = self.storage.unclaim_print(printer_num)
        try:
            await context.bot.edit_message_text(
                chat_id=session.chat_id,
                message_id=session.message_id,
                text=ui.started_text(printer_num, session),
                reply_markup=ui.claim_keyboard(printer_num),
            )
            return f"You have unclaimed Printer {printer_num}."
        except Exception:
            return f"Unclaimed Printer {printer_num}, but could not update the main chat message."

    async def _restart(self, printer_num: int) -> str:
        try:
            self.pm.restart(printer_num)
            return f"Printer {printer_num} reconnection initiated."
        except Exception as e:
            return f"Failed to restart Printer {printer_num}: {e}"

    # --- Commands ---

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handles deep links from the "Start DM with bot" button."""
        user = update.effective_user
        reply = update.effective_message.reply_text
        arg = context.args[0] if context.args else ""

        match = re.fullmatch(r"claim([-_])(\d+)", arg)
        if not match:
            await reply("Welcome! Use the buttons in the main chat to claim prints.")
            return

        # "claim_N" links predate 1-based printer numbers
        printer_num = int(match[2]) + (1 if match[1] == "_" else 0)
        session = self.storage.get_print(printer_num)
        if not session:
            await reply(ui.SESSION_ENDED)
            return
        if session.claimed_by != user.id:
            await reply("You are not the claimer of this print.")
            return

        await self._send_claim_dm(context, user.id, printer_num)

        # Update the main chat message to remove the "Start DM" button
        try:
            await context.bot.edit_message_text(
                chat_id=session.chat_id,
                message_id=session.message_id,
                text=ui.claimed_text(printer_num, session),
            )
        except Exception as e:
            print(f'Exception: could not edit main chat message {session.message_id} on /start deep link: {e}')

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.effective_message.reply_text(ui.HELP_TEXT)

    async def cmd_info(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        resolved = await self._resolve_printer(update, context.args, usage="Usage: /info <printer>")
        if not resolved:
            return
        printer_num, _ = resolved
        reply = update.effective_message.reply_text

        snap = self.pm.snapshot(printer_num)
        if not snap:
            await reply(f"Printer {printer_num} is not connected.")
            return

        info_text = (
            f"Printer {printer_num} Info:\n"
            f"- Status: {snap.state}\n"
            f"- Progress: {snap.progress}%\n"
            f"- Time remaining: {snap.time_left}\n"
            f"- Layer: {snap.layer}/{snap.total_layers}\n"
        )

        session = self.storage.get_print(printer_num)
        if session and session.notify_layer and not session.notify_layer_notified:
            if session.notify_type == "percent":
                info_text += f"- Notification: {session.notify_original_value}% (layer {session.notify_layer})\n"
            else:
                info_text += f"- Notification: layer {session.notify_layer}\n"
        if session and session.notify_every_type:
            info_text += f"- Recurring snapshots: every {session.notify_every_value} {NOTIFY_EVERY_UNITS[session.notify_every_type]}\n"

        await reply(info_text)

    async def cmd_notify(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Set a layer or percentage to be notified at."""
        reply = update.effective_message.reply_text
        if self.storage.claimed_printers(update.effective_user.id) and not context.args:
            await reply(f"{NOTIFY_USAGE}\nExamples: /notify 50 or /notify 2 75%")
            return

        resolved = await self._resolve_printer(update, context.args, takes_value=True,
                                               usage="Usage: /notify <printer> <layer|percent%>")
        if not resolved:
            return
        printer_num, args = resolved
        if not args:
            await reply(NOTIFY_USAGE)
            return

        arg = args[0]
        if arg.endswith('%'):
            try:
                percent = int(arg[:-1])
            except ValueError:
                await reply("Please provide a valid percentage.")
                return
            if not 1 <= percent <= 100:
                await reply("Percentage must be between 1 and 100.")
                return

            # Convert percent to a target layer using the print's total layers
            snap = self.pm.snapshot(printer_num)
            if not snap:
                await reply(f"Printer {printer_num} is not connected.")
                return
            if snap.total_layers <= 0:
                await reply("Cannot determine total layers for this print.")
                return

            target_layer = max(1, (percent * snap.total_layers) // 100)
            self.storage.set_notify_layer(printer_num, target_layer, notify_type="percent", original_value=percent)
            await reply(f"You will be notified when {percent}% is reached "
                        f"(layer {target_layer}/{snap.total_layers}) on Printer {printer_num}.")
            return

        try:
            layer = int(arg)
        except ValueError:
            await reply("Please provide a valid layer number or percentage.")
            return
        if layer < 1:
            await reply("Layer must be a positive number.")
            return

        self.storage.set_notify_layer(printer_num, layer, notify_type="layer", original_value=layer)
        await reply(f"You will be notified when layer {layer} is reached on Printer {printer_num}.")

    async def cmd_notify_every(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send recurring camera snapshots during a print."""
        reply = update.effective_message.reply_text
        if self.storage.claimed_printers(update.effective_user.id) and not context.args:
            await reply(f"{NOTIFY_EVERY_USAGE}\nExamples: /notify_every layers 5, /notify_every time 30")
            return

        resolved = await self._resolve_printer(update, context.args, takes_value=True,
                                               usage="Usage: /notify_every <printer> <layers|time|percent> <number>")
        if not resolved:
            return
        printer_num, args = resolved

        if args and args[0].lower() == "off":
            self.storage.clear_notify_every(printer_num)
            await reply(f"Recurring camera notifications disabled for Printer {printer_num}.")
            return

        if len(args) != 2 or args[0].lower() not in NOTIFY_EVERY_UNITS:
            await reply(NOTIFY_EVERY_USAGE)
            return

        notify_type = args[0].lower()
        try:
            value = int(args[1])
        except ValueError:
            value = 0
        if value < 1:
            await reply("The interval must be a positive whole number.")
            return
        if notify_type == "percent" and value > 100:
            await reply("Percentage interval must be between 1 and 100.")
            return

        # Start counting from the current position so we don't immediately fire
        initial_value = 0
        snap = self.pm.snapshot(printer_num)
        if snap and notify_type == "layers":
            initial_value = snap.layer // value
        elif snap and notify_type == "percent":
            initial_value = snap.progress // value

        self.storage.set_notify_every(printer_num, notify_type, value, initial_value)
        await reply(f"Recurring camera notifications set every {value} {NOTIFY_EVERY_UNITS[notify_type]} "
                    f"on Printer {printer_num}.")

    async def cmd_camera(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Owner has full access, claimers can access their printers."""
        resolved = await self._resolve_printer(update, context.args, owner_any=True, usage="Usage: /camera <printer>")
        if not resolved:
            return
        printer_num, _ = resolved
        message = update.effective_message

        frame = await self.pm.get_camera_frame(printer_num)
        if not frame:
            await message.reply_text(f"Printer {printer_num} is not connected or has no camera frame.")
            return

        await message.reply_photo(
            photo=InputFile(frame, filename=f"printer_{printer_num}.jpg"),
            caption=f"Camera image from Printer {printer_num}",
        )

    async def cmd_light(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Toggle the printer light (owner or claimer)."""
        resolved = await self._resolve_printer(update, context.args, owner_any=True, usage="Usage: /light <printer>")
        if not resolved:
            return
        printer_num, _ = resolved
        reply = update.effective_message.reply_text

        if not self.pm.get_online_printer(printer_num):
            await reply(f"Printer {printer_num} is not connected.")
            return

        try:
            turn_on = not self.pm.is_light_on(printer_num)
            self.pm.set_light(printer_num, turn_on)
            await reply(f"Printer {printer_num} light turned {'ON' if turn_on else 'OFF'}.")
        except Exception as e:
            await reply(f"Failed to toggle light: {e}")

    async def cmd_unclaim(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Unclaim the print and revert the main chat message."""
        resolved = await self._resolve_printer(update, context.args, usage="Usage: /unclaim <printer>")
        if not resolved:
            return
        printer_num, _ = resolved
        await update.effective_message.reply_text(await self._unclaim(context, printer_num))

    async def cmd_restart(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Owner only: reboot a printer and reconnect."""
        reply = update.effective_message.reply_text
        if update.effective_user.id != cfg.OWNER_ID:
            await reply("Only the bot owner can use this command.")
            return

        if not context.args:
            await reply(f"Usage: /restart <printer>\nAvailable printers: {self.pm.numbers_text()}")
            return
        if not context.args[0].isdigit():
            await reply("Please provide a valid printer number.")
            return
        printer_num = int(context.args[0])
        if printer_num not in self.pm:
            await reply(f"Invalid printer number. Use {self.pm.numbers_text()}")
            return

        await reply(await self._restart(printer_num))

    # --- Callback buttons ---

    async def cb_claim(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        user = query.from_user
        printer_num, _ = _parse_callback(query)

        session = self.storage.get_print(printer_num)
        if not session:
            await query.answer()
            await query.edit_message_text(ui.SESSION_ENDED)
            return
        if session.claimed_by:
            await query.answer(f"Already claimed by {session.claimed_username}", show_alert=True)
            return
        await query.answer()

        username = f"@{user.username}" if user.username else user.full_name
        session = self.storage.claim_print(printer_num, user.id, username)
        claimed_text = ui.claimed_text(printer_num, session)

        try:
            await self._send_claim_dm(context, user.id, printer_num)
            await query.edit_message_text(claimed_text)
        except Exception:
            # User hasn't started a conversation with the bot yet
            await query.edit_message_text(
                f"{claimed_text}\n\n{username}, please start a conversation with the bot to configure your print settings:",
                reply_markup=ui.start_dm_keyboard(context.bot.username, printer_num),
            )

    async def _claimer_session(self, query: CallbackQuery, printer_num: int):
        """Return the session if the button presser is its claimer, otherwise answer the query and return None."""
        session = self.storage.get_print(printer_num)
        if not session:
            await query.answer()
            await query.edit_message_text(ui.SESSION_ENDED)
            return None
        if session.claimed_by != query.from_user.id:
            await query.answer("You are not the claimer of this print.", show_alert=True)
            return None
        await query.answer()
        return session

    async def cb_dm_preference(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        printer_num, preference = _parse_callback(query)
        if not await self._claimer_session(query, printer_num):
            return

        self.storage.set_dm_preference(printer_num, preference)
        text, keyboard = ui.settings_message(printer_num, self.storage.get_print(printer_num))
        await query.edit_message_text(text, reply_markup=keyboard)

    async def cb_layer2_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        printer_num, _ = _parse_callback(query)
        session = await self._claimer_session(query, printer_num)
        if not session:
            return

        self.storage.set_layer2_notify(printer_num, not session.layer2_notify)
        text, keyboard = ui.settings_message(printer_num, self.storage.get_print(printer_num))
        await query.edit_message_text(text, reply_markup=keyboard)

    async def cb_unclaim(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        printer_num, _ = _parse_callback(query)
        if not await self._claimer_session(query, printer_num):
            return
        reply_text = await self._unclaim(context, printer_num)
        # Unclaim buttons also appear on photo notifications, which can't be edited as text
        if query.message and query.message.text:
            await query.edit_message_text(reply_text)
        elif query.message:
            await query.message.reply_text(reply_text)

    async def cb_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        await query.message.reply_text(ui.HELP_TEXT)


def setup_handlers(app: Application, storage: Storage, message_service: MessageService, printer_manager: PrinterManager):
    BotHandlers(storage, message_service, printer_manager).register(app)
