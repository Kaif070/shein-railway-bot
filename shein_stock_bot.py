import json, os, re, time, random
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

load_dotenv()

# === CONFIG ===
SHEIN_URL   = os.getenv("SHEIN_URL", "https://www.sheinindia.in/c/sverse-5939-37961")
BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID")
CACHE_PATH  = Path("seen.json")
CHECK_EVERY = int(os.getenv("CHECK_EVERY", "600"))
ALERT_MODE  = os.getenv("ALERT_MODE", "new_only").lower()  # new_only | restock | any_instock
VERBOSE     = os.getenv("VERBOSE", "0") == "1"

OOS_PATTERNS = [r"out\s*of\s*stock", r"sold\s*out", r"unavailable", r"notify\s*me"]

# ----------------- Telegram helpers -----------------
def tg(method, **data):
    if not BOT_TOKEN: return {}
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=data, timeout=25)
        return r.json()
    except Exception as e:
        print("[warn] telegram:", e); return {}

def send_text(chat_id, text, reply_markup=None):
    if not BOT_TOKEN or not chat_id: return
    tg("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True, reply_markup=reply_markup)

def answer_cbq(cid, text=""): 
    if cid: tg("answerCallbackQuery", callback_query_id=cid, text=text)

def menu_markup():
    return {"inline_keyboard":[
        [{"text":"🔎 Check stock","callback_data":"check"},
         {"text":"🔄 Refresh","callback_data":"refresh"}]
    ]}

# ----------------- Cache -----------------
def load_cache():
    if CACHE_PATH.exists():
        try: return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}

def save_cache(c): CACHE_PATH.write_text(json.dumps(c, indent=2, ensure_ascii=False), encoding="utf-8")

# ----------------- Utils -----------------
def is_oos(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in OOS_PATTERNS)

def extract_id(href, title):
    if href:
        p = urlparse(href).path
        m = re.search(r"(\d{6,})", p)
        if m: return m.group(1)
        return p.strip("/")[:200]
    return (title or "unknown")[:200]

def summarize_instock(items):
    inst = [i for i in items if i["in_stock"]]
    if not inst:
        return "No items are in stock right now."
    lines = ["🟢 In-stock items:"]
    for i in inst[:30]:
        lines.append(f"• {i['title']}\n{i['href']}")
    if len(inst) > 30:
        lines.append(f"...and {len(inst)-30} more.")
    return "\n".join(lines)

def decide_alerts(prev, cur):
    alerts=[]
    if ALERT_MODE == "new_only":
        for k,v in cur.items():
            if v["in_stock"] and k not in prev: alerts.append(v)
    elif ALERT_MODE == "restock":
        for k,v in cur.items():
            was = prev.get(k, {"in_stock": False})
            if v["in_stock"] and not was.get("in_stock", False): alerts.append(v)
    else:  # any_instock
        alerts = [v for v in cur.values() if v["in_stock"]]
    return alerts

# ----------------- Scraper (JSON-first, DOM-fallback) -----------------
def scrape_once():
    """
    Returns list of dicts: [{id, title, href, in_stock}]
    """
    results = []
    json_products = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox","--disable-gpu","--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process"
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
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        # Pre-set region/currency cookies so the site serves India catalog immediately
        try:
            context.add_cookies([
                {"name":"region","value":"IN","domain":".sheinindia.in","path":"/"},
                {"name":"local_country","value":"IN","domain":".sheinindia.in","path":"/"},
                {"name":"currency","value":"INR","domain":".sheinindia.in","path":"/"},
            ])
        except Exception: pass

        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        # Capture JSON data requests the SPA makes
        api_json_blobs = []
        def on_response(resp):
            ct = (resp.headers.get("content-type") or "").lower()
            url = resp.url
            # many shein category/search endpoints include 'list', 'goods', 'search', 'products'
            if "application/json" in ct and any(k in url for k in ["list","goods","search","product"]):
                try:
                    data = resp.json()
                    api_json_blobs.append({"url": url, "json": data})
                except:
                    pass
        page.on("response", on_response)

        page.goto(SHEIN_URL, wait_until="domcontentloaded", timeout=90000)

        # Try to close/accept cookie or region gate if shown
        for sel in [
            'button:has-text("Accept")', 'button:has-text("I agree")',
            'button:has-text("Agree")', 'button:has-text("OK")',
            '[data-testid="accept-all"]', '.c-cookie-accept', '.cookie-accept',
        ]:
            try:
                if page.locator(sel).first.count() > 0:
                    page.locator(sel).first.click(timeout=1000)
                    break
            except: pass

        # Wait for network calm then scroll to trigger more requests
        page.wait_for_load_state("networkidle", timeout=120000)

        last_h = 0
        for _ in range(24):   # ~ deep enough, API path triggers early anyway
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            time.sleep(1.2)
            h = page.evaluate("() => document.body.scrollHeight")
            if h == last_h: break
            last_h = h

        # If we caught any JSON, parse it for products
        def harvest_from_json():
            items=[]
            for blob in api_json_blobs:
                data = blob["json"]
                # look for common fields: 'goods', 'products', 'list', 'items'
                for key in ["goods","products","list","items","data"]:
                    arr = data.get(key) if isinstance(data, dict) else None
                    if isinstance(arr, list) and arr:
                        for it in arr:
                            title = str(it.get("goods_name") or it.get("title") or it.get("name") or "Unknown").strip()
                            href  = it.get("goods_url") or it.get("url") or it.get("detail_url")
                            gid   = str(it.get("goods_id") or it.get("id") or extract_id(href, title))
                            # Try stock signals
                            in_stock = True
                            if isinstance(it.get("sold_out"), bool):
                                in_stock = not it["sold_out"]
                            elif "stock" in it and isinstance(it["stock"], int):
                                in_stock = it["stock"] > 0
                            elif "status" in it and isinstance(it["status"], str):
                                in_stock = not is_oos(it["status"])
                            items.append({"id": gid, "title": title, "href": href or SHEIN_URL, "in_stock": in_stock})
                        # don’t double-count
            return items

        json_products = harvest_from_json()

        # Fallback to DOM if JSON wasn’t captured (or empty)
        if not json_products:
            cards = page.locator(
                '[data-sqin="product-card"], .product-card, .S-product-item, '
                '.S-product-item__wrapper, [class*="product"] article, a[href*="/product/"]'
            ).all()

            seen=set()
            for el in cards:
                try:
                    href = el.get_attribute("href")
                    if not href:
                        a = el.locator("a").first
                        if a and a.count()>0:
                            href = a.get_attribute("href")
                    if href and href.startswith("/"):
                        href = urljoin(SHEIN_URL, href)

                    title=None
                    for s in ['[title]','.product-title','[class*="title"]','a[title]','a']:
                        try:
                            c=el.locator(s).first
                            if c and c.count()>0:
                                val = c.get_attribute("title") or c.inner_text(timeout=800)
                                if val and len(val.strip())>1:
                                    title=" ".join(val.strip().split()); break
                        except: pass

                    all_txt=""
                    try: all_txt = el.inner_text(timeout=800)
                    except: pass

                    pid = extract_id(href, title)
                    if pid and pid not in seen:
                        seen.add(pid)
                        json_products.append({
                            "id": pid,
                            "title": title or "Unknown",
                            "href": href or SHEIN_URL,
                            "in_stock": not is_oos(all_txt),
                        })
                except: 
                    continue

        if VERBOSE:
            page.screenshot(path="debug.png", full_page=True)
            print(f"[debug] saved debug.png  blobs={len(api_json_blobs)} products={len(json_products)}")

        context.close(); browser.close()

    # normalize hrefs
    final=[]
    for it in json_products:
        href = it.get("href")
        if href and href.startswith("/"):
            href = urljoin(SHEIN_URL, href)
        final.append({"id": it["id"], "title": it["title"], "href": href or SHEIN_URL, "in_stock": bool(it["in_stock"])})

    return final

# ----------------- Periodic + Alerts -----------------
def run_check():
    items = scrape_once()
    prev  = load_cache()
    cur   = {i["id"]: {"in_stock": i["in_stock"], "title": i["title"], "href": i["href"]} for i in items}
    alerts = decide_alerts(prev, cur)
    save_cache(cur)

    if VERBOSE:
        print(f"[debug] total={len(items)} instock={sum(1 for v in cur.values() if v['in_stock'])} alerts={len(alerts)}")

    if alerts and CHAT_ID:
        lines = ["🟢 SHEIN stock alert:"]
        for a in alerts:
            lines.append(f"• {a['title']} — In stock\n{a['href']}")
        send_text(CHAT_ID, "\n".join(lines), reply_markup=menu_markup())
    return items

# ----------------- Telegram bot (polling + buttons) -----------------
def handle_update(upd):
    if "message" in upd:
        m   = upd["message"]
        chat= m["chat"]["id"]
        txt = (m.get("text") or "").strip().lower()
        if txt == "/start":
            send_text(chat, "Welcome! Use the buttons below to check stock or refresh.", reply_markup=menu_markup())
        elif txt in ("/check","/refresh"):
            send_text(chat, "⏳ Checking current stock…")
            items = scrape_once()
            send_text(chat, summarize_instock(items), reply_markup=menu_markup())

    if "callback_query" in upd:
        cq   = upd["callback_query"]
        cid  = cq["id"]
        data = cq.get("data","")
        chat = cq["message"]["chat"]["id"]
        mid  = cq["message"]["message_id"]
        answer_cbq(cid)
        if data in ("check","refresh"):
            tg("editMessageText", chat_id=chat, message_id=mid, text="⏳ Loading latest stock…")
            items = scrape_once()
            tg("editMessageText", chat_id=chat, message_id=mid,
               text=summarize_instock(items), reply_markup=menu_markup())

def poll_loop():
    offset=None
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                params={"timeout": 50, **({"offset": offset} if offset else {})},
                timeout=60
            )
            js = r.json()
            for u in js.get("result", []):
                offset = u["update_id"] + 1
                handle_update(u)
        except Exception as e:
            print("[warn] poll:", e)
            time.sleep(3)

def main():
    print("[bot] started")
    last = 0
    while True:
        try:
            if time.time() - last >= CHECK_EVERY:
                run_check()
                last = time.time()
            poll_loop()
        except Exception as e:
            print("[warn] main:", e)
            time.sleep(3)

if __name__ == "__main__":
    main()
