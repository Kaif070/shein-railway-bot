import json, os, random, re, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

load_dotenv()

SHEIN_URL = os.getenv("SHEIN_URL", "https://www.sheinindia.in/c/sverse-5939-37961")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CACHE_PATH = Path("seen.json")
ALERT_MODE = os.getenv("ALERT_MODE", "restock").lower()
VERBOSE = os.getenv("VERBOSE", "0") == "1"
CHECK_EVERY = int(os.getenv("CHECK_EVERY", "600"))

OOS_PATTERNS = [r"out\s*of\s*stock", r"sold\s*out", r"unavailable", r"notify\s*me"]
ID_PATTERNS = [re.compile(r"/product/(\d+)"), re.compile(r"/p-(\d+)-"), re.compile(r"item/(\d+)")]

# ---------- Telegram ----------
def tg(method, **data):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=data, timeout=20)
        return r.json()
    except Exception as e:
        print("[warn] Telegram:", e)

def send_text(chat, text, reply_markup=None):
    tg("sendMessage", chat_id=chat, text=text,
       disable_web_page_preview=True, reply_markup=reply_markup)

def answer_cbq(cid, text=""): tg("answerCallbackQuery", callback_query_id=cid, text=text)

def menu_markup():
    return {"inline_keyboard":[
        [{"text":"🔎 Check stock","callback_data":"check"},
         {"text":"🔄 Refresh","callback_data":"refresh"}]
    ]}

# ---------- Cache ----------
def load_cache():
    if CACHE_PATH.exists():
        try: return json.loads(CACHE_PATH.read_text())
        except Exception: return {}
    return {}

def save_cache(c): CACHE_PATH.write_text(json.dumps(c, indent=2))

# ---------- Scraper ----------
def is_oos(txt):
    t=(txt or "").lower()
    return any(re.search(p,t) for p in OOS_PATTERNS)

def extract_id(url,title):
    if url:
        p=urlparse(url).path
        for r in ID_PATTERNS:
            m=r.search(p)
            if m: return m.group(1)
        m=re.search(r"(\d{6,})",p)
        if m: return m.group(1)
        return p.strip("/")[:200]
    return (title or "unknown")[:200]

def scrape_once():
    results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(
            headless=True,
            args=["--disable-gpu","--no-sandbox","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
        )
        context=browser.new_context(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
            viewport={"width":1366,"height":900},
        )
        page=context.new_page()
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        page.goto(SHEIN_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_load_state("networkidle", timeout=120000)

        # ---------- deep scroll to load all ----------
        last_height=0
        for _ in range(40):       # up to ~8000 items
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            time.sleep(2)
            new_height=page.evaluate("()=>document.body.scrollHeight")
            if new_height==last_height: break
            last_height=new_height
        time.sleep(3)
        page.screenshot(path="debug.png", full_page=True)
        print("[debug] screenshot saved as debug.png")

        cards=page.locator(
            '[data-sqin="product-card"], .product-card, '
            '.S-product-item, .S-product-item__wrapper, '
            '[class*="product"] article, a[href*="/product/"]'
        ).all()
        print(f"[debug] located {len(cards)} cards")

        seen=set()
        for el in cards:
            try:
                href=el.get_attribute("href")
                if not href:
                    a=el.locator("a").first
                    if a and a.count()>0: href=a.get_attribute("href")
                if href and href.startswith("/"): href=urljoin(SHEIN_URL, href)

                title=None
                for s in ['[title]','.product-title','[class*=\"title\"]','a[title]','a']:
                    try:
                        c=el.locator(s).first
                        if c and c.count()>0:
                            val=c.get_attribute("title") or c.inner_text(timeout=800)
                            if val and len(val.strip())>1:
                                title=" ".join(val.strip().split()); break
                    except Exception: pass

                badge=""
                for s in ['[class*=\"badge\"]','[class*=\"oos\"]','[class*=\"sold\"]',
                          'text=Sold Out','text=Out of stock','[class*=\"stock\"]']:
                    try:
                        b=el.locator(s).first
                        if b and b.count()>0:
                            badge=(b.inner_text(timeout=800) or "").strip()
                            if badge: break
                    except Exception: pass

                all_txt=""
                try: all_txt=el.inner_text(timeout=800)
                except Exception: pass

                in_stock=not is_oos(badge or all_txt)
                pid=extract_id(href,title)
                if pid and pid not in seen:
                    seen.add(pid)
                    results.append({"id":pid,"title":title or "Unknown","href":href or SHEIN_URL,"in_stock":in_stock})
            except Exception: continue

        context.close(); browser.close()
    return results

# ---------- Logic ----------
def summarize_instock(items):
    inst=[i for i in items if i["in_stock"]]
    if not inst: return "No items are in stock right now."
    lines=["🟢 In-stock items:"]
    for i in inst[:30]: lines.append(f"• {i['title']}\n{i['href']}")
    if len(inst)>30: lines.append(f"...and {len(inst)-30} more.")
    return "\n".join(lines)

def decide_alerts(prev,cur):
    alerts=[]
    if ALERT_MODE=="new_only":
        for k,v in cur.items():
            if v["in_stock"] and k not in prev: alerts.append(v)
    elif ALERT_MODE=="restock":
        for k,v in cur.items():
            was=prev.get(k,{"in_stock":False})
            if v["in_stock"] and not was.get("in_stock",False): alerts.append(v)
    elif ALERT_MODE=="any_instock":
        alerts=[v for v in cur.values() if v["in_stock"]]
    return alerts

def run_check():
    items=scrape_once()
    prev=load_cache()
    cur={i["id"]:{k:i[k] for k in("in_stock","title","href")} for i in items}
    alerts=decide_alerts(prev,cur)
    save_cache(cur)
    print(f"[debug] products_total={len(items)} in_stock_now={sum(1 for v in cur.values() if v['in_stock'])} alerts={len(alerts)}")
    if alerts and CHAT_ID:
        lines=["🟢 SHEIN stock alert:"]
        for a in alerts: lines.append(f"• {a['title']} — In stock\n{a['href']}")
        send_text(CHAT_ID,"\n".join(lines),reply_markup=menu_markup())
    return items

# ---------- Telegram ----------
def handle_update(upd):
    if "message" in upd:
        m=upd["message"]; chat=m["chat"]["id"]
        txt=(m.get("text") or "").lower()
        if txt=="/start": send_text(chat,"Welcome! Use buttons below.",reply_markup=menu_markup())
        elif txt in ("/check","/refresh"):
            send_text(chat,"⏳ Checking...")
            items=scrape_once()
            send_text(chat,summarize_instock(items),reply_markup=menu_markup())
    if "callback_query" in upd:
        cq=upd["callback_query"]; cid=cq["id"]; data=cq.get("data","")
        chat=cq["message"]["chat"]["id"]; mid=cq["message"]["message_id"]
        answer_cbq(cid)
        if data in ("check","refresh"):
            tg("editMessageText",chat_id=chat,message_id=mid,text="⏳ Loading latest stock…")
            items=scrape_once()
            tg("editMessageText",chat_id=chat,message_id=mid,
               text=summarize_instock(items),reply_markup=menu_markup())

def poll():
    off=None
    while True:
        try:
            r=requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                           params={"timeout":50, **({"offset":off} if off else {})},timeout=60)
            js=r.json()
            for u in js.get("result",[]):
                off=u["update_id"]+1
                handle_update(u)
        except Exception as e:
            print("[warn] poll error:",e); time.sleep(5)

def main():
    print("[bot] started")
    last=0
    while True:
        if time.time()-last>=CHECK_EVERY:
            try: run_check()
            except Exception as e: print("[warn] run_check:",e)
            last=time.time()
        poll()  # continuous
        time.sleep(2)

if __name__=="__main__": main()
