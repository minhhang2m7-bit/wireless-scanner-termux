# wireless-scanner-termux

A modern Termux toolkit for scanning Wi‑Fi, testing internet speed, checking websites, scheduling SMS/calls, and tracking network history.

## Features
- Wi‑Fi scanning with signal strength and security info
- Active network detection (Wi‑Fi / mobile data)
- Latency, download and upload testing
- Website IP / DNS / HTTP analysis
- SMS and call scheduling via Termux:API
- Local history and settings persistence
- Modern Rich-based terminal UI

## Requirements
- Android + Termux
- Python 3.9+
- `termux-api` package and permissions

## Install on Termux
```bash
pkg update -y
pkg install -y python git
pip install -r requirements.txt
```

## Run
```bash
python wireless_scanner.py
```

## Optional Termux:API setup
```bash
pkg install -y termux-api
```

Then grant permissions in: `Settings > Apps > Termux:API > Permissions`

Required permissions:
- Location
- Phone
- SMS

## Notes
This tool is intended only for networks and devices that you are authorized to test.
