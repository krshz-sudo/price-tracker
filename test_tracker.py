"""Offline logic tests for tracker.py (no network, no Telegram, no file writes).

Run:  python test_tracker.py
"""
import sys
from pathlib import Path

import tracker

tracker.logging.basicConfig(level="WARNING")
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

passed = 0


def check(cond, label):
    global passed
    assert cond, label
    passed += 1


# --- parse_price (0 / None / junk are never prices) ---
check(tracker.parse_price("₹1,299.00") == 1299.0, "parse rupees")
check(tracker.parse_price("Rs. 54,999") == 54999.0, "parse Rs.")
check(tracker.parse_price(799) == 799.0, "parse int")
check(tracker.parse_price("N/A") is None, "parse junk")
check(tracker.parse_price(0) is None, "parse 0")
check(tracker.parse_price("₹0") is None, "parse ₹0")
check(tracker.parse_price(None) is None, "parse None")

# --- extract_price per site (selectors untouched) ---
check(tracker.extract_price(
    '<div id="corePrice_feature_div"><span class="a-price"><span class="a-offscreen">₹59,900.00</span></span></div>',
    "amazon") == 59900.0, "amazon selector")
check(tracker.extract_price('<div class="Nx9bqj CxhGGd">₹61,999</div>', "flipkart") == 61999.0, "flipkart selector")
check(tracker.extract_price('<script>window.__myx = {"price":{"mrp":999,"discounted":449}}</script>',
                            "myntra") == 449.0, "myntra regex")
check(tracker.extract_price('<div class="prod-sp">₹ 1,049</div>', "ajio") == 1049.0, "ajio selector")
check(tracker.extract_price(
    '<script type="application/ld+json">{"@type":"Product","offers":{"price":"1234"}}</script>',
    "ajio") == 1234.0, "json-ld fallback")

# --- Flipkart: real saved page snippets with a decoy ₹152 add-on placed FIRST ---
FK_URL = "https://www.flipkart.com/nothing-phone-4a-white-128-gb/p/itm4d8bff10483ce?pid=MOBHZAWYUQDFFTRB"
fk_lines = (Path(__file__).parent / "test_data" / "flipkart_sample.html").read_text(encoding="utf-8").splitlines()


def fk_page(*drop_ids):
    """Fixture with the given script blocks removed ('main-ppd', 'ld+json', ...)."""
    return "\n".join(l for l in fk_lines if not any(d in l for d in drop_ids))


check("152" in fk_page(), "fixture contains the ₹152 decoy")
check(tracker.extract_price(fk_page(), "flipkart", FK_URL) == 44999.0, "flipkart full page -> ppd 44999")
check(tracker.extract_price(fk_page(), "flipkart") == 44999.0, "flipkart without url")
check(tracker.extract_price(fk_page("main-ppd"), "flipkart", FK_URL) == 44999.0,
      "no ppd -> JSON-LD Product 44999")
wrong_pid = FK_URL.replace("MOBHZAWYUQDFFTRB", "MOBXXXXXXXXXXXXX")
check(tracker.extract_price(fk_page("main-ppd", "main-tracking", "main-primary"), "flipkart", wrong_pid)
      is None, "JSON-LD with a different sku is ignored")
check(tracker.extract_price(fk_page("main-ppd", "ld+json"), "flipkart", FK_URL) == 44999.0,
      "no ppd/JSON-LD -> productPrice regex 44999")
check(tracker.extract_price(fk_page("main-ppd", "ld+json", "main-tracking"), "flipkart", FK_URL)
      == 44999.0, "-> primaryProductPrice regex 44999")
check(tracker.extract_price(fk_page("main-ppd", "ld+json", "main-tracking", "main-primary"),
                            "flipkart", FK_URL) is None, "decoy only -> None, never 152")
check(tracker.extract_price('<div class="Nx9bqj">₹41,999</div>' + fk_page("main-ppd", "ld+json"),
                            "flipkart", FK_URL) == 41999.0, "CSS selector beats regex fallback")

# --- decide(): pure rule ---
check(tracker.decide(1001, 1000, None) == "none", "above target")
check(tracker.decide(1000, 1000, None) == "alert", "equal to target alerts")
check(tracker.decide(900, 1000, 900) == "duplicate", "same as last alert")
check(tracker.decide(850, 1000, 900) == "alert", "below last alert")
check(tracker.decide(950, 1000, 900) == "hold", "above last alert, still under target")
check(tracker.decide(1100, 1000, 900) == "reset", "back above target clears")
check(tracker.decide(500, None, None) == "none", "no target -> no alert")

# --- process_product with fake fetch + fake Telegram ---
sent = []
ORIGINAL_FETCH = tracker.fetch_price
tracker.send_telegram = lambda msg: sent.append(msg) or True
URL = "https://www.flipkart.com/x/p/itm1"
product = {"name": "Test <Phone>", "url": URL, "site": "flipkart", "target_price": 37000}


def run_sequence(prices, state):
    it = iter(prices)
    tracker.fetch_price = lambda s, u, site: next(it)
    counts = []
    for _ in prices:
        tracker.process_product(product, state, None, dry_run=False)
        counts.append(len(sent))
    return counts


state = {}
#           above  at-target dup    lower  higher(hold) dup    above(reset) drop again  None   0      junk
seq = [39000, 37000, 37000, 36500, 36800, 36500, 38000, 36800, None, 0, "abc"]
counts = run_sequence(seq, state)
check(counts == [0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3], f"alert sequence {counts}")
check(state[URL]["last_alerted_price"] == 36800, "last alerted stored")
check(state[URL]["last_price"] == 36800, "invalid prices don't overwrite last_price")

msg = sent[0]
for part in ("Test &lt;Phone&gt;", "Current price: <b>₹37,000</b>", "Target price: ₹37,000", URL):
    check(part in msg, f"message contains {part!r}")
check("Drop" not in msg and "Old price" not in msg, "message has no drop/old-price lines")

# Reset after going above target
state2 = {URL: {"last_price": 36000, "last_alerted_price": 36000, "last_alerted_at": "x"}}
sent.clear()
run_sequence([38000], state2)
check("last_alerted_price" not in state2[URL] and "last_alerted_at" not in state2[URL], "reset clears")

# Failed Telegram send -> not stored, retried next run
tracker.send_telegram = lambda msg: False
state3 = {}
run_sequence([36000], state3)
check("last_alerted_price" not in state3.get(URL, {}), "failed send not recorded")
tracker.send_telegram = lambda msg: sent.append(msg) or True

# fetch raising never crashes
def boom(*a):
    raise RuntimeError("boom")
tracker.fetch_price = boom
check(tracker.process_product(product, {}, None, dry_run=False) is False, "fetch exception skipped")

# --- Sanity guard: < 50% of last stored price -> no alert, not saved ---
sent.clear()
guard_entry = {"last_price": 44999.0, "name": "Test <Phone>", "site": "flipkart", "updated_at": "t"}
state4 = {URL: dict(guard_entry)}
tracker.fetch_price = lambda s, u, site: 152
changed = tracker.process_product(product, state4, None, dry_run=False)
check(changed is False and state4[URL] == guard_entry and not sent, "₹152 vs ₹44,999: skipped, not saved")
tracker.fetch_price = lambda s, u, site: 22499
tracker.process_product(product, state4, None, dry_run=False)
check(state4[URL] == guard_entry and not sent, "just under 50% skipped")
tracker.fetch_price = lambda s, u, site: 22500
tracker.process_product(product, state4, None, dry_run=False)
check(state4[URL]["last_price"] == 22500 and len(sent) == 1, "exactly 50% accepted (and alerts: <= target)")
sent.clear()

# --- Target guard: < 40% of target_price -> no alert, not saved (even with no saved price) ---
# target 37000 -> threshold 14800
tracker.fetch_price = lambda s, u, site: 152
state6 = {}
check(tracker.process_product(product, state6, None, dry_run=False) is False
      and state6 == {} and not sent, "first check: ₹152 vs target ₹37,000 skipped, nothing saved")
tracker.fetch_price = lambda s, u, site: 14799
tracker.process_product(product, state6, None, dry_run=False)
check(state6 == {} and not sent, "first check: just under 40% of target skipped")
tracker.fetch_price = lambda s, u, site: 14800
tracker.process_product(product, state6, None, dry_run=False)
check(state6[URL]["last_price"] == 14800 and len(sent) == 1, "exactly 40% of target accepted (alerts)")
sent.clear()

for site_name, site_url in (("amazon", "https://www.amazon.in/dp/X"), ("flipkart", "https://www.flipkart.com/x/p/y"),
                            ("myntra", "https://www.myntra.com/x/1/buy"), ("ajio", "https://www.ajio.com/p/1")):
    p = {"name": site_name, "url": site_url, "site": site_name, "target_price": 1000}
    for start in ({}, {site_url: {"last_price": 700.0}}):  # 399 >= 50% of 700: only the target guard applies
        st = {k: dict(v) for k, v in start.items()}
        tracker.fetch_price = lambda s, u, sname: 399
        tracker.process_product(p, st, None, dry_run=False)
        check(st == start and not sent, f"target guard applies to {site_name} (saved price: {bool(start)})")

no_target = {"name": "No target", "url": URL, "site": "flipkart", "target_price": None}
state7 = {}
tracker.fetch_price = lambda s, u, site: 152
tracker.process_product(no_target, state7, None, dry_run=False)
check(state7[URL]["last_price"] == 152 and not sent, "no target_price: target guard not applied, no alert")



# End-to-end: real fixture through fetch_price -> process_product (fake HTTP)
class FakeResp:
    status_code, url, text = 200, FK_URL, fk_page()


class FakeSession:
    def get(self, *a, **k):
        return FakeResp()


tracker.fetch_price = ORIGINAL_FETCH
fk_product = {"name": "Nothing Phone (4a)", "url": FK_URL, "site": "flipkart", "target_price": 37000}
state5 = {}
tracker.process_product(fk_product, state5, FakeSession(), dry_run=False)
check(state5[FK_URL]["last_price"] == 44999.0 and not sent, "fixture end-to-end: 44999 stored, no alert")

# --- migrate_state: backward compatibility ---
s, ch = tracker.migrate_state({})
check(s == {} and ch is False, "empty {} loads")
s, ch = tracker.migrate_state(None)
check(s == {} and ch is False, "missing file loads")
s, ch = tracker.migrate_state([1, 2])
check(s == {} and ch is True, "list replaced")
old = {URL: {"last_price": 1940.0, "previous_price": 2000.0, "last_alerted_price": 1940.0,
             "last_alerted_at": "2026-10-11T00:00:00+00:00", "name": "Old", "site": "flipkart",
             "updated_at": "2026-10-11T00:00:00+00:00"}}
s, ch = tracker.migrate_state(old)
check(s == old and ch is False, "current/old dict format kept as-is")
s, ch = tracker.migrate_state({URL: 1999})
check(s == {URL: {"last_price": 1999.0}} and ch, "{url: price} migrated")
s, ch = tracker.migrate_state({URL: {"last_price": "₹1,999", "last_alerted_price": 0,
                                     "last_alerted_at": "x"}})
check(s == {URL: {"last_price": 1999.0}} and ch, "bad values cleaned")

print(f"ALL {passed} CHECKS PASSED")
print("\nSample alert:\n" + msg)
