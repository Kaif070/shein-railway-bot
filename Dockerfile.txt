FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# ---- System deps needed by Chromium/Playwright ----
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates fonts-liberation wget xdg-utils \
    # core GLib/GObject/GIO
    libglib2.0-0 libgobject-2.0-0 \
    # NSS/NSPR + DBus
    libnspr4 libnss3 libdbus-1-3 \
    # X11 + GPU + windowing bits
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 \
    libxdamage1 libxext6 libxfixes3 libxrandr2 libxrender1 \
    libxss1 libxtst6 libgbm1 libdrm2 libxkbcommon0 \
    # accessibility / atk
    libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 \
    # graphics / fonts / printing
    libcairo2 libpango-1.0-0 libcups2 \
    # audio (Chromium requires even in headless)
    libasound2 \
    # misc runtime
    libexpat1 \
    # optional but useful
    libgtk-3-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Install the Chromium browser itself at build time
RUN python -m playwright install chromium

# App code
COPY shein_stock_bot.py entrypoint.sh ./
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
