# Telebambu

A Telegram bot for monitoring and managing a fleet of Bambu Labs 3D printers. The bot connects to printers via MQTT and provides real-time status updates, print notifications, and camera access through Telegram.

## Features

- Real-time monitoring of multiple Bambu Labs printers
- Telegram notifications when prints start, finish, or encounter errors
- Users can "claim" print jobs and receive personalized updates
- Live camera access to view printers
- Layer 2 progress notifications
- Persistent status message showing all printer states
- Configurable notification preferences (chat or DM)

## Requirements

- Python 3.8+
- Bambu Labs printer(s) on the same network
- Telegram Bot Token (from [@BotFather](https://t.me/botfather))

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/pcider/telebambu.git
   cd telebambu
   ```

2. Create a virtual environment (recommended):
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Configuration

1. Copy the example configuration file:
   ```bash
   cp config_example.yml config.yml
   ```

2. Edit `config.yml` with your bot token, chat IDs, owner ID and printers. See the comments in
   `config_example.yml` for what each setting does. Printers are listed by number:

   ```yaml
   printers:
     1:
       mac: 'AA:BB:CC:DD:EE:FF'
       ip: '192.168.1.123'
       access_code: '12345678'
       serial: '0123456789ABCDE'
   ```

   Upgrading from `config.py`? Start the bot once without a `config.yml` and it will create one
   from your `config.py`, which can then be deleted.

   The owner can change `chat_id`, `status_chat_id`, `log_chat_id` and `update_interval` at runtime
   with `/config`. The bot rewrites `config.yml` when settings change, so hand-written comments
   are not kept.

   **Finding your printer information:**
   - **IP Address**: Check your router's DHCP client list or the printer's network settings
   - **Access Code**: Found in the printer's LAN settings
   - **Serial Number**: Found in the printer's device info
   - **MAC Address**: Found in the printer's network settings

## Running the Bot

### Direct execution

```bash
python3 main.py
```

### Using the start script (recommended for production)

The `start.sh` script runs the bot in the background with logging:

```bash
chmod +x start.sh  # Make executable (first time only)
./start.sh
```

This will:
- Change to the script's directory
- Log a restart timestamp to `telebambu.log`
- Run the bot with output redirected to `telebambu.log`
- Write the bot's PID to `telebambu.pid` so `./stop.sh` can shut it down cleanly

To view logs in real-time:
```bash
tail -f telebambu.log
```

### Running as a service (Linux)

For persistent operation, create a systemd service:

```bash
sudo nano /etc/systemd/system/telebambu.service
```

```ini
[Unit]
Description=Telebambu Telegram Bot
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/path/to/telebambu
ExecStart=/path/to/telebambu/venv/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl enable telebambu
sudo systemctl start telebambu
```

## Usage

### Telegram Commands

`[printer]` is optional when you have exactly one print claimed.

- `/help` - List available commands
- `/info [printer]` - Show progress, time remaining, layer and pending notifications for your print
- `/camera [printer]` - Get a fresh camera snapshot (owner can access all printers, users can access their claimed printers)
- `/light [printer]` - Toggle the printer light (owner or claimer)
- `/notify [printer] <layer>` or `/notify [printer] <percent>%` - Send a one-time notification with a camera snapshot at a milestone
- `/notify_every [printer] layers|time|percent <number>` - Send recurring camera snapshots during a print
- `/notify_every [printer] off` - Disable recurring camera snapshots
- `/unclaim [printer]` - Release your claim on a print
- `/restart <printer>` - Reboot and reconnect a printer (owner only)

### Interactive Buttons

When a print starts, the bot sends a message with a **Claim Print** button. Claiming a print allows you to:
- Receive the finish notification directly
- Choose between chat or DM delivery
- Enable/disable Layer 2 progress notifications
- Access the printer's camera

## Project Structure

```
telebambu/
├── main.py              # Application entry point
├── settings.py          # Loads and saves config.yml
├── config.yml           # Configuration (create from config_example.yml)
├── config_example.yml   # Configuration template
├── requirements.txt     # Python dependencies
├── start.sh             # Shell script to run the bot
├── bot/                 # Telegram bot module
│   ├── telegram_bot.py  # Bot initialization
│   ├── handlers.py      # Command and callback handlers
│   ├── messages.py      # Message sending service
│   └── ui.py            # Shared message texts and inline keyboards
├── printers/            # Printer management module
│   ├── manager.py       # Printer connection manager
│   └── monitor.py       # State monitoring loop
└── data/                # Data persistence module
    └── storage.py       # Session and preference storage
```

## License

This project is licensed under the GNU Affero General Public License v3.0. See [LICENSE](LICENSE) for details.
