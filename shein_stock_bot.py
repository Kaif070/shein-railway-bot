import json, os, random, re, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

# === CONFIG ===
SHEIN_URL = os.getenv("SHEIN_URL", "https://www.sheinindia.in/c/sverse-5939-37961")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")              # used for push alerts
CACHE_PATH = Path("seen.json")
ALERT_MODE = os.getenv("ALERT_MODE", "new_only").lower()  # new_only | restock | any_instock
VERBOSE = os.getenv("VERBOSE", "0") == "1"
CHECK_EVERY = int(os.getenv("CHECK_EVERY", "600"))   # automatic check interval (sec)

# OOS / ID helpers
OOS_PATTERNS = [r"out\s*of\s*stock", r"sold\s*out", r"unavailable", r"notify\s*me"]
ID_PATTERNS = [re.compile(r"/product/(\d+)"), re.compile(r"/p-(\d+)-"), re.compile(r"item/(\d+)")]

# --- Telegram helpers ---
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""

def tg(method: str, **data):
    if not API_BASE:
        print("Missing TELEGRAM_BOT_TOKEN")
        return None
    try:
        r = requests.post(f"{API_BASE}/{method}", json=data, timeout=25)
        if r.status_code >= 300:
            print(f"[warn] Telegram {method} failed:", r.status_code, r.text[:200])
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else None
    except Exception as e:
        print(f"[warn] Telegram error on {method}:", e)
        return None

def send_text(chat_id, text, reply_markup=None):
    return tg("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True, reply_markup=reply_markup)

def edit_text(chat_id, message_id, text, reply_markup=None):
    return tg("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
              disable_web_page_preview=True, reply_markup=reply_markup)

def answer_cbq(cbq_id, text=""):
    return tg("answerCallbackQuery", callback_query_id=cbq_id, text=text, show_alert=False)

def menu_markup():
    return {
        "inline_keyboard": [[
            {"text": "🔎 Check stock", "callback_data": "check"},
            {"text": "🔄 Refresh",     "callback_data": "refresh"},
        ]]
    }

def send_menu(chat_id):
    return send_text(chat_id, "What would you like to do?", reply_markup=menu_markup())

# --- Cache ---
def load_cache():
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_cache(cache):
    CACHE_PATH.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")

# --- Scraping ---
def is_oos(text: str) -> bool:
    t = (text or "").strip().lower()
    for pat in OOS_PATTERNS:
        if re.search(pat, t):
            return True
    return False

def extract_id(url: str, title: str) -> str:
    if url:
        path = urlparse(url).path
        for rx in ID_PATTERNS:
            m = rx.search(path)
            if m:
                return m.group(1)
        m = re.search(r"(\d{6,})", path)
        if m:
            return m.group(1)
        return path.strip("/")[:200]
    return (title or "unknown")[:200]

def scrape_once():
    """
    Returns list of dicts:
      {"id": "...", "title": "...", "href": "...", "in_stock": True/False}
    """
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        time.sleep(random.uniform(0.8, 1.6))

        page.goto(SHEIN_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_load_state("networkidle", timeout=90000)
        for _ in range(4):
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            time.sleep(0.8)

        cards = page.locator(
            '[data-sqin="product-card"], .product-card, [class*="product"] article, a[href*="/product/"]'
        ).all()

        if VERBOSE:
            print(f"[debug] located {len(cards)} candidate cards")

        seen_ids = set()
        for el in cards:
            try:
                href = el.get_attribute("href")
                if not href:
                    link_el = el.locator("a").first
                    if link_el and link_el.count() > 0:
                        href = link_el.get_attribute("href")
                if href and href.startswith("/"):
                    href = urljoin(SHEIN_URL, href)

                title = None
                for sel in ['[title]', '.product-title', '[class*="title"]', "a[title]", "a"]:
                    try:
                        cand = el.locator(sel).first
                        if cand and cand.count() > 0:
                            val = cand.get_attribute("title") or cand.inner_text(timeout=1200)
                            if val and len(val.strip()) > 1:
                                title = " ".join(val.strip().split())
                                break
                    except Exception:
                        pass

                badge_text = ""
                for bsel in [
                    '[class*="badge"]', '[class*="oos"]', '[class*="sold"]',
                    'text=Sold Out', 'text=Out of stock', '[class*="stock"]'
                ]:
                    try:
                        b = el.locator(bsel).first
                        if b and b.count() > 0:
                            badge_text = (b.inner_text(timeout=1200) or "").strip()
                            if badge_text:
                                break
                    except Exception:
                        pass

                all_text = ""
                try:
                    all_text = el.inner_text(timeout=1200)
                except Exception:
                    pass

                in_stock = not is_oos(badge_text or all_text)
                pid = extract_id(href, title)

                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    results.append({
                        "id": pid,
                        "title": title or "Unknown item",
                        "href": href or SHEIN_URL,
                        "in_stock": in_stock
                    })
            except Exception:
                continue

        context.close()
        browser.close()
    return results

# --- Bot logic ---
def summarize_instock(items):
    instock = [it for it in items if it["in_stock"]]
    if not instock:
        return "No items are in stock right now."
    lines = ["🟢 In-stock items:"]
    for it in instock[:20]:
        lines.append(f"• {it['title']}\n{it['href']}")
    if len(instock) > 20:
        lines.append(f"...and {len(instock)-20} more.")
    return "\n".join(lines)

def decide_alerts(prev, current):
    alerts = []
    if ALERT_MODE == "new_only":
        for iid, cur in current.items():
            if cur["in_stock"] and iid not in prev:
                alerts.append(cur)
    elif ALERT_MODE == "restock":
        for iid, cur in current.items():
            was = prev.get(iid, {"in_stock": False})
            if cur["in_stock"] and not was.get("in_stock", False):
                alerts.append(cur)
    elif ALERT_MODE == "any_instock":
        alerts = [cur for cur in current.values() if cur["in_stock"]]
    return alerts

def run_check_and_maybe_alert():
    items = scrape_once()
    prev = load_cache()
    current = {it["id"]: {"in_stock": it["in_stock"], "title": it["title"], "href": it["href"]} for it in items}
    alerts = decide_alerts(prev, current)
    save_cache(current)

    total = len(items)
    in_now = sum(1 for v in current.values() if v["in_stock"])
    print(f"[debug] products_total={total} in_stock_now={in_now} alerts_to_send={len(alerts)} mode={ALERT_MODE}")

    if alerts:
        lines = ["🟢 SHEIN stock alert:"]
        for n in alerts:
            lines.append(f"• {n['title']} — In stock\n{n['href']}")
        if CHAT_ID:
            send_text(CHAT_ID, "\n".join(lines), reply_markup=menu_markup())
    return items  # for immediate replies

def handle_update(upd):
    # Message: /start or /check
    if "message" in upd:
        msg = upd["message"]
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip().lower()
        if text == "/start":
            send_text(chat_id, "Welcome! Use the buttons below to query stock.", reply_markup=menu_markup())
        elif text == "/check":
            send_text(chat_id, "Checking…")
            items = scrape_once()
            send_text(chat_id, summarize_instock(items), reply_markup=menu_markup())
        elif text == "/refresh":
            send_text(chat_id, "Refreshing…")
            items = scrape_once()
            send_text(chat_id, summarize_instock(items), reply_markup=menu_markup())

    # Button press (callback_query)
    if "callback_query" in upd:
        cq = upd["callback_query"]
        cbq_id = cq["id"]
        data = cq.get("data", "")
        chat_id = cq["message"]["chat"]["id"]
        mid = cq["message"]["message_id"]
        answer_cbq(cbq_id)  # stop spinner

        if data in ("check", "refresh"):
            edit_text(chat_id, mid, "⏳ Loading latest stock…")
            items = scrape_once()
            edit_text(chat_id, mid, summarize_instock(items), reply_markup=menu_markup())

def poll_telegram_loop():
    if not BOT_TOKEN:
        print("[warn] TELEGRAM_BOT_TOKEN not set; polling disabled.")
        return
    print("[bot] telegram polling started")
    offset = None
    idle_backoff = 1
    while True:
        try:
            params = {"timeout": 50}
            if offset is not None:
                params["offset"] = offset
            r = requests.get(f"{API_BASE}/getUpdates", params=params, timeout=60)
            data = r.json()
            if not data.get("ok"):
                time.sleep(2)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                handle_update(upd)
            idle_backoff = 1
        except Exception as e:
            print("[warn] polling error:", e)
            time.sleep(min(30, idle_backoff))
            idle_backoff = min(30, idle_backoff * 2)

def main():
    print("[bot] starting loop...")
    last_check = 0
    while True:
        # 1) serve Telegram button presses quickly
        poll_until = time.time() + 2.0  # poll for ~2s each cycle to stay responsive
        while time.time() < poll_until:
            poll_telegram_loop()  # returns only if token missing; otherwise it loops forever
            break                # if token exists, loop will not return; this break is for safety

        # 2) periodic automatic alerting
        now = time.time()
        if now - last_check >= CHECK_EVERY:
            try:
                run_check_and_maybe_alert()
            except Exception as e:
                print("[warn] periodic check failed:", e)
            last_check = now

        # small sleep to avoid tight spin if polling disabled
        time.sleep(2)

if __name__ == "__main__":
    main()
