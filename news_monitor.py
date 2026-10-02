#!/usr/bin/env python3
"""
Thailand Fuel Price News Monitor - Production
Monitors Thai news RSS feeds + API fallback for fuel price adjustments
Runs every 30 min on GitHub Actions
"""

import os
import re
import sqlite3
import hashlib
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from pathlib import Path

TH_TZ = timezone(timedelta(hours=7))
DB = Path("fuel_news.db")

SMS_API = "https://restapi.easysendsms.app/v1/rest/sms/send"
SENDER = "FuelAlert"

TARGET_FUELS = {
    "GASOHOL_E20": {"th": "แก๊สโซฮอล E20", "en": "Gasohol E20", "patterns": ["E20", "E 20", "แก๊สโซฮอล E20", "แก๊สโซฮอลE20", "gasohol e20", "gasohol e 20"]},
    "GASOHOL_95":  {"th": "แก๊สโซฮอล 95",  "en": "Gasohol 95",  "patterns": ["95", "แก๊สโซฮอล 95", "แก๊สโซฮอล95", "gasohol 95", "gasohol95"]},
}

RSS_FEEDS = [
    "https://www.thairath.co.th/rss/money.xml",
    "https://www.bangkokbiznews.com/rss/economy.xml",
    "https://www.js100.com/rss/economy.xml",
    "https://rss.sanook.com/rss/economy.xml",
    "https://www.matichon.co.th/rss/economy.xml",
    "https://www.bangkokpost.com/rss/data/business.xml",
    "https://www.pttor.com/th/news/rss",
    "https://www.bangchak.co.th/th/news/rss",
    "https://www.eppo.go.th/rss.xml",
]

# SPECIFIC keywords for Thai fuel price adjustment announcements
NEWS_KEYWORDS = [
    # Must-have: price adjustment terms
    r"ปรับราคาน้ำมัน", r"ประกาศราคาน้ำมัน", r"ราคาน้ำมัน.*(?:ลด|เพิ่ม|ขึ้น|ลง)",
    r"กองทุนน้ำมัน.*(?:ประกาศ|ปรับ)", r"ออillฟันด์.*(?:ประกาศ|ปรับ)", 
    r"Oil Fund.*(?:announc|adjust)", r"Fuel Fund.*(?:announc|adjust)",
    # Specific fuel types with price context
    r"(?:แก๊สโซฮอล|Gasohol|ดีเซล|Diesel|เบนซิน|Gasoline).*(?:ลด|เพิ่ม|ขึ้น|ลง|ปรับ).*\d",
    r"(?:E20|E85|91|95|B20|B7).*(?:ลด|เพิ่ม|ขึ้น|ลง|ปรับ).*\d",
    r"(?:ลด|เพิ่ม|ขึ้น|ลง).*\d\.\d{2}.*(?:บาท|ลิตร)",
    # Effective date patterns
    r"มีผล.*\d{1,2}/\d{1,2}/\d{2,4}", r"มีผล.*\d{1,2}\s*(?:ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)",
    r"มีผลพรุ่งนี้", r"มีผลวันนี้", r"เวลา\s*\d{1,2}\s*โมงเช้า",
    # Official sources
    r"PTT\s*OR.*(?:ประกาศ|ปรับ)", r"PTTOR.*(?:ประกาศ|ปรับ)", r"Bangchak.*(?:ประกาศ|ปรับ)",
]

COMPILED_KEYWORDS = [re.compile(kw, re.IGNORECASE) for kw in NEWS_KEYWORDS]

# Price patterns
PRICE_PATTERNS = {
    "GASOHOL_E20": [
        re.compile(r"(?:E20|E\s*20|แก๊สโซฮอล\s*E\s*20|gasohol\s*e\s*20)[^\d]*(\d{1,2}\.\d{2})", re.IGNORECASE),
        re.compile(r"(\d{1,2}\.\d{2})[^\d]*(?:E20|E\s*20|แก๊สโซฮอล\s*E\s*20)", re.IGNORECASE),
    ],
    "GASOHOL_95": [
        re.compile(r"(?:Gasohol\s*95|แก๊สโซฮอล\s*95|gasohol\s*95|^95[\s:])[^\d]*(\d{1,2}\.\d{2})", re.IGNORECASE),
        re.compile(r"(\d{1,2}\.\d{2})[^\d]*(?:Gasohol\s*95|แก๊สโซฮอล\s*95)(?!\d)", re.IGNORECASE),
    ],
}

CHANGE_PATTERNS = [
    re.compile(r"[+\-]\s*(\d\.\d{2})\s*บาท", re.IGNORECASE),
    re.compile(r"(?:ขึ้น|เพิ่ม)\s*(\d\.\d{2})\s*บาท", re.IGNORECASE),
    re.compile(r"(?:ลง|ลด)\s*(\d\.\d{2})\s*บาท", re.IGNORECASE),
    re.compile(r"(?:ขึ้น|เพิ่ม)\s*(\d\.\d{2})", re.IGNORECASE),
    re.compile(r"(?:ลง|ลด)\s*(\d\.\d{2})", re.IGNORECASE),
]

EFFECTIVE_DATE_PATTERNS = [
    re.compile(r"มีผล\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE),
    re.compile(r"มีผล\s*(\d{1,2}\s*(?:ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)\s*\d{2,4})", re.IGNORECASE),
    re.compile(r"มีผลพรุ่งนี้", re.IGNORECASE),
    re.compile(r"มีผลวันนี้", re.IGNORECASE),
]

THAI_MONTHS = {
    "ม.ค.": 1, "มกราคม": 1, "ก.พ.": 2, "กุมภาพันธ์": 2, "มี.ค.": 3, "มีนาคม": 3,
    "เม.ย.": 4, "เมษายน": 4, "พ.ค.": 5, "พฤษภาคม": 5, "มิ.ย.": 6, "มิถุนายน": 6,
    "ก.ค.": 7, "กรกฎาคม": 7, "ส.ค.": 8, "สิงหาคม": 8, "ก.ย.": 9, "กันยายน": 9,
    "ต.ค.": 10, "ตุลาคม": 10, "พ.ย.": 11, "พฤศจิกายน": 11, "ธ.ค.": 12, "ธันวาคม": 12,
}

# API Fallback - check actual prices from thai-oil-api
API_URL = "https://api.chnwt.dev/thai-oil-api/latest"

def init_db():
    with sqlite3.connect(DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS processed_articles (
            hash TEXT PRIMARY KEY, title TEXT, link TEXT, published TEXT, processed_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS last_price_info (
            fuel TEXT PRIMARY KEY, current_price REAL, change_amount REAL, 
            effective_date TEXT, article_hash TEXT, updated_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS last_api_prices (
            fuel TEXT PRIMARY KEY, price REAL, updated_at TEXT
        )""")

def is_processed(article_hash: str) -> bool:
    with sqlite3.connect(DB) as c:
        return c.execute("SELECT 1 FROM processed_articles WHERE hash = ?", (article_hash,)).fetchone() is not None

def mark_processed(article_hash: str, title: str, link: str, published: str):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR IGNORE INTO processed_articles VALUES (?, ?, ?, ?, ?)",
                  (article_hash, title, link, published, datetime.now(TH_TZ).isoformat()))

def get_last_price_info(fuel: str) -> dict | None:
    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT current_price, change_amount, effective_date, article_hash FROM last_price_info WHERE fuel = ?", (fuel,)).fetchone()
        if row: return {"price": row[0], "change": row[1], "eff_date": row[2], "article_hash": row[3]}
    return None

def save_price_info(fuel: str, price: float, change: float, eff_date: str, article_hash: str):
    with sqlite3.connect(DB) as c:
        c.execute("""INSERT OR REPLACE INTO last_price_info 
                     (fuel, current_price, change_amount, effective_date, article_hash, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (fuel, price, change, eff_date, article_hash, datetime.now(TH_TZ).isoformat()))

def is_duplicate_price_change(fuel: str, price: float, change: float, eff_date: str) -> bool:
    last = get_last_price_info(fuel)
    if not last: return False
    return (abs(last["price"] - price) < 0.01 and abs(last["change"] - change) < 0.01 and last["eff_date"] == eff_date)

def get_last_api_price(fuel: str) -> float | None:
    with sqlite3.connect(DB) as c:
        row = c.execute("SELECT price FROM last_api_prices WHERE fuel = ?", (fuel,)).fetchone()
        return row[0] if row else None

def save_api_price(fuel: str, price: float):
    with sqlite3.connect(DB) as c:
        c.execute("INSERT OR REPLACE INTO last_api_prices (fuel, price, updated_at) VALUES (?, ?, ?)",
                  (fuel, price, datetime.now(TH_TZ).isoformat()))

def article_hash(title: str, link: str) -> str:
    return hashlib.sha256(f"{title}|{link}".encode()).hexdigest()[:16]

def is_fuel_news(title: str, summary: str) -> bool:
    """Strict filter for fuel price adjustment news"""
    text = f"{title} {summary}"
    # Must match at least one specific keyword
    return any(p.search(text) for p in COMPILED_KEYWORDS)

def fetch_article_content(url: str) -> str:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; FuelMonitor/1.0)"}
        r = requests.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        from html.parser import HTMLParser
        class TextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.text = []
                self.skip = False
            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "nav", "header", "footer", "aside"):
                    self.skip = True
            def handle_endtag(self, tag):
                if tag in ("script", "style", "nav", "header", "footer", "aside"):
                    self.skip = False
            def handle_data(self, data):
                if not self.skip:
                    self.text.append(data)
        extractor = TextExtractor()
        extractor.feed(r.text)
        return " ".join(extractor.text)[:5000]
    except Exception as e:
        print(f"[WARN] Failed to fetch {url}: {e}")
        return ""

def parse_effective_date(text: str) -> str | None:
    for pat in EFFECTIVE_DATE_PATTERNS:
        m = pat.search(text)
        if m:
            date_str = m.group(1) if m.groups() else m.group(0)
            if "พรุ่งนี้" in date_str:
                return (datetime.now(TH_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
            if "วันนี้" in date_str:
                return datetime.now(TH_TZ).strftime("%Y-%m-%d")
            try:
                for th, num in THAI_MONTHS.items():
                    date_str = date_str.replace(th, f"-{num:02d}-")
                date_str = date_str.replace("/", "-")
                parts = date_str.split("-")
                if len(parts) == 3:
                    day, month, year = parts
                    year = int(year)
                    if year < 100: year += 2500 if year < 50 else 1900
                    return f"{year:04d}-{int(month):02d}-{int(day):02d}"
            except: pass
    return None

def extract_price(text: str, fuel_key: str) -> float | None:
    for pat in PRICE_PATTERNS.get(fuel_key, []):
        m = pat.search(text)
        if m:
            try: return float(m.group(1))
            except: pass
    return None

def extract_change(text: str, fuel_key: str) -> float | None:
    fuel_patterns = TARGET_FUELS[fuel_key]["patterns"]
    for fp in fuel_patterns:
        for m in re.finditer(re.escape(fp), text, re.IGNORECASE):
            start = max(0, m.start() - 300)
            end = min(len(text), m.end() + 300)
            context = text[start:end]
            for cp in CHANGE_PATTERNS:
                cm = cp.search(context)
                if cm:
                    try:
                        change = float(cm.group(1))
                        before = context[max(0, cm.start()-20):cm.start()]
                        if any(w in before for w in ["ลง", "ลด", "-", "decrease", "drop", "down"]):
                            return -change
                        if any(w in before for w in ["ขึ้น", "เพิ่ม", "+", "increase", "rise", "up"]):
                            return +change
                        if cm.group(0).strip().startswith("-"): return -change
                        if cm.group(0).strip().startswith("+"): return +change
                        return change
                    except: pass
    return None

def parse_article(title: str, summary: str, link: str) -> dict | None:
    text = f"{title} {summary}"
    if len(text) < 200:
        content = fetch_article_content(link)
        if content:
            text = f"{title} {summary} {content}"
    
    eff_date = parse_effective_date(text)
    results = {}
    for fuel_key in TARGET_FUELS:
        price = extract_price(text, fuel_key)
        change = extract_change(text, fuel_key)
        if price is not None and change is not None:
            results[fuel_key] = {"price": price, "change": change}
    
    if not results:
        return None
    return {"effective_date": eff_date or (datetime.now(TH_TZ) + timedelta(days=1)).strftime("%Y-%m-%d"),
            "changes": results}

# ─── API Fallback ───
def check_api_prices() -> dict | None:
    """Check thai-oil-api for actual price changes"""
    try:
        r = requests.get(API_URL, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "success":
            return None
        stations = data.get("response", {}).get("stations", {})
        out = {}
        for brand, fuels in stations.items():
            for fuel_key, info in fuels.items():
                our_key = {"gasohol_e20": "GASOHOL_E20", "gasohol_95": "GASOHOL_95"}.get(fuel_key)
                if our_key and our_key not in out:
                    try:
                        out[our_key] = float(info.get("price", 0))
                    except: pass
        return out if out else None
    except Exception as e:
        print(f"[WARN] API check failed: {e}")
        return None

def send_sms(msg: str, key: str, to: str) -> bool:
    try:
        payload = {"from": SENDER, "to": to, "text": msg, "type": "0"}
        headers = {"apikey": key, "Content-Type": "application/json", "Accept": "application/json"}
        r = requests.post(SMS_API, headers=headers, json=payload, timeout=15)
        print(f"[SMS] Status: {r.status_code}, Response: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"[ERR] sms: {e}")
        return False

def build_sms_message(parsed: dict) -> str:
    eff_date = parsed["effective_date"]
    try:
        dt = datetime.strptime(eff_date, "%Y-%m-%d")
        eff_str = dt.strftime("%d/%m/%Y")
    except: eff_str = eff_date
    lines = [f"🚨 ราคาน้ำมันพรุ่งนี้", f"มีผล {eff_str} เวลา 05:00 น.", ""]
    for fuel_key, info in parsed["changes"].items():
        fuel_info = TARGET_FUELS[fuel_key]
        price = info["price"]
        change = info["change"]
        sign = "+" if change >= 0 else ""
        lines.append(f"⛽ {fuel_info['en']}: {price:.2f} {sign}{change:.2f} บาท/L")
    lines.append(""), lines.append(f"🕐 {datetime.now(TH_TZ).strftime('%d/%m/%Y %H:%M')}")
    return "\n".join(lines)

def build_api_sms_message(changes: dict) -> str:
    """Build SMS from API price changes"""
    eff = (datetime.now(TH_TZ) + timedelta(days=1)).strftime("%d/%m/%Y")
    lines = [f"🚨 ราคาน้ำมันพรุ่งนี้ (จาก API)", f"มีผล {eff} เวลา 05:00 น.", ""]
    for fuel_key, (old, new) in changes.items():
        fuel_info = TARGET_FUELS[fuel_key]
        diff = new - old
        sign = "+" if diff >= 0 else ""
        lines.append(f"⛽ {fuel_info['en']}: {new:.2f} {sign}{diff:.2f} บาท/L")
    lines.append(""), lines.append(f"🕐 {datetime.now(TH_TZ).strftime('%d/%m/%Y %H:%M')}")
    return "\n".join(lines)

def main() -> int:
    key, phone = os.getenv("EASYSEND_API_KEY"), os.getenv("ALERT_PHONE")
    if not key or not phone: print("[ERR] missing secrets"); return 1

    init_db()
    total_new = 0
    total_alerts = 0

    # ─── 1. News Monitoring ───
    for feed_url in RSS_FEEDS:
        print(f"[INFO] Checking {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:100]:
                title = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))
                link = entry.get("link", "")
                published = entry.get("published", entry.get("updated", ""))

                if not is_fuel_news(title, summary):
                    continue

                a_hash = article_hash(title, link)
                if is_processed(a_hash):
                    continue

                print(f"[NEWS] Found: {title[:100]}")
                total_new += 1

                parsed = parse_article(title, summary, link)
                if not parsed:
                    print("[WARN] Could not extract price info")
                    mark_processed(a_hash, title, link, published)
                    continue

                any_new = False
                for fuel_key, info in parsed["changes"].items():
                    price, change, eff_date = info["price"], info["change"], parsed["effective_date"]
                    if is_duplicate_price_change(fuel_key, price, change, eff_date):
                        print(f"[SKIP] Duplicate: {fuel_key} {price} ({change:+.2f})")
                        continue
                    any_new = True
                    save_price_info(fuel_key, price, change, eff_date, a_hash)

                if any_new:
                    msg = build_sms_message(parsed)
                    print(f"[ALERT] Sending SMS:\n{msg}")
                    if send_sms(msg, key, phone):
                        total_alerts += 1
                        print("[OK] SMS sent")
                    else:
                        print("[ERR] SMS failed")
                        continue

                mark_processed(a_hash, title, link, published)

        except Exception as e:
            print(f"[ERROR] Feed {feed_url}: {e}")

    # ─── 2. API Fallback Check (always run) ───
    print("[INFO] Checking API prices as fallback...")
    api_prices = check_api_prices()
    if api_prices:
        api_changes = {}
        for fuel_key in TARGET_FUELS:
            new_price = api_prices.get(fuel_key)
            old_price = get_last_api_price(fuel_key)
            if new_price and old_price and abs(new_price - old_price) > 0.001:
                api_changes[fuel_key] = (old_price, new_price)
                print(f"[API] Price change: {fuel_key} {old_price:.2f} -> {new_price:.2f}")

        if api_changes:
            # Check if already alerted via news
            already_alerted = False
            for fuel_key, (old, new) in api_changes.items():
                last = get_last_price_info(fuel_key)
                if last and abs(last["price"] - new) < 0.01 and abs(last["change"] - (new-old)) < 0.01:
                    already_alerted = True
                    break
            
            if not already_alerted:
                for fuel_key, (old, new) in api_changes.items():
                    save_price_info(fuel_key, new, new-old, 
                                   (datetime.now(TH_TZ) + timedelta(days=1)).strftime("%Y-%m-%d"),
                                   "api_fallback")
                    save_api_price(fuel_key, new)
                
                msg = build_api_sms_message(api_changes)
                print(f"[API ALERT] Sending SMS:\n{msg}")
                if send_sms(msg, key, phone):
                    total_alerts += 1
                    print("[OK] API SMS sent")
                else:
                    print("[ERR] API SMS failed")
            else:
                print("[SKIP] API changes already alerted via news")
                for fuel_key, (old, new) in api_changes.items():
                    save_api_price(fuel_key, new)
        else:
            print("[API] No price changes detected")
            for fuel_key, price in api_prices.items():
                if fuel_key in TARGET_FUELS:
                    save_api_price(fuel_key, price)
    else:
        print("[API] Could not fetch prices")

    print(f"[DONE] News: {total_new} new articles, {total_alerts} alerts sent")
    return 0

if __name__ == "__main__":
    exit(main())