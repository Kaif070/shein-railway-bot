import json, os, random, re, time, traceback
from pathlib import Path
from urllib.parse import urljoin
import requests
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# ── Load .env in dev; Railway uses Variables tab
load_dotenv()

# ── Config
SHEIN_URL = os.getenv("SHEIN_URL", "https://www.sheinindia.in/c/sverse-5939-37961")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")  # optional; we’ll reply to any user who presses buttons too
CACHE_PATH = Path("seen.json")
ALERT_MODE = os.getenv("ALERT_MODE", "new_only")  # "new_only" = alert only brand-new + in-stock
SCAN_INTERVAL_MIN = int(os.getenv("SCAN_INTERVAL_MIN", "15"))

# Optional proxy for Playwright (HIGHLY RECOMMENDED for Shein India)
PROXY_SERVER = os.getenv("PROXY_SERVER")          # e.g. "http://219.65.73.81:8080"
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")

# Keywords that usually mean "out of stock"
OOS_PATTERNS = [r"out\s*of\s*stock", r"sold\s*out", r"unavailable", r"notify\s*me"]

# ── Telegram endpoints
TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

# ── Simple helpers
def send_telegram(text: str, chat_id: str | None = None, reply_markup: dict | None = None):
    if not TG_API:
        print("Missing TELEGRAM_BOT_TOKEN")
        return
    if not chat_id:
        chat_id = CHAT_ID
    if not chat_id:
        print("Missing TELEGRAM_CHAT_ID and no chat_id provided")
        return

    payload = {
        "chat_id": chat_id,
        "text": text[:4096],
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        r = requests.post(f"{TG_API}/sendMessage", data=payload, timeout=20)
        if r.status_code >= 400:
            print("Telegram send error:", r.text)
    except Exception as e:
        print("Telegram error:", e)

def answer_callback(cb_id: str, text: str = ""):
    if not TG_API: return
    try:
        requests.post(f"{TG_API}/answerCallbackQuery", data={"callback_query_id": cb_id, "text": text[:200]}, timeout=15)
    except Exception:
        pass

def send_menu(chat_id: str | None = None):
    kb = {
        "inline_keyboard": [
            [{"text": "🔍 Check stock", "callback_data": "check"}],
            [{"text": "🔄 Refresh (clear cache + rescan)", "callback_data": "refresh"}],
        ]
    }
    send_telegram("Choose an action:", chat_id=chat_id, reply_markup=kb)

def tg_get_updates(offset: int | None):
    if not TG_API:
        return []
    params = {"timeout": 25}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"{TG_API}/getUpdates", params=params, timeout=30)
        data = r.json()
        return data.get("result", [])
    except Exception as e:
        print("getUpdates error:", e)
        return []

# ── Cache
def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

def is_oos(text: str) -> bool:
    t = (text or "").strip().lower()
    for pat in OOS_PATTERNS:
        if re.search(pat, t):
            return True
    return False

# ── Core scraping with Playwright
def scrape_once(debug_screenshot: str = "debug.png"):
    """
    Returns list of dicts:
      [{"id": "...", "title": "...", "href": "...", "oos": True/False}]
    Saves a debug screenshot on failure/block for inspection.
    """
    results = []

    # Build proxy dict for Playwright if present
    proxy = None
    if PROXY_SERVER:
        proxy = {"server": PROXY_SERVER}
        if PROXY_USERNAME and PROXY_PASSWORD:
            proxy["username"] = PROXY_USERNAME
            proxy["password"] = PROXY_PASSWORD

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy=proxy,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

        context = browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
            extra_http_headers={
                "Accept-Language": "en-IN,en;q=0.9",
            },
        )
        page = context.new_page()
        try:
            time.sleep(random.uniform(1.0, 2.0))
            page.goto(SHEIN_URL, wait_until="domcontentloaded", timeout=60000)
            # If the page is an Akamai/Access Denied, bail after screenshot
            body_text = (page.inner_text("body") or "").lower()
            if "access denied" in body_text or "permission to access" in body_text:
                try:
                    page.screenshot(path=debug_screenshot, full_page=True)
                except Exception:
                    pass
                print("[debug] Access denied page detected")
                return []

            # Let lazy content settle
            page.wait_for_load_state("networkidle", timeout=90000)

            # Heuristic selectors (DOM may change over time)
            cards = page.locator(
                '[data-sqin="product-card"], .product-card, [class*="product"] article, a[href*="/product/"]'
            )

            # Scroll & load a bit to improve capture
            try:
                for _ in range(3):
                    page.mouse.wheel(0, 1200)
                    time.sleep(0.75)
            except Exception:
                pass

            elements = cards.all()
            seen_ids = set()

            for el in elements:
                try:
                    href = el.get_attribute("href")
                    if not href:
                        link_el = el.locator("a").first
                        if link_el and link_el.count() > 0:
                            href = link_el.get_attribute("href")
                    if href and href.startswith("/"):
                        href = urljoin(SHEIN_URL, href)

                    title = None
                    for sel in ['[title]', '.product-title', '[class*="title"]', "a"]:
                        try:
                            cand = el.locator(sel).first
                            if cand and cand.count() > 0:
                                val = cand.get_attribute("title") or cand.inner_text(timeout=800)
                                if val and len(val.strip()) > 1:
                                    title = " ".join(val.strip().split())
                                    break
                        except Exception:
                            pass

                    badge_text = ""
                    for bsel in ['[class*="badge"]', '[class*="oos"]', '[class*="sold"]',
                                 'text=Sold Out', 'text=Out of stock', '[class*="stock"]']:
                        try:
                            b = el.locator(bsel).first
                            if b and b.count() > 0:
                                badge_text = (b.inner_text(timeout=600) or "").strip()
                                if badge_text:
                                    break
                        except Exception:
                            pass

                    all_text = ""
                    try:
                        all_text = el.inner_text(timeout=800)
                    except Exception:
                        pass

                    oos_flag = is_oos(badge_text or all_text)
                    pid = (href or (title or "")).strip()[:300]
                    if pid and pid not in seen_ids:
                        seen_ids.add(pid)
                        results.append({
                            "id": pid,
                            "title": title or "Unknown item",
                            "href": href or SHEIN_URL,
                            "oos": oos_flag
                        })
                except Exception:
                    continue

            # Screenshot for debugging success state too
            try:
                page.screenshot(path=debug_screenshot, full_page=True)
            except Exception:
                pass

        except Exception as e:
            print("[scrape] error:", e)
            traceback.print_exc()
            try:
                page.screenshot(path=debug_screenshot, full_page=True)
            except Exception:
                pass
        finally:
            context.close()
            browser.close()

    return results

# ── Business logic
def build_alerts(items: list[dict], cache: dict):
    """
    Decide which items to alert per ALERT_MODE.
    Returns: (notifications, updated_cache)
    """
    notifications = []
    for it in items:
        prev = cache.get(it["id"])

        if ALERT_MODE == "new_only":
            # alert for brand-new items that are currently in stock
            if prev is None and it["oos"] is False:
                notifications.append(it)
        else:
            # fallback: alert if in stock and was unseen or previously OOS
            if it["oos"] is False and (prev is None or (prev and prev.get("oos") is True)):
                notifications.append(it)

        # always update cache
        cache[it["id"]] = {"oos": it["oos"], "title": it["title"], "href": it["href"]}

    return notifications, cache

def notify(notifications: list[dict], chat_id: str | None = None):
    if not notifications:
        send_telegram("No items are in stock right now.", chat_id=chat_id)
        return
    lines = ["🟢 <b>SHEIN new stock alert</b>:"]
    for n in notifications[:50]:  # guard against huge spam
        lines.append(f"• {n['title']} — <b>In stock</b>\n{n['href']}")
    send_telegram("\n".join(lines), chat_id=chat_id)

def check_now(chat_id: str | None = None, clear_cache: bool = False):
    if clear_cache and CACHE_PATH.exists():
        try:
            CACHE_PATH.unlink()
        except Exception:
            pass

    cache = load_cache()
    items = scrape_once()
    total = len(items)
    instock = sum(1 for x in items if not x["oos"])

    # If no products were parsed at all, hint blocking
    if total == 0:
        send_telegram(
            "⚠️ I couldn’t read the products (possibly blocked). "
            "I saved <code>debug.png</code> in the app folder. "
            "Open the Railway shell and run:\n\n"
            "<code>ls -l debug.png</code>\n"
            "Then download it with:\n"
            "<code>cat debug.png</code> (copy via your terminal) or use SFTP.",
            chat_id=chat_id,
        )
        return

    notifications, new_cache = build_alerts(items, cache)
    save_cache(new_cache)

    # summary first
    send_telegram(f"🔎 Parsed <b>{total}</b> products. In stock: <b>{instock}</b>.", chat_id=chat_id)
    # then alerts if any
    if notifications:
        notify(notifications, chat_id=chat_id)
    else:
        send_telegram("No brand-new in-stock items to alert right now.", chat_id=chat_id)

# ── Main loop: handle Telegram buttons + periodic scans
def main():
    print("[bot] starting loop...")
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN is required.")
    last_update_id = None
    last_scan_ts = 0

    # Send a one-time “started” ping
    try:
        if CHAT_ID:
            send_menu(CHAT_ID)
            send_telegram("✅ Bot started.", chat_id=CHAT_ID)
    except Exception:
        pass

    while True:
        # 1) Handle Telegram updates (buttons + /start)
        updates = tg_get_updates(last_update_id + 1 if last_update_id else None)
        for upd in updates:
            last_update_id = upd["update_id"]

            # Button press
            if "callback_query" in upd:
                cq = upd["callback_query"]
                cb_id = cq.get("id")
                from_chat = str(cq["from"]["id"])
                data = cq.get("data")

                if data == "check":
                    answer_callback(cb_id, "Checking now…")
                    check_now(chat_id=from_chat, clear_cache=False)
                    send_menu(from_chat)
                elif data == "refresh":
                    answer_callback(cb_id, "Refreshing…")
                    check_now(chat_id=from_chat, clear_cache=True)
                    send_menu(from_chat)
                else:
                    answer_callback(cb_id, "Unknown action")

            # /start or normal message
            elif "message" in upd:
                msg = upd["message"]
                text = (msg.get("text") or "").strip().lower()
                from_chat = str(msg["chat"]["id"])

                if text == "/start":
                    send_telegram("Hi! I can watch SHEIN stock. Use the buttons below.", chat_id=from_chat)
                    send_menu(from_chat)
                elif text in ("/check", "check"):
                    check_now(chat_id=from_chat, clear_cache=False)
                elif text in ("/refresh", "refresh"):
                    check_now(chat_id=from_chat, clear_cache=True)
                else:
                    send_menu(from_chat)

        # 2) Periodic auto-scan (alerts to default CHAT_ID if set)
        now = time.time()
        if now - last_scan_ts > SCAN_INTERVAL_MIN * 60:
            print("[bot] auto-scan…")
            try:
                cache = load_cache()
                items = scrape_once()
                notifications, new_cache = build_alerts(items, cache)
                save_cache(new_cache)
                if notifications and CHAT_ID:
                    notify(notifications, chat_id=CHAT_ID)
                print(f"[debug] total={len(items)} instock={sum(1 for x in items if not x['oos'])} alerts={len(notifications)}")
            except Exception as e:
                print("[scan] error:", e)
            last_scan_ts = now

        time.sleep(2)

if __name__ == "__main__":
    main()
