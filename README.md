# BrawlStar-Bot

Brawl Stars automation bot with a Telegram interface and a web dashboard.

The bot drives an Android emulator (or a real device via USB) over
`adb` + `scrcpy`, plays matches with on-device ONNX vision models,
and tracks per-account match/session/trophy history in SQLite.

## Features

- **Telegram control** — interactive `/start` wizard with the brawler
  list filtered to those you own (scraped from brawlace.com), `/stop`,
  `/forcestop`, `/status`, `/screenshot`.
- **Web panel** (`http://127.0.0.1:8000`) — live KPIs, account
  trophy progression chart, win-rate-by-brawler, recent matches +
  session history, controls (Start, Push max, Stop, Force stop), a
  global Settings modal for editing Telegram alerts on the fly.
- **Push-max mode** — smart rotation across every brawler you own:
  picks the brawler with the highest expected gain, switches after N
  consecutive defeats, stops when every brawler is exhausted.
- **Configurable Telegram alerts** — toggle and template each event
  (match, target reached, cycle started, …) in `cfg/alerts.toml`. Hot
  reload, no restart needed.
- **Auto account detection** — taps the in-game profile, OCRs the
  player tag (`#XXXXXXX`), and fetches the owned-brawlers list from
  brawlace.com. Re-detected on each `/start` so you can swap accounts
  between runs.
- **Multi-account ready** — every component (DB schema, `WorkerPool`,
  panel routes) is keyed by `account_id`. Adding a second worker
  bound to a different device serial is a one-liner.
- **GPU OCR on Apple Silicon** — EasyOCR runs on MPS by default.
  CUDA on nvidia, CPU fallback elsewhere.

## Requirements

- Python 3.12+
- ADB + scrcpy installed on the host
- An Android emulator (BlueStacks on macOS/Windows) or a real Android
  device connected over USB with debugging enabled
- Brawl Stars installed and signed in on the emulator/device

## Setup

```bash
git clone https://github.com/Zeffut/BrawlStar-Bot.git
cd BrawlStar-Bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt   # or see setup notes
cp cfg/telegram.toml.example cfg/telegram.toml  # fill in your bot token + chat id
python telegram_main.py
```

Open the panel at <http://127.0.0.1:8000>, or interact via Telegram.

## Project layout

```
telegram_main.py    — entry point (Telegram bot + panel server + bot worker)
panel/              — FastAPI backend + single-page HTML dashboard
db.py               — SQLite schema and helpers
worker_pool.py      — multi-account worker registry
account_detect.py   — in-game profile OCR + brawlace.com scraper
push_max.py         — smart-rotation strategy
alerts.py           — configurable Telegram alerts (hot-reloaded)
logging_setup.py    — central logging configuration
stage_manager.py    — state-machine match driver
play.py             — match-play logic
lobby_automation.py — brawler selection in the in-game menu
state_finder/       — screen-state detection (lobby/match/popup/...)
models/             — ONNX vision models
cfg/                — configuration files
data/               — SQLite database (gitignored)
logs/               — rotating bot logs (gitignored)
```

## License

See [LICENSE](LICENSE).
