#!/usr/bin/env bash
set -e

# one-time browser install (first container boot)
python -m playwright install chromium

echo "[bot] starting loop..."
while true; do
  python shein_stock_bot.py
  # wait 10 minutes between checks
  sleep 600
done
