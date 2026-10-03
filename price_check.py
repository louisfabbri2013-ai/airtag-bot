import json
import os
import re
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PRODUCT_URL = os.environ.get("PRODUCT_URL", "").strip()
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
TARGET_PRICE = float(os.environ["TARGET_PRICE"]) if os.environ.get("TARGET_PRICE", "").strip() else None
STATE_FILE = Path("price_state.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml",
}


class Blocked(Exception):
    """Amazon ha risposto con CAPTCHA / blocco anti-bot."""


def parse_price(text: str) -> float:
    cleaned = re.sub(r"[^\d,\.]", "", text)
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    return float(cleaned)


def fetch_price() -> tuple[str, float]:
    resp = requests.get(PRODUCT_URL, headers=HEADERS, timeout=20)
    if resp.status_code in (429, 503):
        raise Blocked(f"HTTP {resp.status_code}")
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    if soup.find("form", action=re.compile("validateCaptcha")) or "captcha" in resp.text.lower()[:5000]:
        raise Blocked("CAPTCHA")

    title_el = soup.select_one("#productTitle")
    title = title_el.get_text(strip=True) if title_el else "Prodotto Amazon"

    for selector in (
        "#corePrice_feature_div span.a-offscreen",
        "span.a-price span.a-offscreen",
        "#priceblock_ourprice",
        "#priceblock_dealprice",
    ):
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            return title, parse_price(el.get_text())

    raise ValueError("Prezzo non trovato nella pagina")


def notify(title: str, message: str, priority: str = "default", tags: str = "moneybag") -> None:
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": tags,
                "Click": PRODUCT_URL,
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        print(f"Invio notifica fallito: {exc}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_price": None, "blocked_notified": False}


def main() -> None:
    if not PRODUCT_URL or not NTFY_TOPIC:
        sys.exit("Mancano i secrets PRODUCT_URL e/o NTFY_TOPIC.")

    state = load_state()

    try:
        title, price = fetch_price()
    except Blocked as exc:
        print(f"Bloccato da Amazon ({exc})")
        if not state["blocked_notified"]:
            notify(
                "Price bot bloccato",
                f"Amazon sta bloccando le richieste ({exc}). Se succede sempre, prova Keepa.",
                priority="high",
                tags="warning",
            )
            state["blocked_notified"] = True
            STATE_FILE.write_text(json.dumps(state))
        return
    except (requests.RequestException, ValueError) as exc:
        print(f"Errore nel controllo: {exc}")
        STATE_FILE.write_text(json.dumps(state))
        return

    state["blocked_notified"] = False
    previous = state["last_price"]
    print(f"{title[:50]} -> {price:.2f} EUR (prima: {previous})")

    changed = previous is not None and abs(price - previous) > 0.001
    dropped = previous is not None and price < previous

    if previous is None:
        notify(
            "Price bot attivo ✅",
            f"{title[:60]}\nPrezzo attuale: {price:.2f} €",
            tags="robot",
        )
    elif TARGET_PRICE is not None:
        if price <= TARGET_PRICE and (previous > TARGET_PRICE or dropped):
            notify(
                "🎯 Prezzo sotto soglia!",
                f"{title[:60]}\nOra: {price:.2f} € (soglia {TARGET_PRICE:.2f} €)",
                priority="high",
                tags="tada",
            )
    elif changed:
        arrow = "📉 Sceso" if dropped else "📈 Salito"
        notify(
            f"{arrow} il prezzo",
            f"{title[:60]}\n{previous:.2f} € → {price:.2f} €",
            priority="high" if dropped else "default",
            tags="chart_with_downwards_trend" if dropped else "chart_with_upwards_trend",
        )

    state["last_price"] = price
    STATE_FILE.write_text(json.dumps(state))


if __name__ == "__main__":
    main()
