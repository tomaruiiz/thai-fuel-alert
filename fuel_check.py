#!/usr/bin/env python3
"""
Thailand Fuel Price Check - All fuels monitored, E20/95 highlighted
Runs 2x/day: 17:30 & 22:00 UTC+7 on GitHub Actions
Uses thai-oil-api (primary) + Bangchak (fallback)
"""

import os
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

TH_TZ = timezone(timedelta(hours=7))
PRIMARY_API = "https://api.chnwt.dev/thai-oil-api/latest"
FALLBACK_API = "https://oil-price.bangchak.co.th/api/v1/oil-price/today"
DB = Path("fuel.db")

TARGET_FUELS = {
    "GASOHOL_E20": ("แก๊สโซฮอล E20", "Gasohol E20"),
    "GASOHOL_95":  ("แก๊สโซฮอล 95",  "Gasohol 95"),
}

THAI_OIL_MAP = {
    "gasohol_e20":  "GASOHOL_E20",
    "gasohol_95":   "GASOHOL_95",
    "gasohol_91":   "GASOHOL_91",
    "gasohol_e85":  "GASOHOL_E85",
    "diesel":       "DIESEL",
    "diesel_b7":    "DIESEL_B7",
    "diesel_b20":   "DIESEL_B20",
    "premium_diesel": "HI_DIESEL",
    "premium_gasohol_95": "HI_PREMIUM_G",
    "gasoline_95":  "GASOLINE_95",
}

FUEL_DISPLAY = {
    "GASOHOL_E20":  ("แก๊สโซฮอล E20",  "Gasohol E20"),
    "GASOHOL_95":   ("แก๊สโซฮอล 95",   "Gasohol 95"),
    "GASOHOL_91":   ("แก๊สโซฮอล 91",   "Gasohol 91"),
    "GASOHOL_E85":  ("แก๊สโซฮอล E85",  "Gasohol E85"),
    "DIESEL":       ("ดีเซล",         "Diesel"),
    "DIESEL_B7":    ("ดีเซล B7",      "Diesel B7"),
    "DIESEL_B20":   ("ดีเซล B20",     "Diesel B20"),
    "HI_DIESEL":    ("ดีเซลพรีเมียม", "Premium Diesel"),
    "HI_PREMIUM_G": ("แก๊สโซฮอล 95 พรีเมียม", "Premium Gasohol 95"),
    "GASOLINE_95":  ("เบนซิน 95",      "Gasoline 95"),
}

SMS_API = "https://restapi.easysendsms.app/v1/rest/sms/send"
SENDER = "FuelAlert"

def init_db():
    with sqlite3.connect(DB) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS prices (
            fuel TEXT PRIMARY KEY, price REAL, updated TEXT, effective TEXT
        )""")

def load_last() -> dict:
    with sqlite3.connect(DB) as c:
        return {r[0]: {"price": r[1], "eff": r[3]} for r in
                c.execute("SELECT fuel, price, updated, effective FROM prices")}

def save_now(prices: dict, eff_date: str):
    now = datetime.now(TH_TZ).isoformat()
    with sqlite3.connect(DB) as c:
        for f, p in prices.items():
            c.execute("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?)",
                      (f, p, now, eff_date))

def fetch_thai_oil_api() -> dict | None:
    try:
        r = requests.get(PRIMARY_API, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "success":
            return None
        stations = data.get("response", {}).get("stations", {})
        out = {}
        for brand, fuels in stations.items():
            for fuel_key, info in fuels.items():
                our_key = THAI_OIL_MAP.get(fuel_key)
                if our_key and our_key not in out:
                    try:
                        out[our_key] = float(info.get("price", 0))
                    except (ValueError, TypeError):
                        pass
        return out if out else None
    except Exception as e:
        print(f"[WARN] thai-oil-api failed: {e}")
        return None

def fetch_bangchak() -> dict | None:
    try:
        r = requests.get(FALLBACK_API, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", {}).get("today", [])
        out = {}
        for item in data:
            name = item.get("name", "").upper()
            price = float(item.get("price", 0))
            if "E20" in name and "GASOHOL" in name:
                out["GASOHOL_E20"] = price
            elif "95" in name and "GASOHOL" in name and "E20" not in name and "E85" not in name:
                out["GASOHOL_95"] = price
            elif "E85" in name and "GASOHOL" in name:
                out["GASOHOL_E85"] = price
            elif "91" in name and "GASOHOL" in name:
                out["GASOHOL_91"] = price
            elif "DIESEL" in name and "B20" in name:
                out["DIESEL_B20"] = price
            elif "DIESEL" in name and "HI" not in name and "PREMIUM" not in name:
                out["DIESEL"] = price
            elif "HI" in name and "DIESEL" in name:
                out["HI_DIESEL"] = price
            elif "PREMIUM" in name and "DIESEL" in name:
                out["HI_DIESEL"] = price
            elif "PREMIUM" in name and "GASOHOL" in name:
                out["HI_PREMIUM_G"] = price
            elif "95" in name and "GASOHOL" not in name and "BENZIN" in name:
                out["GASOLINE_95"] = price
        return out if out else None
    except Exception as e:
        print(f"[WARN] Bangchak API failed: {e}")
        return None

def fetch_all() -> dict | None:
    prices = fetch_thai_oil_api()
    if prices:
        print(f"[INFO] Got prices from thai-oil-api: {len(prices)} fuels")
        return prices
    print("[INFO] Falling back to Bangchak API...")
    prices = fetch_bangchak()
    if prices:
        print(f"[INFO] Got prices from Bangchak: {len(prices)} fuels")
        return prices
    return None

def send_sms(msg: str, key: str, to: str) -> bool:
    try:
        payload = {
            "from": SENDER,
            "to": to,
            "text": msg,
            "type": "0"
        }
        headers = {
            "apikey": key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        r = requests.post(SMS_API, headers=headers, json=payload, timeout=15)
        print(f"[SMS] Status: {r.status_code}, Response: {r.text[:200]}")
        return r.status_code == 200
    except Exception as e:
        print(f"[ERR] sms: {e}")
        return False

def build_detailed_msg(target_changes: dict, other_changes: dict, eff: str) -> str:
    lines = [f"⛽ แจ้งเตือนปรับราคาน้ำมัน (มีผล {eff})"]
    for fuel, (old, new) in target_changes.items():
        th, en = FUEL_DISPLAY.get(fuel, (fuel, fuel))
        diff = new - old
        arrow = "🔺" if diff > 0 else "🔻"
        lines.append(f"{arrow} {th} ({en}): {old:.2f} -> {new:.2f} บาท ({diff:+.2f})")
    if other_changes:
        lines.append("\n📋 น้ำมันอื่นที่เปลี่ยน:")
        for fuel, (old, new) in other_changes.items():
            th, en = FUEL_DISPLAY.get(fuel, (fuel, fuel))
            diff = new - old
            arrow = "🔺" if diff > 0 else "🔻"
            lines.append(f"  {arrow} {th} ({en}): {old:.2f} -> {new:.2f} ({diff:+.2f})")
    lines += [f"\n📊 แหล่งข้อมูล: thai-oil-api / Bangchak",
              f"🕐 {datetime.now(TH_TZ).strftime('%d/%m/%Y %H:%M')}"]
    return "\n".join(lines)

def build_notice_msg(other_changes: dict, eff: str) -> str:
    lines = [f"ℹ️ มีการประกาศปรับราคาน้ำมัน (มีผล {eff})"]
    lines.append("✅ แก๊สโซฮอล E20 และ 95 คงราคาเดิม")
    lines.append("\n📋 น้ำมันที่เปลี่ยน:")
    for fuel, (old, new) in other_changes.items():
        th, en = FUEL_DISPLAY.get(fuel, (fuel, fuel))
        diff = new - old
        arrow = "🔺" if diff > 0 else "🔻"
        lines.append(f"  {arrow} {th} ({en}): {old:.2f} -> {new:.2f} ({diff:+.2f})")
    lines += [f"\n📊 แหล่งข้อมูล: thai-oil-api / Bangchak",
              f"🕐 {datetime.now(TH_TZ).strftime('%d/%m/%Y %H:%M')}"]
    return "\n".join(lines)

def main() -> int:
    key, phone = os.getenv("EASYSEND_API_KEY"), os.getenv("ALERT_PHONE")
    if not key or not phone:
        print("[ERR] missing secrets"); return 1

    init_db()
    last = load_last()
    now_prices = fetch_all()
    if not now_prices:
        print("[ERR] no prices from any source"); return 1

    all_changes = {}
    for fuel, new_price in now_prices.items():
        old = last.get(fuel, {}).get("price")
        if old is not None and abs(old - new_price) > 0.001:
            all_changes[fuel] = (old, new_price)

    # สำคัญ: ถ้าไม่มีการเปลี่ยนแปลงเลย -> ไม่ส่ง SMS, ออกไปเลย
    if not all_changes:
        print("[OK] no price changes anywhere")
        return 0

    target_changes = {f: all_changes[f] for f in TARGET_FUELS if f in all_changes}
    other_changes = {f: all_changes[f] for f in all_changes if f not in TARGET_FUELS}

    eff = (datetime.now(TH_TZ) + timedelta(days=1)).strftime("%Y-%m-%d")

    if target_changes:
        msg = build_detailed_msg(target_changes, other_changes, eff)
        print(f"[ALERT] Target fuels changed\n{msg}")
    else:
        msg = build_notice_msg(other_changes, eff)
        print(f"[NOTICE] Other fuels changed\n{msg}")

    if send_sms(msg, key, phone):
        save_now(now_prices, eff)
        print("[OK] sent & saved")
        return 0
    print("[ERR] send failed")
    return 1

if __name__ == "__main__":
    exit(main())