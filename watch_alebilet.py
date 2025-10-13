#!/usr/bin/env python3
import os
import sys
import csv
import json
import smtplib
import time
from email.message import EmailMessage
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

URL = os.getenv("URL", "https://www.alebilet.pl/bilety/coma/2025-10-17/20:00/re-start-gra-o-wszystko")
THRESHOLD = float(os.getenv("THRESHOLD_PLN", "230.0"))
EMAIL_TO = os.getenv("EMAIL_TO", "boryzo@gmail.com")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")  # ustaw w Secrets
SMTP_PASS = os.getenv("SMTP_PASS")  # ustaw w Secrets
FROM_ADDR = os.getenv("FROM_ADDR") or SMTP_USER or "no-reply@example.com"

STATE_PATH = os.getenv("STATE_PATH", ".alebilet_state.json")
LOG_PATH = os.getenv("LOG_PATH", "logs/alebilet_log.csv")

TZ = ZoneInfo("Europe/Warsaw")


def log_row(timestamp: datetime, price, status: str, note: str = ""):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    write_header = not os.path.exists(LOG_PATH)
    with open(LOG_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(["timestamp", "price_pln", "status", "note"])
        w.writerow([
            timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            f"{price:.2f}" if isinstance(price, (float, int)) else "",
            status,
            note or ""
        ])


def load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_below": False}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_below": False}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def base_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def fetch_html_with_warm(url: str, max_retries: int = 3, backoff_ms: int = 800) -> str:
    sess = requests.Session()
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            # warm-up on domain to pick cookies
            r0 = sess.get("https://www.alebilet.pl/", headers=base_headers(), timeout=15, allow_redirects=True)
            # even if 403 here, still try target; some WAFs set cookies anyway
            headers = dict(base_headers())
            headers["Referer"] = "https://www.alebilet.pl/"
            r = sess.get(url, headers=headers, timeout=20, allow_redirects=True)
            if 200 <= r.status_code < 300 and r.text and len(r.text) > 1000:
                return r.text
            # retry on 403/5xx or too-small responses
            if r.status_code in (403, 429) or r.status_code >= 500 or len(r.text) < 1000:
                raise RuntimeError(f"Bad HTTP {r.status_code} or tiny body")
            raise RuntimeError(f"Unexpected HTTP {r.status_code}")
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                time.sleep((backoff_ms / 1000.0) * (attempt + 1))
                continue
            break
    raise RuntimeError(f"Fetch failed after retries: {last_err}")


def parse_price_pln(s: str) -> float:
    # accepts "244,95", "1 234,56", "1.234,56", with &nbsp;
    cleaned = (
        s.replace("\xa0", " ")
         .replace(" ", "")
         .replace(".", "", 1)  # remove one thousands dot if present; we will still be safe below
    )
    # remove all thousand separators (spaces or dots), keep decimal comma
    cleaned = cleaned.replace(" ", "").replace(".", "")
    cleaned = cleaned.replace(",", ".")
    return float(cleaned)


def extract_plate_price(html: str):
    soup = BeautifulSoup(html, "html.parser")
    # find tr with data-area="plyta" and class including "category"
    row = None
    for tr in soup.find_all("tr"):
        if tr.has_attr("data-area") and str(tr["data-area"]).lower() == "plyta":
            classes = " ".join(tr.get("class", [])).lower()
            if "category" in classes:
                row = tr
                break
    if not row:
        return None

    price_td = row.find("td", class_="price")
    if not price_td:
        return None
    bold = price_td.find("b")
    if not bold or not bold.get_text(strip=True):
        return None

    text = bold.get_text(" ", strip=True)
    # e.g. "244,95 zł"
    num_part = text.lower().replace("zł", "").strip()
    return parse_price_pln(num_part)


def send_email_alert(current_price: float):
    if not (SMTP_HOST and SMTP_PORT and SMTP_USER and SMTP_PASS):
        raise RuntimeError("SMTP credentials missing. Set SMTP_USER and SMTP_PASS as repo secrets.")

    msg = EmailMessage()
    msg["Subject"] = 'ALERT: COMA „Płyta” < 230 zł (alebilet)'
    msg["From"] = FROM_ADDR
    msg["To"] = EMAIL_TO
    lines = [
        "Cena „Płyta” spadła poniżej progu.",
        "",
        f"Aktualna cena: {current_price:.2f} zł",
        f"Próg: {THRESHOLD:.2f} zł",
        "",
        f"Strona: {URL}",
        "",
        "—",
        "Automatyczny monitoring (GitHub Actions).",
    ]
    msg.set_content("\n".join(lines))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


def main():
    now = datetime.now(TZ)
    state = load_state()
    last_below = bool(state.get("last_below", False))

    try:
        html = fetch_html_with_warm(URL)
        price = extract_plate_price(html)
        if price is None:
            log_row(now, None, "NO_MATCH", "Nie znaleziono wiersza Płyta/ceny")
            print("NO_MATCH")
            save_state(state)  # no change
            return 0

        status = "BELOW" if price < THRESHOLD else "ABOVE"

        note = ""
        if status == "BELOW" and not last_below:
            # send email and latch
            try:
                send_email_alert(price)
                note = "Alert sent"
                state["last_below"] = True
            except Exception as e:
                # log failure but keep status latched to avoid mail storm
                note = f"Email error: {e}"
                state["last_below"] = False
        elif status == "ABOVE":
            if last_below:
                note = "Latch reset (price went above threshold)"
            state["last_below"] = False

        log_row(now, price, status, note)
        save_state(state)
        print(f"{status}: {price:.2f}")
        return 0

    except Exception as e:
        log_row(now, None, "ERROR", str(e))
        print(f"ERROR: {e}", file=sys.stderr)
        # nie wysadzaj całego joba przez 403 — zwróć 0, żeby workflow mógł działać dalej
        save_state(state)
        return 0


if __name__ == "__main__":
    sys.exit(main())
