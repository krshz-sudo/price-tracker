#!/usr/bin/env python3
"""
Price tracker + Telegram alert bot.

Alert rule: a Telegram alert is sent when current_price <= target_price.
The last alerted price is stored per product URL in prices.json so the same
price never alerts twice; a lower price alerts again, and once the price goes
back above target_price the stored value is cleared so a later drop alerts again.

Usage:
    python tracker.py            # normal run: check all products, alert, update prices.json
    python tracker.py --test     # send a test Telegram message and exit
    python tracker.py --dry-run  # check prices and print results; no Telegram, no file writes

Secrets are read ONLY from environment variables:
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "products.json"
PRICES_FILE = BASE_DIR / "prices.json"

MAX_PRODUCTS = 10
REQUEST_TIMEOUT = 20  # seconds
DELAY_RANGE = (1.0, 3.0)  # random delay between products, seconds
# Sanity guard: a new price below this fraction of the last stored price is treated
# as a mis-read (e.g. an add-on / accessory price) — no alert, not saved.
SANITY_MIN_RATIO = 0.5
# Sanity guard (all sites, works even with no saved price): a price below this
# fraction of the product's target_price is treated as a mis-read — no alert, not saved.
TARGET_MIN_RATIO = 0.4

# ---------------------------------------------------------------------------
# SITE CONFIG — edit this when a site changes its HTML.
#
#   selectors      : CSS selectors tried in order; first one with a parseable
#                    price wins.
#   regex          : regex patterns tried against the raw HTML (useful for
#                    prices embedded in inline JSON). Group 1 must be the price.
#   captcha_markers: lowercase substrings that indicate a bot-check page.
#
# After selectors + regex, a generic fallback (JSON-LD / meta tags) is tried.
# ---------------------------------------------------------------------------
SITE_CONFIG: dict[str, dict[str, list[str]]] = {
    "amazon": {
        "selectors": [
            "#corePrice_feature_div .a-price .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price-whole",
            ".a-price.priceToPay .a-offscreen",
            "#priceblock_dealprice",
            "#priceblock_ourprice",
            "#priceblock_saleprice",
            "#apex_desktop .a-price .a-offscreen",
            "span.a-price span.a-offscreen",
        ],
        "regex": [
            r'"priceAmount"\s*:\s*([\d.]+)',
        ],
        "captcha_markers": [
            "validatecaptcha",
            "click the button below to continue shopping",
            "type the characters you see",
            "enter the characters you see below",
            "api-services-support@amazon.com",
        ],
    },
    "flipkart": {
        "selectors": [
            "div.Nx9bqj.CxhGGd",
            "div.Nx9bqj",
            "div._30jeq3._16Jk6d",
            "div._30jeq3",
            "div.hl05eU div.Nx9bqj",
        ],
        "regex": [
            # Fallbacks scoped to the MAIN product only. Never use a bare
            # "finalPrice" match: add-ons (e.g. extended warranty plans) have one too.
            r'"ppd"\s*:\s*\{[^{}]*?"finalPrice"\s*:\s*"?([\d.]+)',
            r'"ppd"\s*:\s*\{[^{}]*?"fsp"\s*:\s*"?([\d.]+)',
            r'"MetaValue"\s*:\s*"([\d.]+)"\s*,\s*"MetaKey"\s*:\s*"productPrice"',
            r'"primaryProductPrice"\s*:\s*([\d.]+)',
        ],
        "captcha_markers": [
            "are you a human",
            "please verify you are a human",
            "recaptcha",
        ],
    },
    "myntra": {
        "selectors": [
            "span.pdp-price strong",
            "span.pdp-price",
            ".pdp-discount-container .pdp-price",
        ],
        "regex": [
            r'"discounted"\s*:\s*([\d.]+)',
            r'"price"\s*:\s*\{\s*"mrp"\s*:\s*[\d.]+\s*,\s*"discounted"\s*:\s*([\d.]+)',
        ],
        "captcha_markers": [
            "access denied",
            "reference #",
            "oops! something went wrong",
        ],
    },
    "ajio": {
        "selectors": [
            ".prod-sp",
            ".prod-price-section .prod-sp",
            "div.prod-price-section span",
        ],
        "regex": [
            r'"offerPrice"\s*:\s*\{[^{}]*?"value"\s*:\s*([\d.]+)',
            r'"price"\s*:\s*\{[^{}]*?"value"\s*:\s*([\d.]+)',
        ],
        "captcha_markers": [
            "access denied",
            "reference #",
        ],
    },
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]

log = logging.getLogger("tracker")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def build_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-IN,en-GB;q=0.9,en-US;q=0.8,en;q=0.7",
        # No 'br': requests can only decode brotli if the optional package is installed.
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "DNT": "1",
    }


def parse_price(text: Any) -> Optional[float]:
    """Turn '₹1,299.00', 'Rs. 1299', '1,299' or 1299 into 1299.0."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if text > 0 else None
    cleaned = str(text).replace(",", "").replace("\xa0", " ")
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if not match:
        return None
    try:
        value = float(match.group(0))
    except ValueError:
        return None
    return value if value > 0 else None


def detect_site(url: str, declared: Optional[str]) -> Optional[str]:
    if declared:
        key = declared.strip().lower().replace(".in", "").replace(".com", "")
        if key in SITE_CONFIG:
            return key
    host = url.lower()
    for key in SITE_CONFIG:
        if key in host:
            return key
    return None


def fmt_inr(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    if float(value).is_integer():
        return f"₹{int(value):,}"
    return f"₹{value:,.2f}"


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        log.error("Could not read %s (%s); using default.", path.name, exc)
        return default


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Price extraction
# ---------------------------------------------------------------------------
def _price_from_jsonld(soup: BeautifulSoup) -> Optional[float]:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                offers = node.get("offers")
                if offers is not None:
                    for offer in offers if isinstance(offers, list) else [offers]:
                        if isinstance(offer, dict):
                            price = parse_price(offer.get("price") or offer.get("lowPrice"))
                            if price:
                                return price
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return None


def _price_from_meta(soup: BeautifulSoup) -> Optional[float]:
    for attrs in (
        {"itemprop": "price"},
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
    ):
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            price = parse_price(tag["content"])
            if price:
                return price
    return None


def _flipkart_pid(url: Optional[str]) -> Optional[str]:
    match = re.search(r"[?&]pid=([A-Za-z0-9]+)", url or "")
    return match.group(1).upper() if match else None


def _flipkart_ppd_price(html_text: str) -> Optional[float]:
    """Main product's price block: "ppd":{"fsp":..,"finalPrice":..,"mrp":..}."""
    prices = set()
    for match in re.finditer(r'"ppd"\s*:\s*(\{[^{}]*\})', html_text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        for key in ("finalPrice", "fsp"):
            price = parse_price(data.get(key))
            if price:
                prices.add(price)
                break
    if len(prices) == 1:
        return prices.pop()
    if len(prices) > 1:
        log.debug("  multiple different ppd prices %s; not trusting ppd.", sorted(prices))
    return None


def _flipkart_jsonld_price(soup: BeautifulSoup, pid: Optional[str]) -> Optional[float]:
    """Top-level JSON-LD Product (sku must match the URL's pid when both are known)."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        nodes = nodes + [n for d in nodes if isinstance(d, dict)
                         for n in (d.get("@graph") or []) if isinstance(d.get("@graph"), list)]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = node.get("@type")
            if "Product" not in (types if isinstance(types, list) else [types]):
                continue
            sku = str(node.get("sku") or node.get("productID") or "").upper()
            if pid and sku and sku != pid:
                continue
            offers = node.get("offers")
            for offer in offers if isinstance(offers, list) else [offers]:
                if isinstance(offer, dict):
                    price = parse_price(offer.get("price") or offer.get("lowPrice"))
                    if price:
                        return price
    return None


def _extract_flipkart(html_text: str, soup: BeautifulSoup, url: Optional[str]) -> Optional[float]:
    """Flipkart: main product data first, then CSS selectors, then main-product regex."""
    cfg = SITE_CONFIG["flipkart"]
    price = _flipkart_ppd_price(html_text)
    if price:
        log.debug("  matched flipkart ppd block -> %s", price)
        return price
    price = _flipkart_jsonld_price(soup, _flipkart_pid(url))
    if price:
        log.debug("  matched flipkart JSON-LD Product -> %s", price)
        return price
    for selector in cfg.get("selectors", []):
        for el in soup.select(selector):
            price = parse_price(el.get("content") or el.get_text(" ", strip=True))
            if price:
                log.debug("  matched selector %r -> %s", selector, price)
                return price
    for pattern in cfg.get("regex", []):
        match = re.search(pattern, html_text)
        if match:
            price = parse_price(match.group(1))
            if price:
                log.debug("  matched regex %r -> %s", pattern, price)
                return price
    price = _price_from_meta(soup)
    if price:
        log.debug("  matched meta fallback -> %s", price)
    return price


def extract_price(html_text: str, site: str, url: Optional[str] = None) -> Optional[float]:
    cfg = SITE_CONFIG[site]
    soup = BeautifulSoup(html_text, "html.parser")

    if site == "flipkart":
        return _extract_flipkart(html_text, soup, url)

    for selector in cfg.get("selectors", []):
        for el in soup.select(selector):
            price = parse_price(el.get("content") or el.get_text(" ", strip=True))
            if price:
                log.debug("  matched selector %r -> %s", selector, price)
                return price

    for pattern in cfg.get("regex", []):
        match = re.search(pattern, html_text)
        if match:
            price = parse_price(match.group(1))
            if price:
                log.debug("  matched regex %r -> %s", pattern, price)
                return price

    price = _price_from_jsonld(soup) or _price_from_meta(soup)
    if price:
        log.debug("  matched generic JSON-LD/meta fallback -> %s", price)
    return price


def fetch_price(session: requests.Session, url: str, site: str) -> Optional[float]:
    """Return the current price, or None on any failure. Never raises."""
    try:
        resp = session.get(url, headers=build_headers(), timeout=REQUEST_TIMEOUT,
                           allow_redirects=True)
    except requests.RequestException as exc:
        log.warning("  network error: %s", exc)
        return None

    body = resp.text or ""
    lowered = body.lower()
    markers = SITE_CONFIG[site].get("captcha_markers", [])
    looks_blocked = any(marker in lowered for marker in markers)

    if "captcha" in resp.url.lower() or (resp.status_code != 200 and looks_blocked):
        log.warning("  CAPTCHA / bot-check page (HTTP %s). Skipping.", resp.status_code)
        return None

    if resp.status_code != 200:
        log.warning("  HTTP %s from %s. Skipping.", resp.status_code, site)
        return None

    try:
        price = extract_price(body, site, url)
    except Exception as exc:  # parser bugs must never kill the run
        log.warning("  parse error: %s", exc)
        return None

    if price is None:
        if looks_blocked:
            log.warning("  CAPTCHA / bot-check page detected. Skipping.")
        else:
            log.warning("  price not found — selectors in SITE_CONFIG['%s'] may need updating.", site)
    return price


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID env vars are not set; cannot send.")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as exc:
        # Don't log the URL: it contains the token.
        log.error("Telegram request failed: %s", type(exc).__name__)
        return False
    if resp.status_code != 200:
        log.error("Telegram API error %s: %s", resp.status_code, resp.text[:300])
        return False
    return True


def build_alert(name: str, url: str, price: float, target: float) -> str:
    return "\n".join([
        "🔔 <b>Price Alert!</b>",
        "",
        f"<b>{html.escape(name)}</b>",
        f"Current price: <b>{fmt_inr(price)}</b>",
        f"Target price: {fmt_inr(target)}",
        "",
        f'<a href="{html.escape(url, quote=True)}">🛒 Open product</a>',
    ])


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def decide(price: float, target: Optional[float], last_alerted: Optional[float]) -> str:
    """Pure alert rule. Returns one of:

    'alert'     price <= target and (never alerted, or below the last alerted price)
    'duplicate' price <= target and equal to the last alerted price
    'hold'      price <= target but above the last alerted price (no new alert)
    'reset'     price > target and an alert was stored -> clear it
    'none'      nothing to do (above target, or no valid target)
    """
    if target is None:
        return "none"
    if price > target:
        return "reset" if last_alerted is not None else "none"
    if last_alerted is None or price < last_alerted:
        return "alert"
    if price == last_alerted:
        return "duplicate"
    return "hold"


def migrate_state(raw: Any) -> tuple[dict, bool]:
    """Normalise prices.json content into {url: {...}}. Never raises.

    Accepts {} / older formats (including {url: price}) and drops invalid
    values (0, None, non-numeric). Returns (state, changed).
    """
    if not isinstance(raw, dict):
        if raw not in (None, {}):
            log.warning("prices.json has an unexpected format; starting fresh.")
        return {}, raw not in (None, {})

    state: dict = {}
    changed = False
    for url, entry in raw.items():
        if not isinstance(url, str) or not url.strip():
            changed = True
            continue
        if not isinstance(entry, dict):
            # Very old / hand-written format: {url: price}
            price = parse_price(entry)
            state[url] = {"last_price": price} if price else {}
            changed = True
            continue
        new_entry = dict(entry)
        for key in ("last_price", "previous_price", "last_alerted_price"):
            if key in new_entry:
                value = parse_price(new_entry[key])
                if value is None:
                    new_entry.pop(key)
                    if key == "last_alerted_price":
                        new_entry.pop("last_alerted_at", None)
                elif value != new_entry[key]:
                    new_entry[key] = value
        if new_entry != entry:
            changed = True
        state[url] = new_entry
    if changed:
        log.info("prices.json migrated to the current format.")
    return state, changed


def process_product(product: dict, state: dict, session: requests.Session,
                    dry_run: bool) -> bool:
    """Check one product. Mutates `state`. Returns True if state changed."""
    name = str(product.get("name") or "Unnamed product")
    url = str(product.get("url") or "").strip()
    if not url:
        log.warning("[%s] missing url, skipping.", name)
        return False

    site = detect_site(url, product.get("site"))
    if not site:
        log.warning("[%s] unsupported site %r, skipping.", name, product.get("site"))
        return False

    log.info("[%s] (%s) fetching...", name, site)
    try:
        new_price = parse_price(fetch_price(session, url, site))
    except Exception as exc:
        log.warning("  fetch failed: %s. Skipping.", exc)
        return False
    if new_price is None or new_price <= 0:
        log.warning("  no valid price (missing, zero or unparsable). Skipping.")
        return False

    entry = state.get(url)
    if not isinstance(entry, dict):
        entry = {}
    old_price = parse_price(entry.get("last_price"))
    last_alerted = parse_price(entry.get("last_alerted_price"))
    target = parse_price(product.get("target_price"))
    log.info("  price: %s (last: %s, target: %s, last alerted: %s)", fmt_inr(new_price),
             fmt_inr(old_price), fmt_inr(target), fmt_inr(last_alerted))
    if target is None:
        log.warning("  target_price missing or invalid; no alerts for this product.")

    if target is not None and new_price < target * TARGET_MIN_RATIO:
        log.warning("  %s is less than %d%% of the target price %s — probably a mis-read "
                    "(add-on/accessory price). Not alerting, not saving. Skipping.",
                    fmt_inr(new_price), int(TARGET_MIN_RATIO * 100), fmt_inr(target))
        return False

    if old_price and new_price < old_price * SANITY_MIN_RATIO:
        log.warning("  %s is less than %d%% of the last price %s — probably a mis-read "
                    "(add-on/accessory price). Not alerting, not saving. Skipping.",
                    fmt_inr(new_price), int(SANITY_MIN_RATIO * 100), fmt_inr(old_price))
        return False

    new_entry = dict(entry)
    new_entry["name"] = name
    new_entry["site"] = site

    action = decide(new_price, target, last_alerted)
    if action == "alert":
        msg = build_alert(name, url, new_price, target)  # type: ignore[arg-type]
        if dry_run:
            log.info("  [dry-run] would send alert:\n%s", msg)
        elif send_telegram(msg):
            log.info("  ✅ alert sent.")
            new_entry["last_alerted_price"] = new_price
            new_entry["last_alerted_at"] = now_iso()
        else:
            log.warning("  alert NOT sent (will retry next run).")
    elif action == "duplicate":
        log.info("  already alerted at %s — skipping duplicate.", fmt_inr(new_price))
    elif action == "hold":
        log.info("  still at/below target but above last alerted %s — no new alert.",
                 fmt_inr(last_alerted))
    elif action == "reset":
        # Back above target: allow a fresh alert on the next drop.
        log.info("  back above target — clearing last alerted price.")
        new_entry.pop("last_alerted_price", None)
        new_entry.pop("last_alerted_at", None)

    if old_price is None or old_price != new_price:
        new_entry["last_price"] = new_price
        new_entry["updated_at"] = now_iso()
        if old_price is not None:
            new_entry["previous_price"] = old_price

    if new_entry != entry:
        state[url] = new_entry
        return True
    return False


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_products() -> list[dict]:
    data = load_json(PRODUCTS_FILE, [])
    if isinstance(data, dict):  # allow {"products": [...]}
        data = data.get("products", [])
    if not isinstance(data, list):
        log.error("products.json must contain a list of products.")
        return []
    products = [p for p in data if isinstance(p, dict)]
    if len(products) > MAX_PRODUCTS:
        log.warning("products.json has %d products; only the first %d are tracked.",
                    len(products), MAX_PRODUCTS)
        products = products[:MAX_PRODUCTS]
    return products


def run(dry_run: bool = False) -> int:
    products = load_products()
    if not products:
        log.warning("No products to track.")
        return 0

    state, changed = migrate_state(load_json(PRICES_FILE, {}))

    with requests.Session() as session:
        for i, product in enumerate(products):
            if i > 0:
                time.sleep(random.uniform(*DELAY_RANGE))
            try:
                if process_product(product, state, session, dry_run):
                    changed = True
            except Exception:  # absolute last line of defence: never crash
                log.exception("Unexpected error for product %r; continuing.",
                              product.get("name"))

    # Drop state for products no longer in products.json
    tracked = {str(p.get("url", "")).strip() for p in products}
    for url in list(state):
        if url not in tracked:
            del state[url]
            changed = True

    if dry_run:
        log.info("[dry-run] prices.json not written.")
    elif changed:
        try:
            save_json(PRICES_FILE, state)
            log.info("prices.json updated.")
        except OSError as exc:
            log.error("Failed to write prices.json: %s", exc)
    else:
        log.info("No price changes.")
    return 0


def send_test_message() -> int:
    text = (
        "✅ <b>Price Tracker test message</b>\n\n"
        "Your Telegram bot is configured correctly.\n"
        f"Sent at {now_iso()}"
    )
    if send_telegram(text):
        log.info("Test message sent successfully.")
        return 0
    log.error("Test message failed. Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Price tracker + Telegram alerts")
    parser.add_argument("--test", action="store_true", help="send a test Telegram message")
    parser.add_argument("--dry-run", action="store_true",
                        help="check prices without sending alerts or writing prices.json")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args()

    # Windows consoles may not be UTF-8; avoid UnicodeEncodeError on ₹ / emoji.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.test:
        return send_test_message()
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
