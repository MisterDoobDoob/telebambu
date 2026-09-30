from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from bambulabs_api import GcodeState

from .manager import PrinterManager, PrinterEvent, EventType, format_duration, to_int

if TYPE_CHECKING:
    from bot.messages import MessageService
    from settings import Settings

PRINT_STARTED_DELAY = 2  # seconds to let the printer update its time estimate
# Don't flag a missing camera frame until this long after a printer connects/restarts,
# since the camera stream can take a while to establish (TLS handshake, auth, retries)
CAMERA_STARTUP_GRACE = 30  # seconds
# Only auto-restart printers in these states, so a running print is never interrupted
NOT_PRINTING_STATES = (GcodeState.IDLE, GcodeState.FINISH)


class PrinterMonitor:
    def __init__(self, printer_manager: PrinterManager, message_service: MessageService, settings: Settings):
        self.pm = printer_manager
        self.ms = message_service
        self.storage = message_service.storage
        # Settings are read on every tick, so /config changes apply without a restart
        self.settings = settings
        # Printers already reported as having a stale camera, to avoid spam
        self._stale_camera_reported: set[int] = set()
        # Time between tick starts, so event loop stalls show up on top of the sleep interval
        self._stats_since = time.monotonic()
        self._last_tick_at: float | None = None
        self._tick_gaps: list[float] = []

    async def run(self):
        while True:
            await asyncio.sleep(self.settings.update_interval)
            self._record_tick()
            try:
                await self.log_stats_if_due()
                await self.tick()
            except Exception as e:
                print(f'Monitor tick failed: {e}')

    def _record_tick(self):
        now = time.monotonic()
        if self._last_tick_at is not None:
            self._tick_gaps.append(now - self._last_tick_at)
        self._last_tick_at = now

    async def log_stats_if_due(self):
        """Log min/avg/max time between updates and every printer's status, then start a new window."""
        stats_interval = self.settings.stats_log_interval
        if not stats_interval or time.monotonic() - self._stats_since < stats_interval:
            return

        gaps = self._tick_gaps
        if gaps:
            timing = (f'{len(gaps)} updates, avg {sum(gaps) / len(gaps):.2f}s, '
                      f'min {min(gaps):.2f}s, max {max(gaps):.2f}s')
        else:
            timing = 'no updates'
        window = format_duration(round((time.monotonic() - self._stats_since) / 60))
        status = '\n'.join(self.pm.get_status_lines())
        await self.ms.log_message(f'Update stats (last {window}): {timing}\n{status}')

        self._tick_gaps = []
        self._stats_since = time.monotonic()

    async def tick(self):
        await self.pm.reconnect_if_needed(self.ms.log_message)

        try:
            await self.ms.update_status_message(self.pm.get_status_text())
        except Exception as e:
            print(f'Failed to update status message: {e}')

        # Printers are independent, so a slow camera capture on one doesn't hold up the rest
        await asyncio.gather(*(self._process_printer(num) for num in self.pm.numbers()))

        await self.ms.flush_logs()

    async def _process_printer(self, num: int):
        try:
            await self._check_stale_camera(num)
            await self._check_periodic_camera(num)
        except Exception as e:
            print(f'Error running checks for printer {num}: {e}')

        for event in self.pm.poll(num):
            try:
                await self._handle_event(event)
            except Exception as e:
                print(f'Error handling event {event.type} for printer {num}: {e}')

    async def _check_stale_camera(self, num: int):
        """Auto-restart an idle/finished printer whose camera has stopped sending frames (once per outage)."""
        printer = self.pm.get_online_printer(num)
        if not printer:
            return
        if self.pm.seconds_since_connect(num) < CAMERA_STARTUP_GRACE:
            return

        has_frame = self.pm.has_camera_frame(num)
        state = printer.get_state()
        if not has_frame and state in NOT_PRINTING_STATES:
            if num not in self._stale_camera_reported:
                self._stale_camera_reported.add(num)
                if not self.settings.auto_restart_printers:
                    await self.ms.log_message(f"Printer {num} has no camera ({state}). Auto-restart disabled, skipping.")
                    return
                await self.ms.log_message(f"Printer {num} has no camera ({state}). Auto-restarting...")
                try:
                    await self.pm.restart(num)
                except Exception as e:
                    await self.ms.log_message(f"Failed to auto-restart Printer {num}: {e}")
        elif has_frame:
            # Camera recovered, clear the flag
            self._stale_camera_reported.discard(num)

    async def _check_periodic_camera(self, num: int):
        """Send a recurring camera snapshot when the claimer's configured trigger is due."""
        session = self.storage.get_print(num)
        if not session or not session.claimed_by or not session.notify_every_type:
            return
        snap = self.pm.snapshot(num)
        if not snap or snap.state != GcodeState.RUNNING:
            return

        interval = session.notify_every_value or 0
        trigger_value = None
        if session.notify_every_type in ("layers", "percent"):
            current = snap.layer if session.notify_every_type == "layers" else snap.progress
            trigger_value = current // interval if interval else 0
            due = trigger_value > (session.notify_every_last_value or 0)
        else:  # "time"
            due = time.time() - (session.notify_every_last_sent_at or 0) >= interval * 60

        if due:
            frame = await self.pm.get_camera_frame(num)
            await self.ms.send_periodic_camera_notification(num, snap, trigger_value, frame)

    async def _handle_event(self, event: PrinterEvent):
        printer = event.printer
        num = event.printer_num

        if event.type == EventType.STATE_CHANGED:
            # Log state changes to stdout only (not to Telegram)
            if 'new' in event.data:
                await self.ms.log_message(
                    f'Printer {num} GCODE state: {event.data["prev"]} -> {event.data["new"]}', stdout_only=True)
            else:
                await self.ms.log_message(
                    f'Printer {num} PRINT state: {event.data["prev_print"]} -> {event.data["new_print"]}', stdout_only=True)

        elif event.type == EventType.PRINT_STARTED:
            await asyncio.sleep(PRINT_STARTED_DELAY)
            await self.ms.send_print_started(num, format_duration(printer.get_time()), to_int(printer.total_layer_num()))

        elif event.type == EventType.PRINT_FINISHED:
            await self.ms.send_print_finished(num, await self.pm.get_camera_frame(num))

        elif event.type == EventType.PRINT_FAILED:
            frame = await self.pm.get_camera_frame(num)
            await self.ms.send_print_failed(num, event.data.get('error_code'), frame)

        elif event.type == EventType.PRINT_PAUSED:
            frame = await self.pm.get_camera_frame(num)
            await self.ms.log_message(f'Printer {num} has paused printing. (code: {event.data.get("error_code")})', frame)

        elif event.type == EventType.LAYER_CHANGED:
            await self._handle_layer_change(num, event.data['prev_layer'], event.data['layer'])

    async def _handle_layer_change(self, num: int, prev_layer: int, layer: int):
        session = self.storage.get_print(num)
        if not session or not session.claimed_by:
            return

        # Use crossings rather than exact matches so a missed poll doesn't skip a notification
        layer2_due = session.layer2_notify and not session.layer2_notified and prev_layer < 2 <= layer
        custom_due = bool(session.notify_layer) and not session.notify_layer_notified and layer >= session.notify_layer
        if not (layer2_due or custom_due):
            return

        # One capture serves both notifications
        frame = await self.pm.get_camera_frame(num)
        if layer2_due:
            await self.ms.send_layer2_notification(num, frame)
        if custom_due:
            await self.ms.send_custom_layer_notification(num, frame)
