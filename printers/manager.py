import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto

import bambulabs_api as bl
from bambulabs_api import GcodeState, PrintStatus, Printer

from settings import PrinterConfig

# How long to wait for a fresh camera frame after turning the light on
CAMERA_POLL_INTERVAL = 2  # seconds
CAMERA_MAX_POLLS = 10
# Minimum gap between two pause notifications for the same printer
PAUSE_NOTIFY_COOLDOWN = 60  # seconds
# States a printer can be in right before a new print starts running
PRE_PRINT_STATES = (GcodeState.FINISH, GcodeState.IDLE, GcodeState.PREPARE)
# How long to wait for a removed printer to disconnect before giving up on it
DISCONNECT_TIMEOUT = 10  # seconds
# How long a reboot + reconnect may take before it is reported as failed
RESTART_TIMEOUT = 30  # seconds
# Bambu print speed profiles, as reported in spd_lvl and set with the print_speed command
SPEED_LEVELS = {1: 'silent', 2: 'standard', 3: 'sport', 4: 'ludicrous'}
# States where the printer is not in use
IDLE_STATES = (GcodeState.IDLE, GcodeState.FINISH, GcodeState.UNKNOWN)


class _CameraRetryNoiseFilter(logging.Filter):
    """Drop the camera thread's network errors, which bambulabs_api logs on every
    5-second retry while a printer is offline. PrinterManager logs outages itself,
    once per outage; other library errors (e.g. a wrong access code) still get through."""
    NOISE_PREFIXES = ('Error occurred: [Errno', 'Error in socket:')

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().startswith(self.NOISE_PREFIXES)


logging.getLogger('bambulabs_api').addFilter(_CameraRetryNoiseFilter())


def to_int(value, default: int = 0) -> int:
    """Coerce a printer value (which may be None or 'Unknown') to an int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_duration(total_mins) -> str:
    total_mins = to_int(total_mins)
    hrs, mins = divmod(total_mins, 60)
    return f'{hrs}h{mins}m' if hrs > 0 else f'{mins}m'


class EventType(Enum):
    PRINT_STARTED = auto()
    PRINT_FINISHED = auto()
    PRINT_FAILED = auto()
    PRINT_PAUSED = auto()
    LAYER_CHANGED = auto()
    STATE_CHANGED = auto()


@dataclass
class PrinterEvent:
    type: EventType
    printer_num: int
    printer: Printer
    data: dict = field(default_factory=dict)


@dataclass
class PrinterSnapshot:
    """Point-in-time view of a printer's progress."""
    state: GcodeState
    print_state: PrintStatus
    progress: int
    time_left: str
    layer: int
    total_layers: int


@dataclass
class PrinterDetails:
    """Extra information shown by /info for a single printer."""
    file_name: str
    nozzle_temp: float | None
    bed_temp: float | None
    chamber_temp: float | None
    speed_level: str
    speed_percent: int


@dataclass
class PrinterSlot:
    """A configured printer and the state tracked for it between polls."""
    ip: str
    access_code: str
    serial: str
    mac: str = ''
    printer: Printer | None = None
    prev_state: tuple[GcodeState, PrintStatus] = (GcodeState.UNKNOWN, PrintStatus.UNKNOWN)
    prev_layer: int = 0
    last_paused_time: float = 0.0
    connected_at: float = 0.0
    camera_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PrinterManager:
    """Printers are identified by their 1-based printer number everywhere."""

    def __init__(self, printer_configs: dict[int, PrinterConfig]):
        self.slots: dict[int, PrinterSlot] = {
            num: PrinterSlot(ip=c.ip, access_code=c.access_code, serial=c.serial, mac=c.mac)
            for num, c in printer_configs.items()
        }
        self._logged_disconnected: set[int] = set()

    def __len__(self) -> int:
        return len(self.slots)

    def __contains__(self, num: int) -> bool:
        return num in self.slots

    def numbers(self) -> list[int]:
        return sorted(self.slots)

    def numbers_text(self) -> str:
        """Human readable list of valid printer numbers, e.g. ``1-24``."""
        nums = self.numbers()
        if nums and nums == list(range(nums[0], nums[-1] + 1)):
            return f'{nums[0]}-{nums[-1]}' if len(nums) > 1 else str(nums[0])
        return ', '.join(map(str, nums))

    # --- Connection ---

    async def connect_all(self, log_fn):
        for num in self.numbers():
            await self._connect(num, log_fn)

    async def _connect(self, num: int, log_fn):
        slot = self.slots[num]
        await log_fn(f'Connecting to printer {num} at IP {slot.ip}')
        try:
            # Keep the Printer object even if the first connect fails, so
            # reconnect_if_needed() can keep retrying it.
            slot.printer = bl.Printer(slot.ip, slot.access_code, slot.serial)
            slot.printer.connect()
            slot.connected_at = time.time()
        except Exception as e:
            await log_fn(f'Failed to connect to printer {num}: {e}')

    async def reconnect_if_needed(self, log_fn):
        for num in self.numbers():
            slot = self.slots.get(num)
            printer = slot.printer if slot else None
            if not printer:
                continue
            if printer.mqtt_client_connected():
                if num in self._logged_disconnected:
                    self._logged_disconnected.discard(num)
                    await log_fn(f'Printer {num} is back online')
                continue

            # Only log once per outage; retries continue silently until it's back
            first_failure = num not in self._logged_disconnected
            if first_failure:
                self._logged_disconnected.add(num)
                await log_fn(f'Printer {num} ({slot.ip}) is unreachable, retrying quietly until it reconnects')
            try:
                printer.connect()
                slot.connected_at = time.time()
            except Exception as e:
                if first_failure:
                    print(f'Failed to reconnect printer {num}: {e}')

    async def add_printer(self, num: int, config: PrinterConfig, log_fn):
        self.slots[num] = PrinterSlot(ip=config.ip, access_code=config.access_code, serial=config.serial, mac=config.mac)
        await self._connect(num, log_fn)

    async def remove_printer(self, num: int):
        """Forget a printer and disconnect it in the background."""
        slot = self.slots.pop(num, None)
        self._logged_disconnected.discard(num)
        if slot and slot.printer:
            # disconnect() joins the camera thread, which can block for a long time on an
            # unreachable printer; run it off the event loop and stop waiting after a while
            try:
                await asyncio.wait_for(asyncio.to_thread(slot.printer.disconnect), DISCONNECT_TIMEOUT)
            except Exception as e:
                print(f'Printer {num} did not disconnect cleanly: {e}')

    async def replace_printer(self, num: int, config: PrinterConfig, log_fn):
        """Reconnect a printer with new connection details."""
        await self.remove_printer(num)
        await self.add_printer(num, config, log_fn)

    def disconnect_all(self):
        for num, slot in self.slots.items():
            if slot.printer:
                try:
                    slot.printer.disconnect()
                except Exception as e:
                    print(f'Failed to disconnect printer {num}: {e}')

    async def restart(self, num: int):
        """Reboot a printer and re-establish the connection. Raises on failure."""
        printer = self.get_printer(num)
        if not printer:
            raise LookupError(f'Printer {num} not found.')

        def reboot_and_reconnect():
            printer.reboot()
            printer.disconnect()
            printer.connect()

        # disconnect() can block on a stuck camera thread, so keep it off the event loop
        try:
            await asyncio.wait_for(asyncio.to_thread(reboot_and_reconnect), RESTART_TIMEOUT)
        except asyncio.TimeoutError:
            raise TimeoutError(f'no response after {RESTART_TIMEOUT}s') from None
        self.slots[num].connected_at = time.time()

    # --- Accessors ---

    def get_printer(self, num: int) -> Printer | None:
        slot = self.slots.get(num)
        return slot.printer if slot else None

    def seconds_since_connect(self, num: int) -> float:
        return time.time() - self.slots[num].connected_at

    def get_online_printer(self, num: int) -> Printer | None:
        """The printer if it's connected and has reported state, else None."""
        printer = self.get_printer(num)
        if printer and printer.mqtt_client_connected() and printer.mqtt_client_ready():
            return printer
        return None

    def snapshot(self, num: int) -> PrinterSnapshot | None:
        printer = self.get_online_printer(num)
        if not printer:
            return None
        return PrinterSnapshot(
            state=printer.get_state(),
            print_state=printer.get_current_state(),
            progress=to_int(printer.get_percentage()),
            time_left=format_duration(printer.get_time()),
            layer=to_int(printer.current_layer_num()),
            total_layers=to_int(printer.total_layer_num()),
        )

    def file_name(self, num: int) -> str:
        """Name of the current (or last) print: the project name, falling back to the gcode file."""
        printer = self.get_online_printer(num)
        if not printer:
            return ''
        name = printer.subtask_name() or printer.get_file_name() or ''
        return name.rsplit('/', 1)[-1]

    def details(self, num: int) -> PrinterDetails | None:
        printer = self.get_online_printer(num)
        if not printer:
            return None
        level = to_int(printer.mqtt_dump().get('print', {}).get('spd_lvl'))
        return PrinterDetails(
            file_name=self.file_name(num),
            nozzle_temp=printer.get_nozzle_temperature(),
            bed_temp=printer.get_bed_temperature(),
            chamber_temp=printer.get_chamber_temperature(),
            speed_level=SPEED_LEVELS.get(level, 'unknown'),
            speed_percent=to_int(printer.get_print_speed()),
        )

    def set_speed(self, num: int, level: int) -> bool:
        """Switch the print speed profile (1 silent .. 4 ludicrous)."""
        printer = self.get_online_printer(num)
        if not printer:
            return False
        # Printer.set_print_speed() only accepts 0-3, but the printer's profiles are 1-4
        return bool(printer.mqtt_client.set_print_speed_lvl(level))

    # --- State polling ---

    def poll(self, num: int) -> list[PrinterEvent]:
        """Compare a printer's state with the previous poll and return the resulting events."""
        printer = self.get_online_printer(num)
        if not printer:
            return []
        try:
            return list(self._diff_state(num, printer))
        except Exception as e:
            print(f'Error checking printer {num} state: {e}')
            return []

    def _diff_state(self, num: int, printer: Printer):
        slot = self.slots[num]
        prev_gcode_state, prev_print_state = slot.prev_state
        gcode_state = printer.get_state()
        print_state = printer.get_current_state()
        slot.prev_state = (gcode_state, print_state)

        def event(event_type: EventType, **data) -> PrinterEvent:
            return PrinterEvent(type=event_type, printer_num=num, printer=printer, data=data)

        # Check for gcode state changes
        if prev_gcode_state != GcodeState.UNKNOWN and prev_gcode_state != gcode_state:
            yield event(EventType.STATE_CHANGED, prev=prev_gcode_state, new=gcode_state)

            if gcode_state == GcodeState.FINISH:
                yield event(EventType.PRINT_FINISHED)

            elif gcode_state == GcodeState.FAILED:
                yield event(EventType.PRINT_FAILED, error_code=printer.print_error_code())

            elif prev_gcode_state == GcodeState.RUNNING and gcode_state == GcodeState.PAUSE:
                now = time.time()
                if now - slot.last_paused_time > PAUSE_NOTIFY_COOLDOWN:
                    slot.last_paused_time = now
                    yield event(EventType.PRINT_PAUSED, error_code=printer.print_error_code())

            elif prev_gcode_state in PRE_PRINT_STATES and gcode_state == GcodeState.RUNNING:
                slot.prev_layer = 0  # Reset layer tracking for new print
                yield event(EventType.PRINT_STARTED)

        # Check for layer changes
        if gcode_state == GcodeState.RUNNING:
            current_layer = to_int(printer.current_layer_num())
            prev_layer = slot.prev_layer
            if current_layer != prev_layer:
                slot.prev_layer = current_layer
                yield event(EventType.LAYER_CHANGED, prev_layer=prev_layer, layer=current_layer)

        # Check for print state changes
        if prev_print_state != PrintStatus.UNKNOWN and prev_print_state != print_state:
            yield event(EventType.STATE_CHANGED, prev_print=prev_print_state, new_print=print_state)

    def status_line(self, num: int) -> str:
        snap = self.snapshot(num)
        if not snap:
            return f'{num}: OFFLINE'

        line = f'{num}: {snap.state} ({snap.print_state}'
        if snap.state not in IDLE_STATES:
            line += f', {snap.progress}% done, {snap.time_left} left, L:{snap.layer}/{snap.total_layers}'
        line += ')'
        if not self.has_camera_frame(num):
            line += ' [NO CAM]'
        return line

    def get_status_lines(self) -> list[str]:
        return [self.status_line(num) for num in self.numbers()]

    def get_status_text(self) -> str:
        body = '\n'.join(self.get_status_lines())
        return (
            'Printer Statuses:```c\n'
            f'{body}\n'
            'Note: "FINISH/IDLE" means not in use\n'
            f'Updated on: {time.strftime("%Y-%m-%d %H:%M")}\n'
            '```\n'
        )

    # --- Light & camera ---

    def is_light_on(self, num: int) -> bool:
        printer = self.get_online_printer(num)
        return bool(printer) and printer.get_light_state() == 'on'

    def set_light(self, num: int, on: bool):
        printer = self.get_online_printer(num)
        if printer:
            printer.turn_light_on() if on else printer.turn_light_off()

    def has_camera_frame(self, num: int) -> bool:
        printer = self.get_online_printer(num)
        if not printer:
            return False
        # A capture in progress clears the frame on purpose; don't report that as stale.
        return self.slots[num].camera_lock.locked() or printer.camera_client.last_frame is not None

    async def get_camera_frame(self, num: int) -> bytes | None:
        """Turn on the light, capture a fresh frame, then restore the light."""
        printer = self.get_online_printer(num)
        if not printer:
            return None

        # Serialise captures per printer so concurrent requests don't clear each other's frame
        async with self.slots[num].camera_lock:
            light_was_on = self.is_light_on(num)
            if not light_was_on:
                printer.turn_light_on()
            printer.camera_client.last_frame = None
            try:
                for _ in range(CAMERA_MAX_POLLS):
                    await asyncio.sleep(CAMERA_POLL_INTERVAL)
                    if printer.camera_client.last_frame:
                        break
                frame = printer.camera_client.last_frame
            finally:
                if not light_was_on:
                    try:
                        printer.turn_light_off()
                    except Exception as e:
                        print(f'Failed to turn off printer {num} light: {e}')

        return bytes(frame) if frame else None
