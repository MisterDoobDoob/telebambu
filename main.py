import asyncio
import signal

from settings import load as load_settings
from data import Storage
from bot import create_application, get_bot_context, setup_handlers, MessageService
from printers import PrinterManager, PrinterMonitor


async def main():
    settings = load_settings()
    storage = Storage()
    storage.ensure_owner(settings.owner_id)
    printer_manager = PrinterManager(settings.printers)
    app = create_application(settings)
    message_service = MessageService(app.bot, get_bot_context(settings), storage)
    setup_handlers(app, storage, message_service, printer_manager, settings)
    monitor = PrinterMonitor(printer_manager, message_service, settings)

    # Stop cleanly on Ctrl+C or `kill` (stop.sh)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async with app:
        await printer_manager.connect_all(message_service.log_message)
        await app.start()
        await app.updater.start_polling()
        await message_service.log_message('Bot started!')

        monitor_task = asyncio.create_task(monitor.run())
        try:
            await stop.wait()
        finally:
            print('Shutting down...')
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
            await message_service.flush_logs(force=True)
            await app.updater.stop()
            await app.stop()
            # Disabled: printer.disconnect() can hang forever joining the camera thread
            # if it's stuck in a blocking socket connect to an unreachable printer,
            # which blocks the whole event loop and prevents shutdown.
            # printer_manager.disconnect_all()


if __name__ == '__main__':
    asyncio.run(main())
