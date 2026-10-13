# 🛒 Price Tracker + Telegram Alert Bot

Tracks product prices on **Amazon.in, Flipkart, Myntra and Ajio** every 5 minutes using
**GitHub Actions** (free) and pings you on **Telegram** (free) when a price is at or below
your target price.

- No paid APIs, no proxies, no credit card.
- Plain `requests` + `BeautifulSoup`.
- State is kept in `prices.json`, committed back to the repo by the workflow.

---

## How it works

1. `products.json` lists up to 10 products.
2. `tracker.py` fetches each page (realistic browser headers, random 1–3 s delay between products)
   and extracts the price using the selectors in `SITE_CONFIG` at the top of the file.
3. An alert is sent **only** when `current_price <= target_price`.
4. **No duplicate alerts:** `last_alerted_price` is stored per product URL in `prices.json`.
   - Same price as the last alert → no alert.
   - Price falls below the last alerted price → new alert.
   - Price goes back above `target_price` → `last_alerted_price` is cleared, so a later drop alerts again.
5. Failed fetches, HTTP errors, CAPTCHA pages and missing/zero/unparsable prices are logged
   and skipped. The script never crashes and never alerts on a price of 0.
6. `prices.json` is backward compatible: `{}` or files written by older versions are loaded
   and migrated automatically.

---

## 1. Create a Telegram bot

1. In Telegram, open **[@BotFather](https://t.me/BotFather)** and send `/newbot`.
2. Pick a name and a username (it must end in `bot`).
3. BotFather replies with a **token** like `123456789:AAH...`. This is your `TELEGRAM_BOT_TOKEN`.
4. Open a chat with your new bot and send it any message (e.g. `hi`). Bots can't message
   you until you've messaged them first.
5. Get your **chat ID**. Open this URL in a browser (put your token in):
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   Find `"chat":{"id":123456789,...}`. That number is your `TELEGRAM_CHAT_ID`.
   (For a group, add the bot to the group, send a message, and use the negative id, e.g. `-100...`.)

## 2. Put the code on GitHub and add secrets

1. Create a **public** GitHub repository (see the minutes note below) and push this folder to it.
2. Go to **Settings → Secrets and variables → Actions → New repository secret** and add:
   | Name                 | Value                        |
   |----------------------|------------------------------|
   | `TELEGRAM_BOT_TOKEN` | the token from BotFather     |
   | `TELEGRAM_CHAT_ID`   | your chat id                 |
3. Go to **Settings → Actions → General → Workflow permissions** and select
   **Read and write permissions**, so the workflow can commit `prices.json`.
   (The workflow also sets `permissions: contents: write`.)
4. Open the **Actions** tab, enable workflows if asked, pick **Price Tracker → Run workflow**
   to test it right away. After that it runs on its own every 5 minutes.

> [!IMPORTANT]
> **Use a public repo.** A 5-minute schedule means about 8,600 runs a month. Public repos get
> unlimited free Actions minutes. Private repos only get 2,000 free minutes a month, and each
> run is billed as at least 1 minute, so the quota runs out in about a week. Your secrets stay
> encrypted either way. Only `products.json` and `prices.json` are visible.

> [!NOTE]
> GitHub's cron is best-effort. Under load, "every 5 minutes" often turns into every
> 10–20 minutes. Scheduled workflows in public repos are also disabled after 60 days without
> repo activity. If that happens, re-enable the workflow from the Actions tab.

## 3. Add or edit products

Edit `products.json` (max 10 entries; anything beyond 10 is ignored):

```json
[
  {
    "name": "boAt Airdopes 219",
    "url": "https://www.flipkart.com/boat-airdopes-219-.../p/itm6492423f46c95",
    "site": "flipkart",
    "target_price": 799
  }
]
```

| Field              | Meaning                                                            |
|--------------------|--------------------------------------------------------------------|
| `name`             | Label used in the alert                                            |
| `url`              | Product page URL (also the key in `prices.json`)                   |
| `site`             | `amazon`, `flipkart`, `myntra` or `ajio` (auto-detected from URL if omitted) |
| `target_price`     | Alert when price ≤ this (₹). Use `null` to disable alerts         |

Tips: for Amazon, the short form `https://www.amazon.in/dp/<ASIN>` works best. Remove
tracking parameters (`?ref=...`, `&pid=...`) from URLs.

## 4. Run and test locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set the secrets as environment variables **for the current shell only** (don't write them into files):

```powershell
# Windows PowerShell
$env:TELEGRAM_BOT_TOKEN = "123456789:AAH..."
$env:TELEGRAM_CHAT_ID   = "123456789"
```
```bash
# macOS / Linux
export TELEGRAM_BOT_TOKEN="123456789:AAH..."
export TELEGRAM_CHAT_ID="123456789"
```

Then:

```bash
python tracker.py --test      # sends a test Telegram message (exit code 1 if it fails)
python tracker.py --dry-run   # fetch prices and print would-be alerts; sends nothing, writes nothing
python tracker.py             # real run: alerts + updates prices.json
python tracker.py -v          # verbose: shows which selector/regex matched
python test_tracker.py        # offline logic tests (no network, no Telegram)
```

## 5. Fixing a broken site (selectors)

Sites change their HTML often. Everything you need to change is in `SITE_CONFIG` at the
top of `tracker.py`:

- `selectors`: CSS selectors tried in order.
- `regex`: patterns run against the raw HTML, for prices in inline JSON.
- `captcha_markers`: text that means "this is a bot-check page".

If nothing matches, the script also tries JSON-LD (`"offers": {"price": ...}`) and
`<meta itemprop="price">`. When the log says `price not found — selectors ... may need
updating`, open the product page in your browser. Right-click the price, choose **Inspect**,
and add the new selector to the top of that site's list.

---

## ⚠️ Which sites block scraping?

These results come from local testing with plain `requests` (Oct 2026) and will vary by IP and over time:

| Site         | Result            | Notes |
|--------------|-------------------|-------|
| **Myntra**   | ✅ Worked          | Price comes from the inline `window.__myx` JSON (`"discounted"`). Myntra uses Akamai bot protection, so expect occasional 403s, especially from datacenter IPs. |
| **Flipkart** | ✅ Worked          | Worked well. Its obfuscated CSS class names (e.g. `Nx9bqj`) change every few months, which is why there are fallbacks. Dead or removed products return HTTP 500 "E002". Rate-limits with HTTP 529 under heavy traffic. |
| **Amazon.in**| ❌ Blocked (CAPTCHA)| Served the "Click the button below to continue shopping" / `validateCaptcha` page. Amazon blocks by IP reputation, and **GitHub Actions runs on Azure datacenter IPs, which Amazon blocks heavily**. Expect frequent misses. Sometimes it works on later runs. |
| **Ajio**     | ❌ Blocked (403)   | Akamai returns `Access Denied` (HTTP 403) to anything that doesn't look like a real browser (TLS fingerprinting). Plain `requests` is very unlikely to get through from any IP. |

What this means in practice:
- Blocked fetches are logged and skipped. `prices.json` keeps the last good price, so a
  block never causes a false alert.
- Free options if you really need Amazon/Ajio:
  1. Run `tracker.py` on your own PC or a Raspberry Pi (residential IP) with Windows Task
     Scheduler or cron, instead of GitHub Actions.
  2. Use a headless browser (Playwright, free) for those sites. It's heavier and slower, and
     Akamai may still block it.
- Scrape politely: 10 products every 5 minutes is already near the limit of what these sites tolerate.
  Check each site's Terms of Use. Automated scraping may break them.

## Files

```
.github/workflows/tracker.yml   # cron (*/5) + manual trigger, commits prices.json
tracker.py                      # the bot
products.json                   # what to track
prices.json                     # last known prices & alert state (auto-updated)
test_tracker.py                 # offline logic tests
requirements.txt
```
