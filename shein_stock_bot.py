import json, os, random, re, time
from pathlib import Path
from urllib.parse import urljoin
import requests
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

# === CONFIG ===
SHEIN_URL = os.getenv("SHEIN_URL", "https://www.sheinindia.in/c/sverse-5939-37961")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CACHE_PATH = Path("seen.json")
ALERT_MODE = os.getenv("ALERT_MODE", "new_only")  # keep "new_only" for your requirement

# Keywords that usually mean "out of stock"
OOS_PATTERNS = [r"out\s*of\s*stock", r"sold\s*out", r"unavailable", r"notify\s*me"]

def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        requests.post(url, data=payload, timeout=15)
    except Exception as e:
        print("Telegram error:", e)

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

def scrape_once():
    """
    Returns list of dicts:
    [
      {"id": "...", "title": "...", "href": "...", "oos": True/False}
    ]
    """
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-gpu"])
        context = browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/119.0 Safari/537.36"),
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()
        time.sleep(random.uniform(1.0, 2.0))
        page.goto(SHEIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_load_state("networkidle", timeout=90000)

        # Heuristic selectors (DOM may change over time)
        cards = page.locator(
            '[data-sqin="product-card"], .product-card, [class*="product"] article, a[href*="/product/"]'
        ).all()

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
                for sel in ['[title]', '.product-title', '[class*="title"]', "a"]:
                    try:
                        cand = el.locator(sel).first
                        if cand and cand.count() > 0:
                            val = cand.get_attribute("title") or cand.inner_text(timeout=1000)
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
                            badge_text = (b.inner_text(timeout=1000) or "").strip()
                            if badge_text:
                                break
                    except Exception:
                        pass

                all_text = ""
                try:
                    all_text = el.inner_text(timeout=1000)
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

        context.close()
        browser.close()
    return results

def main():
    items = scrape_once()
    cache = load_cache()  # { id: {"oos": bool, "title": str, "href": str} }
    notifications = []

    for it in items:
        prev = cache.get(it["id"])

        if ALERT_MODE == "new_only":
            # ✅ Your requirement: only alert for brand-new items that are currently in stock
            if prev is None and it["oos"] is False:
                notifications.append(it)
        else:
            # Fallback mode (not used by you)
            if it["oos"] is False and (prev is None or (prev and prev.get("oos") is True)):
                notifications.append(it)

        cache[it["id"]] = {"oos": it["oos"], "title": it["title"], "href": it["href"]}

    save_cache(cache)

    if notifications:
        lines = ["🟢 SHEIN new stock alert:"]
        for n in notifications:
            lines.append(f"• {n['title']} — In stock\n{n['href']}")
        send_telegram("\n".join(lines))
    else:
        print("No new in-stock items.")

if __name__ == "__main__":
    main()
