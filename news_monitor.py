#!/usr/bin/env python3
"""
Thailand Fuel Price News Monitor
Monitors Thai news RSS feeds for fuel price adjustments
Runs every 30 min on GitHub Actions
Sends SMS only when price change detected (Gasohol 95 & E20)
"""

import os
import re
import sqlite3
import hashlib
import requests
import feedparser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dateutil import parser as dateparser

TH_TZ = timezone(timedelta(hours=7))
DB = Path("fuel_news.db")

# ─── SMS Config ───
SMS_API = "https://restapi.easysendsms.app/v1/rest/sms/send"
SENDER = "FuelAlert"

# ─── Target Fuels ───
TARGET_FUELS = {
    "GASOHOL_E20": {"th": "แก๊สโซฮอล E20", "en": "Gasohol E20", "patterns": ["E20", "E 20", "แก๊สโซฮอล E20", "แก๊สโซฮอลE20", "gasohol e20", "gasohol e 20"]},
    "GASOHOL_95":  {"th": "แก๊สโซฮอล 95",  "en": "Gasohol 95",  "patterns": ["95", "แก๊สโซฮอล 95", "แก๊สโซฮอล95", "gasohol 95", "gasohol95"]},
}

# ─── RSS Feeds (Thai fuel price news sources) ───
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

# ─── Keywords for fuel price news detection ───
NEWS_KEYWORDS = [
    r"ปรับราคาน้ำมัน", r"ราคาน้ำมัน", r"น้ำมันลด", r"น้ำมันเพิ่ม",
    r"กองทุนน้ำมัน", r"ออillฟันด์", r"Oil Fund",
    r"ดีเซล", r"Diesel", r"แก๊สโซฮอล", r"Gasohol",
    r"มีผล.*\d{1,2}/\d{1,2}/\d{2,4}", r"มีผล.*\d{1,2}\s*(ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)",
    r"มีผลพรุ่งนี้", r"มีผลวันนี้", r"เวลา\s*\d{1,2}\s*โมงเช้า",
]

COMPILED_KEYWORDS = [re.compile(kw, re.IGNORECASE) for kw in NEWS_KEYWORDS]

# ─── Price extraction patterns ───
# รูปแบบ: "E20: 32.10", "Gasohol 95: 38.50", "E20 32.10 บาท", "95: 40.69"
PRICE_PATTERNS = {
    "GASOHOL_E20": [
        re.compile(r"(?:E20|E\s*20|แก๊สโซฮอล\s*E20|แก๊สโซฮอล\s*E\s*20|gasohol\s*e\s*20)\D*(\d{1,2}\.\d{2})", re.IGNORECASE),
        re.compile(r"(\d{1,2}\.\d{2})\s*บาท.*(?:E20|E\s*20)", re.IGNORECASE),
    ],
    "GASOHOL_95": [
        re.compile(r"(?:Gasohol\s*95|แก๊สโซฮอล\s*95|gasohol\s*95|^95:|^95\s)\D*(\d{1,2}\.\d{2})", re.IGNORECASE),
        re.compile(r"(\d{1,2}\.\d{2})\s*บาท.*(?:95|Gasohol\s*95|แก๊สโซฮอล\s*95)(?!\d)", re.IGNORECASE),
    ],
}

# Change patterns: "+0.75", "-1.50", "ขึ้น 0.75", "ลด 1.50", "เพิ่ม 0.50"
CHANGE_PATTERNS = [
    re.compile(r"[+\-]\s*(\d\.\d{2})\s*บาท", re.IGNORECASE),
    re.compile(r"(?:ขึ้น|เพิ่ม)\s*(\d\.\d{2})\s*บาท", re.IGNORECASE),
    re.compile(r"(?:ลง|ลด)\s*(\d\.\d{2})\s*บาท", re.IGNORECASE),
    re.compile(r"(?:ขึ้น|เพิ่ม)\s*(\d\.\d{2})", re.IGNORECASE),
    re.compile(r"(?:ลง|ลด)\s*(\d\.\d{2})", re.IGNORECASE),
]

# Effective date patterns
EFFECTIVE_DATE_PATTERNS = [
    re.compile(r"มีผล\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.IGNORECASE),
    re.compile(r"มีผล\s*(\d{1,2}\s*(?:ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.)\s*\d{2,4})", re.IGNORECASE),
    re.compile(r"มีผลพรุ่งนี้", re.IGNORECASE),
    re.compile(r"มีผลวันนี้", re.IGNORECASE),
]

THAI_MONTHS = {
    "ม.ค.": 1, "มกราคม": 1,
    "ก.พ.": 2, "กุมภาพันธ์": 2,
    "มี.ค.": 3, "มีนาคม": 3,
    "เม.ย.": 4, "เมษายน": 4,
    "พ.ค.": 5, "พฤษภาคม": 5,
    "มิ.ย.": 6, "มิถุนายน": 6,
    "ก.ค.": 7, "กรกฎาคม": 7,
    "ส.ค.": 8, "สิงหาคม": 8,
    "ก.ย.": 9, "กันยายน": 9,
    "ต.ค.": 10, "ตุลาคม": 10,
    "พ.ย.": 11, "พฤศจิกายน": 11,
    "ธ.ค.": 12, "ธันวาคม": 12,
}

# ─── Database ───
def init_db():
    with sqlite3.connect(DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS processed_articles (
            hash TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            link TEXT NOT NULL,
            published TEXT NOT NULL,
            processed_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS last_price_info (
            fuel TEXT PRIMARY KEY,
            current_price REAL,
            change_amount REAL,
            effective_date TEXT,
            article_hash TEXT,
            updated_at TEXT
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
        if row:
            return {"price": row[0], "change": row[1], "eff_date": row[2], "article_hash": row[3]}
    return None

def save_price_info(fuel: str, price: float, change: float, eff_date: str, article_hash: str):
    with sqlite3.connect(DB) as c:
        c.execute("""INSERT OR REPLACE INTO last_price_info 
                     (fuel, current_price, change_amount, effective_date, article_hash, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (fuel, price, change, eff_date, article_hash, datetime.now(TH_TZ).isoformat()))

def is_duplicate_price_change(fuel: str, price: float, change: float, eff_date: str) -> bool:
    """Check if this exact price change was already alerted"""
    last = get_last_price_info(fuel)
    if not last:
        return False
    return (abs(last["price"] - price) < 0.01 and 
            abs(last["change"] - change) < 0.01 and 
            last["eff_date"] == eff_date)

# ─── Helpers ───
def article_hash(title: str, link: str) -> str:
    return hashlib.sha256(f"{title}|{link}".encode()).hexdigest()[:16]

def is_fuel_news(title: str, summary: str) -> bool:
    text = f"{title} {summary}"
    return any(p.search(text) for p in COMPILED_KEYWORDS)

def parse_effective_date(text: str) -> str | None:
    """Parse Thai effective date from text, return YYYY-MM-DD"""
    for pat in EFFECTIVE_DATE_PATTERNS:
        m = pat.search(text)
        if m:
            date_str = m.group(1) if m.groups() else m.group(0)
            if "พรุ่งนี้" in date_str:
                return (datetime.now(TH_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")
            if "วันนี้" in date_str:
                return datetime.now(TH_TZ).strftime("%Y-%m-%d")
            # Try parse Thai date
            try:
                # Replace Thai month abbreviations
                for th, num in THAI_MONTHS.items():
                    date_str = date_str.replace(th, f"-{num:02d}-")
                # Handle DD-MM-YYYY or DD/MM/YYYY
                date_str = date_str.replace("/", "-")
                parts = date_str.split("-")
                if len(parts) == 3:
                    day, month, year = parts
                    year = int(year)
                    if year < 100:
                        year += 2500 if year < 50 else 1900  # BE to CE
                    return f"{year:04d}-{int(month):02d}-{int(day):02d}"
            except Exception:
                pass
    return None

def extract_price(text: str, fuel_key: str) -> float | None:
    """Extract current price from text for specific fuel"""
    for pat in PRICE_PATTERNS.get(fuel_key, []):
        m = pat.search(text)
        if m:
            try:
                return float(m.group(1))
            except (ValueError, IndexError):
                pass
    return None

def extract_change(text: str, fuel_key: str) -> float | None:
    """Extract price change amount from text near fuel mention"""
    # Find fuel mention position
    fuel_patterns = TARGET_FUELS[fuel_key]["patterns"]
    for fp in fuel_patterns:
        for m in re.finditer(re.escape(fp), text, re.IGNORECASE):
            # Look around fuel mention (±200 chars)
            start = max(0, m.start() - 200)
            end = min(len(text), m.end() + 200)
            context = text[start:end]
            
            # Check change patterns in context
            for cp in CHANGE_PATTERNS:
                cm = cp.search(context)
                if cm:
                    try:
                        change = float(cm.group(1))
                        # Determine sign from Thai words
                        if any(w in context[max(0, cm.start()-10):cm.start()] for w in ["ลง", "ลด", "-"]):
                            return -change
                        if any(w in context[max(0, cm.start()-10):cm.start()] for w in ["ขึ้น", "เพิ่ม", "+"]):
                            return +change
                        # Default: check if explicit sign
                        sign_context = context[max(0, cm.start()-5):cm.start()]
                        if "-" in sign_context:
                            return -change
                        if "+" in sign_context:
                            return +change
                        return change
                    except (ValueError, IndexError):
                        pass
    return None

def parse_article(title: str, summary: str) -> dict | None:
    """Parse article for fuel price changes"""
    text = f"{title} {summary}"
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

# ─── SMS ───
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
    """Build SMS in format: Gasohol 95: 40.69 + 0.75 บาท/L"""
    eff_date = parsed["effective_date"]
    # Format date as DD/MM/YYYY
    try:
        dt = datetime.strptime(eff_date, "%Y-%m-%d")
        eff_str = dt.strftime("%d/%m/%Y")
    except:
        eff_str = eff_date
    
    lines = [f"🚨 ราคาน้ำมันพรุ่งนี้"]
    lines.append(f"มีผล {eff_str} เวลา 05:00 น.")
    lines.append("")
    
    for fuel_key, info in parsed["changes"].items():
        fuel_info = TARGET_FUELS[fuel_key]
        price = info["price"]
        change = info["change"]
        sign = "+" if change >= 0 else ""
        lines.append(f"⛽ {fuel_info['en']}: {price:.2f} {sign}{change:.2f} บาท/L")
    
    lines.append("")
    lines.append(f"🕐 {datetime.now(TH_TZ).strftime('%d/%m/%Y %H:%M')}")
    return "\n".join(lines)

# ─── Main ───
def main() -> int:
    key, phone = os.getenv("EASYSEND_API_KEY"), os.getenv("ALERT_PHONE")
    if not key or not phone:
        print("[ERR] missing secrets"); return 1

    init_db()
    
    total_new_articles = 0
    total_alerts = 0

    for feed_url in RSS_FEEDS:
        print(f"[INFO] Checking {feed_url}")
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:30]:  # Check latest 30 entries
                title = entry.get("title", "")
                summary = entry.get("summary", entry.get("description", ""))
                link = entry.get("link", "")
                published = entry.get("published", entry.get("updated", ""))

                if not is_fuel_news(title, summary):
                    continue

                a_hash = article_hash(title, link)
                if is_processed(a_hash):
                    continue

                # New fuel price article found
                print(f"[NEWS] Found: {title[:80]}...")
                total_new_articles += 1

                parsed = parse_article(title, summary)
                if not parsed:
                    print("[WARN] Could not extract price info")
                    mark_processed(a_hash, title, link, published)
                    continue

                # Check each fuel for duplicate alert
                any_new_alert = False
                for fuel_key, info in parsed["changes"].items():
                    price = info["price"]
                    change = info["change"]
                    eff_date = parsed["effective_date"]

                    if is_duplicate_price_change(fuel_key, price, change, eff_date):
                        print(f"[SKIP] Duplicate alert for {fuel_key}: {price} ({change:+.2f})")
                        continue

                    any_new_alert = True
                    # Save price info
                    save_price_info(fuel_key, price, change, eff_date, a_hash)

                if any_new_alert:
                    # Send SMS
                    msg = build_sms_message(parsed)
                    print(f"[ALERT] Sending SMS:\n{msg}")
                    if send_sms(msg, key, phone):
                        total_alerts += 1
                        print("[OK] SMS sent")
                    else:
                        print("[ERR] SMS failed - will retry next run")
                        # Don't mark processed so we retry
                        continue

                # Mark article as processed
                mark_processed(a_hash, title, link, published)

        except Exception as e:
            print(f"[ERROR] Feed {feed_url} failed: {e}")

    print(f"[DONE] Checked {len(RSS_FEEDS)} feeds, {total_new_articles} new articles, {total_alerts} alerts sent")
    return 0

if __name__ == "__main__":
    exit(main())