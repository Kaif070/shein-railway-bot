# shein_stock_bot.py
import json, os, random, re, time, sys
from pathlib import Path
from urllib.parse import urljoin
from typing import Optional, Tuple

import requests
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

# === CONFIG ===
SHEIN_URL = os.getenv("SHEIN_URL", "https://www.sheinindia.in/c/sverse-5939-37961")

# Telegram (optional alerting)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Cache of seen items
CACHE_PATH = Path("seen.json")
ALERT_MODE = os.getenv("ALERT_MODE", "new_only")

# Proxy env (paid/residential recommended)
PROXY_SERVER = os.getenv("PROXY_SERVER")  # e.g. http://host:port
PROXY_USERNAME = os.getenv("PROXY_USERNAME")
PROXY_PASSWORD = os.getenv("PROXY_PASSWORD")

# Free proxy source (user provided)
FREE_PROXY_URL = (
    "https://api.proxyscrape.com/v4/free-proxy-list/get"
    "?request=display_proxies&country=in&proxy_format=protocolipport&format=text&timeout=20000"
)

# Keywords that usually mean "out of stock"
OOS_PATTERNS = [r"out\s*of\s*stock", r"sold\s*out", r"unavailable", r"notify\s*me"]


def log(*a):
    print("[bot]", *a, flush=True)


def send_telegram(text: str):
    if not BOT_TOKEN or not CHAT_ID:
        log("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    try:
        requests.post(url, data=payload, timeout=15)
    except Exception as e:
        log("Telegram error:", e)


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


# ---------- Proxy helpers ----------
def fetch_free_proxies(max_take: int = 100) -> list[str]:
    """Fetch a list of candidate IN proxies from the free proxy API."""
    try:
        r = requests.get(FREE_PROXY_URL, timeout=15)
        r.raise_for_status()
        lines = [ln.strip() for ln in r.text.splitlines() if ln.strip()]
        # normalize (ensure protocol)
        out = []
        for ln in lines:
            if not re.match(r"^[a-z]+://", ln):
                ln = "http://" + ln
            out.append(ln)
        random.shuffle(out)
        return out[:max_take]
    except Exception as e:
        log("free-proxy fetch failed:", e)
        return []


def proxy_tuple_for_playwright(proxy_url: str) -> dict:
    d = {"server": proxy_url}
    # No creds for free proxies; add env creds if using your paid proxy
    if PROXY_USERNAME and PROXY_PASSWORD and PROXY_SERVER and proxy_url.startswith(PROXY_SERVER):
        d["username"] = PROXY_USERNAME
        d["password"] = PROXY_PASSWORD
    return d


def looks_blocked(html: str) -> bool:
    h = (html or "").lower()
    return ("access denied" in h) or ("akamai" in h and "denied" in h)


def test_proxy_can_open_shein(proxy_url: str) -> bool:
    """Quick preflight check using requests through the proxy."""
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        headers = {
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
            "Accept-Language": "en-IN,en;q=0.9",
        }
        r = requests.get("https://www.sheinindia.in/", proxies=proxies, headers=headers,
                         timeout=12, allow_redirects=True)
        if r.status_code >= 400:
            return False
        if looks_blocked(r.text):
            return False
        return True
    except Exception:
        return False


def get_working_proxy() -> Optional[str]:
    """Prefer an env-set paid proxy; otherwise try free list and return the first that passes."""
    # Paid/residential proxy via env
    if PROXY_SERVER:
        env_proxy = PROXY_SERVER
        if not re.match(r"^[a-z]+://", env_proxy):
            env_proxy = "http://" + env_proxy
        ok = test_proxy_can_open_shein(env_proxy)
        log("env proxy test:", env_proxy, "=>", "OK" if ok else "BLOCKED")
        if ok:
            return env_proxy
        # fall through to free list if env proxy blocked

    # Free proxies (very unreliable)
    cands = fetch_free_proxies(max_take=60)
    log(f"testing {len(cands)} free proxies…")
    for i, pxy in enumerate(cands, 1):
        if test_proxy_can_open_shein(pxy):
            log(f"free proxy #{i} works:", pxy)
            return pxy
        if i % 5 == 0:
            log(f"…tried {i}, none worked yet")
    return None


# ---------- Scraper ----------
def scrape_once():
    """
    Returns list of dicts:
    [
      {"id": "...", "title": "...", "href": "...", "oos": True/False}
    ]
    """
    results = []
    proxy_url = get_working_proxy()
    if not proxy_url:
        log("No working proxy found (Shein likely blocking datacenter IPs).")
    else:
        log("Using proxy:", proxy_url)

    with sync_playwright() as p:
        launch_kwargs = dict(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        if proxy_url:
            launch_kwargs["proxy"] = proxy_tuple_for_playwright(proxy_url)

        browser = p.chromium.launch(**launch_kwargs)

        context = browser.new_context(
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )

        # Light stealth
        context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-IN','en']});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        """)
        # Skip heavy assets
        context.route("**/*", lambda route: route.abort()
                      if route.request.resource_type in ["image", "media", "font"] else route.continue_())

        page = context.new_page()

        # Go!
        try:
            time.sleep(random.uniform(1.0, 2.0))
            page.goto(SHEIN_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_load_state("networkidle", timeout=90000)

            # If we still hit an “Access Denied”, dump HTML and screenshot
            html = page.content()
            if looks_blocked(html):
                page.screenshot(path="debug.png", full_page=True)
                log("Access Denied in Playwright too. Saved debug.png")
                context.close()
                browser.close()
                return []

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
        finally:
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
            # only alert for brand-new items that are currently in stock
            if prev is None and it["oos"] is False:
                notifications.append(it)
        else:
            if it["oos"] is False and (prev is None or (prev and prev.get("oos") is True)):
                notifications.append(it)

        cache[it["id"]] = {"oos": it["oos"], "title": it["title"], "href": it["href"]}

    save_cache(cache)

    log(f"[debug] total={len(items)} instock={sum(1 for i in items if not i['oos'])} alerts={len(notifications)}")

    if notifications:
        lines = ["🟢 SHEIN new stock alert:"]
        for n in notifications:
            lines.append(f"• {n['title']} — In stock\n{n['href']}")
        send_telegram("\n".join(lines))
    else:
        log("No new in-stock items.")


if __name__ == "__main__":
    main()

