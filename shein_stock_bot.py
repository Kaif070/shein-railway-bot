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
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CACHE_PATH = Path("seen.json")
ALERT_MODE = os.getenv("ALERT_MODE", "new_only").lower()  # new_only | restock | any_instock
VERBOSE = os.getenv("VERBOSE", "0") == "1"

# Patterns that indicate OOS
OOS_PATTERNS = [r"out\s*of\s*stock", r"sold\s*out", r"unavailable", r"notify\s*me"]

# Try to extract a stable product id from typical SHEIN URLs
ID_PATTERNS = [
    re.compile(r"/product/(\d+)"),
    re.compile(r"/p-(\d+)-"),
    re.compile(r"item/(\d+)"),
]

def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        if r.status_code >= 300:
            print("[warn] Telegram send failed:", r.status_code, r.text[:200])
    except Exception as e:
        print("[warn] Telegram error:", e)

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

def extract_id(url: str, title: str) -> str:
    """Get a stable id from URL if possible, else fallback to a hash-ish string."""
    if url:
        path = urlparse(url).path
        for rx in ID_PATTERNS:
            m = rx.search(path)
            if m:
                return m.group(1)
        # fallback: last numeric block
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
        # Give it time to fetch content & lazy load
        page.wait_for_load_state("networkidle", timeout=90000)
        for _ in range(4):
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            time.sleep(0.8)

        # Product cards – keep this broad to survive minor DOM changes
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

                # Title
                title = None
                for sel in ['[title]', '.product-title', '[class*=\"title\"]', "a[title]", "a"]:
                    try:
                        cand = el.locator(sel).first
                        if cand and cand.count() > 0:
                            val = cand.get_attribute("title") or cand.inner_text(timeout=1200)
                            if val and len(val.strip()) > 1:
                                title = " ".join(val.strip().split())
                                break
                    except Exception:
                        pass

                # OOS badges / hints
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

def main():
    items = scrape_once()
    prev = load_cache()  # { id: {"in_stock": bool, "title": str, "href": str} }

    current = {it["id"]: {"in_stock": it["in_stock"], "title": it["title"], "href": it["href"]} for it in items}
    alerts = []

    if ALERT_MODE == "new_only":
        # alert only for brand-new SKUs that are currently in stock
        for iid, cur in current.items():
            if cur["in_stock"] and iid not in prev:
                alerts.append(cur)

    elif ALERT_MODE == "restock":
        # alert when an existing SKU flips from not-in-stock -> in-stock
        for iid, cur in current.items():
            was = prev.get(iid, {"in_stock": False})
            if cur["in_stock"] and not was.get("in_stock", False):
                alerts.append(cur)

    elif ALERT_MODE == "any_instock":
        # alert every run for whatever is in stock (noisy)
        alerts = [cur for cur in current.values() if cur["in_stock"]]

    # Save latest state
    save_cache(current)

    # Logs
    total = len(items)
    in_now = sum(1 for v in current.values() if v["in_stock"])
    print(f"[debug] products_total={total} in_stock_now={in_now} alerts_to_send={len(alerts)} mode={ALERT_MODE}")

    if VERBOSE:
        for v in list(current.values())[:5]:
            print(f"[debug] sample: {v['title'][:60]} | in_stock={v['in_stock']}")

    if alerts:
        lines = ["🟢 SHEIN stock alert:"]
        for n in alerts:
            lines.append(f"• {n['title']} — In stock\n{n['href']}")
        send_telegram("\n".join(lines))
    else:
        print("No alerts this run.")

if __name__ == "__main__":
    main()
