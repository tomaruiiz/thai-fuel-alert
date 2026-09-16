#!/usr/bin/env python3
"""
Thailand Fuel Price Check - All fuels monitored, E20/95 highlighted
Runs 2x/day: 17:30 & 22:00 UTC+7 on GitHub Actions
"""

import os
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

TH_TZ = timezone(timedelta(hours=7))
API_URL = "https://api.bangchak.co.th/api/v1/oil-price/today"
DB = Path("fuel.db")

TARGET_FUELS = {
    "GASOHOL_E20": ("แก๊สโซฮอล E20", "Gasohol E20"),
    "GASOHOL_95":  ("แก๊สโซฮอล 95",  "Gasohol 95"),
}

FUEL_PATTERNS = {
    "GASOHOL_E85":  ("E85", "Gasohol E85"),
    "GASOHOL_E20":  ("E20", "Gasohol E20"),
    "GASOHOL_91":   ("91", "Gasohol 91"),
    "GASOHOL_95":   ("95", "Gasohol 95"),
    "GASOLINE_95":  ("95", "Gasoline 95"),
    "DIESEL_B20":   ("B20", "Diesel B20"),
    "DIESEL":       ("DSL", "Diesel"),
    "HI_DIESEL":    ("HiD", "Hi Diesel"),
    "HI_PREMIUM_D": ("HiP", "Hi Premium Diesel"),
    "HI_PREMIUM_G": ("HiP", "Hi Premium Gasohol"),
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

def fetch_all() -> dict | None:
    try:
        r = requests.get(API_URL, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", {}).get("today", [])
        out = {}
        for item in data:
            name = item.get("name", "").upper()
            price = float(item.get("price", 0))
            for key, (pat, _) in FUEL_PATTERNS.items():
                if pat in name and key not in out:
                    out[key] = price
                    break
        return out if out else None
    except Exception as e:
        print(f"[ERR] fetch: {e}")
        return None

def send(msg: str, key: str, to: str) -> bool:
    try:
        r = requests.post(SMS_API, headers={"apikey": key, "Content-Type": "application/json"},
                          json={"from": SENDER, "to": to, "text": msg, "type": "0"}, timeout=15)
        return r.status_code == 200
    except Exception as e:
        print(f"[ERR] sms: {e}")
        return False

def build_detailed_msg(target_changes: dict, other_changes: dict, eff: str) -> str:
    lines = [f"⛽ แจ้งเตือนปรับราคาน้ำมัน (มีผล {eff})"]
    for fuel, (old, new) in target_changes.items():
        th, en = TARGET_FUELS[fuel]
        diff = new - old
        arrow = "🔺" if diff > 0 else "🔻"
        lines.append(f"{arrow} {th} ({en}): {old:.2f} → {new:.2f} บาท ({diff:+.2f})")
    if other_changes:
        lines.append("\n📋 น้ำมันอื่นที่เปลี่ยน:")
        for fuel, (old, new) in other_changes.items():
            _, en = FUEL_PATTERNS.get(fuel, (fuel, fuel))
            diff = new - old
            arrow = "🔺" if diff > 0 else "🔻"
            lines.append(f"  {arrow} {en}: {old:.2f} → {new:.2f} ({diff:+.2f})")
    lines += [f"\n📊 Bangchak Corporation",
              f"🕐 {datetime.now(TH_TZ).strftime('%d/%m/%Y %H:%M')}"]
    return "\n".join(lines)

def build_notice_msg(other_changes: dict, eff: str) -> str:
    lines = [f"ℹ️ มีการประกาศปรับราคาน้ำมัน (มีผล {eff})"]
    lines.append("✅ แก๊สโซฮอล E20 และ 95 **คงราคาเดิม**")
    lines.append("\n📋 น้ำมันที่เปลี่ยน:")
    for fuel, (old, new) in other_changes.items():
        _, en = FUEL_PATTERNS.get(fuel, (fuel, fuel))
        diff = new - old
        arrow = "🔺" if diff > 0 else "🔻"
        lines.append(f"  {arrow} {en}: {old:.2f} → {new:.2f} ({diff:+.2f})")
    lines += [f"\n📊 Bangchak Corporation",
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
        print("[ERR] no prices"); return 1

    all_changes = {}
    for fuel, new_price in now_prices.items():
        old = last.get(fuel, {}).get("price")
        if old is not None and abs(old - new_price) > 0.001:
            all_changes[fuel] = (old, new_price)

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

    if send(msg, key, phone):
        save_now(now_prices, eff)
        print("[OK] sent & saved")
        return 0
    print("[ERR] send failed")
    return 1

if __name__ == "__main__":
    exit(main())