# 🚨 Price Glitch Monitor

Every 5 minutes, scans **Amazon, Flipkart, Flipkart Minutes, Blinkit, Zepto, Swiggy Instamart and JioMart**
for **≥ 80 % price drops** and **pricing errors (≤ ₹1)** and sends them to **Telegram** (optionally WhatsApp) with a
**🛒 Buy Now** button.

```
🚨 80%+ PRICE DROP / GLITCH DETECTED!

📦 Product: Cashew Nuts Premium W320 1 kg
🏢 Platform: Zepto
💰 Glitch Price: ₹99 (MRP: ₹1,200)
🔥 Discount: 92% OFF

🔗 Buy Link: https://www.zepto.com/pn/.../pvid/...
            [ 🛒 Buy Now ]
```

---

## 1. Install (≈ 3 minutes)

Requires **Python 3.9+**. Google Chrome is used automatically if it's installed (harder to detect); otherwise the bundled Chromium is used.

**macOS / Linux**
```bash
./install.sh
```

**Windows**: double-click `install.bat`

**Manual install**
```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chromium
python run.py                      # first run starts the setup wizard
```

## 2. Setup wizard

`python run.py` (first run) or `python run.py --setup` at any time. It walks you through:

1. **Telegram**, either of:
   * **Bot (recommended, ~1 min).** Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and paste the token.
     The wizard checks it, asks you to press **Start** on your bot, **detects your chat automatically**, and sends
     `✅ Connected! Price glitch alerts will appear here.` To alert a group, add the bot to the group and send a message there.
   * **Your own account → Saved Messages** (Telethon/MTProto). Get `api_id`/`api_hash` from
     <https://my.telegram.org>, then enter your phone number and the login code Telegram sends you (plus your 2FA password if
     you have one). Accounts can't send buttons, so these alerts have a plain link instead.
2. **WhatsApp (optional)** via the free [CallMeBot](https://www.callmebot.com/blog/free-api-whatsapp-messages/) relay to your
   own number. Telegram is easier and better: instant delivery, a Buy button, no rate limits. WhatsApp has no free
   official API for personal numbers.
3. **Delivery location**: a pincode, an area name ("Indiranagar, Bengaluru") or `lat,lng`. It's geocoded via
   OpenStreetMap, so quick-commerce apps show your nearest dark store's prices.
4. **Platforms and rules.** Defaults: all platforms, ≥ 80 % or ≤ ₹1, MRP ≥ ₹99, every 5 min, 12 h dedupe.

Everything is saved to `config.json` with `chmod 600` permissions. Environment variables or a `.env` file can override
any value (see `.env.example`).

## 3. Log in / set your address once (recommended for quick commerce)

```bash
python run.py --login zepto
python run.py --login blinkit
python run.py --login instamart
python run.py --login flipkart_minutes
```

A normal browser window opens. Log in (OTP), **pick your delivery address**, solve any "are you human" check, then press
**Enter** in the terminal (or close the window). The profile is saved in `./sessions/<platform>/` and reused headlessly.

Without a login, each platform runs in **guest mode**: the browser gets your coordinates via geolocation and location
cookies (`gr_1_lat/lon` for Blinkit, `userLocation` for Swiggy, `nms_mgo_pincode` for JioMart, the Amazon pincode
widget), and it presses the site's *"Select location → Use current location"* buttons when they're shown.

## 4. Run

| Command | What it does |
|---|---|
| `python run.py` | Monitor forever, one scan every `interval_minutes` |
| `python run.py --once` | Single scan, then exit (good for cron) |
| `python run.py --once --dry-run --show-top 5` | Scan and print the biggest discounts per platform. Sends nothing and **writes nothing** to `deals.db` |
| `python run.py --platforms zepto,blinkit` | Limit to some platforms |
| `python run.py --headful` | Watch the browser work |
| `python run.py --debug` | Verbose logs; saves screenshots/HTML of blocked pages and raw JSON in `./debug/` |
| `python run.py --test-alert` | Send a sample alert |
| `python run.py --status` | Sessions, cooldowns, recent alerts |
| `python run.py --login <platform>` | Log in / set address / clear a bot check |
| `python run.py --reset-session <platform>` | Delete a saved browser profile |
| `python -m unittest discover -s tests -v` | Offline tests |

Logs go to `logs/monitor.log`, rotated automatically. `config.json` is re-read every cycle, so edits apply without a restart.

### Keep it running 24/7

* **macOS:** `caffeinate -i .venv/bin/python run.py` stops the Mac sleeping while it runs.
* **Linux (systemd):**
  ```ini
  # /etc/systemd/system/price-glitch.service
  [Service]
  WorkingDirectory=/home/you/price-glitch-monitor
  ExecStart=/home/you/price-glitch-monitor/.venv/bin/python run.py
  Restart=always
  User=you
  [Install]
  WantedBy=multi-user.target
  ```
  Then `sudo systemctl enable --now price-glitch`.
* **cron alternative:** `*/5 * * * * cd /path && .venv/bin/python run.py --once >> logs/cron.log 2>&1`

---

## How it works

```
run.py ── APScheduler (every 5 min, never overlapping)
   └─ core/monitor.py  run_cycle()
        ├─ core/browser.py   stealth Playwright, 1 persistent profile per platform, 4 platforms in parallel
        ├─ scrapers/*.py     deal pages + rotating auto-discovered category pages
        │     └─ core/extract.py   products from JSON API responses + embedded state + DOM cards
        ├─ core/detector.py  80 % / ≤ ₹1 rule + false-positive filters
        ├─ core/database.py  deals.db: alert dedupe, price history, cooldowns
        └─ core/notifier.py  Telegram bot / Telethon / WhatsApp
```

**Scanning feeds instead of search queries.** Each cycle visits the platform's deal hub and a few category
listings. Quick-commerce categories are auto-discovered from the home page (cached for 6 h) and **rotated**, so every
category gets covered over time while each cycle stays inside 5 minutes. Amazon uses its "80 % off or more" filtered
department listings; Flipkart uses the "70 % or more" discount facet, and the detector then applies the real 80 % rule.

**Extraction that survives redesigns.** Instead of fragile CSS selectors, every scraper:
1. captures the site's own **JSON API responses** and looks for any object holding both a selling-price field and an
   MRP field (`mrp`, `sellingPrice`, `discountedSellingPrice`, `offer_price`, `finalPrice`, …), then looks up the name, id,
   URL and stock around it;
2. reads embedded state (`__NEXT_DATA__`, `__INITIAL_STATE__`, …);
3. reads **product cards in the DOM**, finding the MRP by its **strikethrough styling** (every Indian store shows
   MRP that way) and falling back to "NN% off" badges.

**Stealth.** `playwright-stealth` plus an extra init script; real Chrome when available; no `--enable-automation`
flag; India locale and timezone; random viewport; user-agent rotation matched to the real browser version and OS
(a mismatched UA is itself a bot signal); mouse movement and scrolling; 2–5 s random delay between pages; fonts and
video blocked for speed. Logged-in profiles keep a stable fingerprint so sessions aren't invalidated.

Some stealth patches are themselves detectable. In testing, Swiggy's AWS WAF blocked every browser that had
playwright-stealth's `chrome_load_times`, `media_codecs` or `navigator_user_agent` patches, and let the same browser
through without them. Those three are switched off; the other 15 evasions stay on. Bot managers remember their verdict
in cookies, so a flagged guest profile gets its cookies wiped automatically and starts clean after the cooldown.
Each platform also has a hard time budget (`platform_timeout_s`, default 240 s), so one hung page can't stall a cycle.
When a **Cloudflare / DataDome / AWS WAF / Amazon robot check** appears, the platform is **paused with escalating
back-off** (15 → 30 → 60 … 240 min), and you get a Telegram note telling you to run `--login <platform>` and clear it
by hand. The tool never tries to solve CAPTCHAs.

### Default: glitch-only mode

Out of the box (`"alert_mode": "glitch"`) you're alerted **only** when an item costs **₹10 or less** with an MRP of
**₹99 or more**: real pricing errors such as ₹0, ₹1 or ₹9 on a ₹500 product. Ordinary "80 % off" deals are ignored,
so sellers who inflate the MRP and then "discount" it can't trigger an alert.

Before every alert the monitor **re-checks** the item: it opens the product page and confirms the main price shown
there. If the product page shows no readable price (Instamart, Zepto), it reloads the listing instead. Only confirmed
glitches are sent, and the alert says `✅ Price re-checked`. If one platform suddenly shows many "₹0" items at
once, only the top 5 are checked, since that's usually a parsing issue or a promo shelf.

Settings: `glitch_price_max` (default 10), `min_mrp` (default 99). `"alert_mode": "deals"` brings back the broader
80 %-off rules described below.

### Alert rules and false-positive filtering

An item alerts when `((mrp - price) / mrp) * 100 >= 80` **or** `price <= 1`, **and** it passes these checks:

| Filter | Why |
|---|---|
| In stock ("out of stock / sold out / notify me" or stock flags) | Can't buy it |
| MRP present, ≥ price, ≥ `min_mrp` (₹99), ≤ `max_mrp` | Missing or garbage MRPs; ₹12 → ₹2 isn't interesting |
| Blocklist (`free gift`, `sample`, `voucher`, …) | Freebies show up as ₹0 |
| **Permanent discount**: ≥ 3 sightings over ≥ 12 h at about the same price | The "80 % off" is an inflated MRP, not a drop |
| **MRP jump**: MRP suddenly 1.5× higher than before, while the price didn't fall | Seller inflated the MRP |
| **Strict marketplace mode** (Amazon, Flipkart) | See below |
| **Silent baseline**: the first scan of each page records its current deals without alerting (except ≤ ₹1 / ≥ 95 %) | Deals that were already sitting there aren't news. From then on you're alerted when a product is **new** or its **price falls** |

**Why strict mode exists.** In testing, Amazon's "80 % off" listings were full of items like a ₹199 adapter with an
"MRP" of ₹1,499. On marketplaces the seller types in the MRP, so a big percentage means little. By default Amazon
and Flipkart only alert on a **real glitch** (≤ ₹1 or ≥ 95 % off) or a **price drop the monitor has seen itself**
(≥ 20 % below the highest price recorded for that product; the alert then says "📉 Was ₹X here recently").
Quick-commerce MRPs are the printed legal MRP, so there 80 % applies from the first sighting. To get every 80 %+
listing, set `"marketplace_mode": "all"`.

**Why the baseline exists.** The first Blinkit test scan found 13 near-identical products at "84–87 % off"
(₹399, "MRP" ₹2,999) that had clearly been listed that way for a long time. Without a baseline, the first run and
every newly rotated category page would flood you with stale deals like these. The log shows what was held back each
cycle, e.g. `[amazon] 55 deal(s) at 80%+ held back: baseline 55`. To disable it, set `"baseline_first_scan": false`.

**Dedupe.** The same product at the same or a higher price is never re-alerted within 12 h. A further price drop
alerts immediately.

## Configuration reference (`config.json`)

```jsonc
{
  "notifier": { "mode": "bot", "bot_token": "…", "chat_id": "…", "whatsapp_enabled": false },
  "location": { "pincode": "560001", "latitude": 12.97, "longitude": 77.59, "label": "Bengaluru" },
  "monitor": {
    "interval_minutes": 5,
    "min_discount_pct": 80,
    "glitch_price_max": 1,
    "dedupe_hours": 12,
    "min_mrp": 99,
    "marketplace_mode": "strict",        // "all" = alert on every 80%+ Amazon/Flipkart listing
    "extreme_discount_pct": 95,
    "baseline_first_scan": true,
    "trusted_mrp_platforms": ["blinkit", "zepto", "instamart", "jiomart", "flipkart_minutes"],
    "inflated_mrp_check": true,
    "blocklist_keywords": ["free gift", "sample", "..."],
    "platforms": ["amazon", "flipkart", "flipkart_minutes", "blinkit", "zepto", "instamart", "jiomart"],
    "concurrency": 4,                    // platforms scanned in parallel (~300 MB RAM each)
    "max_category_pages": 3,             // category pages per platform per cycle (rotated)
    "platform_timeout_s": 240
  },
  "browser": { "headless": true, "min_delay": 2, "max_delay": 5, "scrolls": 4, "proxy": "" },
  "extra_urls": { "zepto": ["https://www.zepto.com/cn/..."] }   // add your own deal/category pages
}
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `BLOCKED … pausing N min` | `python run.py --login <platform>` and solve the check. If it keeps happening: `"headless": false`, a residential `proxy`, or a longer `interval_minutes`. |
| Instamart blocked | Swiggy uses AWS WAF. The guest profile is reset automatically after a block. If it persists, `python run.py --reset-session instamart`, then `--login instamart`. |
| Flipkart Minutes: 0 products, "closed for the day" | It shuts overnight (roughly 12 to 6 AM) and only serves some pincodes. The log says `store says: 'closed for the day'`. |
| 0 products on a quick-commerce app | The location isn't set, or the store is closed. Run `--login <platform>` and choose an address. Check with `--once --dry-run --show-top 5 --platforms <p>`. |
| A cycle takes longer than 5 min | The next one is skipped, never run on top. Lower `max_category_pages` or disable some platforms. |
| Wrong prices on quick commerce | The saved address in that profile differs from your config. Re-pick it with `--login`. |
| Want to see what the scraper sees | `--debug --headful`, then look in `./debug/<platform>/` |
| Telegram errors | `python run.py --test-alert`, or rerun `--setup`. |

## Responsible use

For personal deal-watching. Keep the default intervals and delays. The tool is deliberately low-volume: a handful of
page views per platform every 5 minutes. Automated access may be against a site's terms, so check them and use your
own judgement. Retailers often cancel orders placed at obvious pricing errors, so treat alerts as leads, not guarantees.

## Run free on GitHub Actions (no computer needed)

`.github/workflows/monitor.yml` runs `python run.py --once` on GitHub's servers every ~5–15 minutes
(GitHub delays scheduled runs when it's busy). `deals.db` is carried between runs in the Actions cache, so dedupe,
price history and baselines keep working.

1. Create a **public** repo on GitHub. Actions minutes are unlimited for public repos; private repos get 2,000
   free minutes a month, which lasts about 3 days at this schedule. Push this folder to it.
   `config.json`, `sessions/`, `.env` and `deals.db` are git-ignored, so no secrets get uploaded.
2. On your computer, run `python run.py --export-cloud-secrets`. It prints the values to add under the repo's
   **Settings → Secrets and variables → Actions → New repository secret**, one secret per name. Secrets are encrypted
   and never shown in logs.
3. Open the **Actions** tab, enable workflows, and press **Run workflow** once to test.

Notes:
* Personal-account mode stores `TELEGRAM_SESSION`, which gives **full access to your Telegram account**. It's only
  as safe as your GitHub account (use 2FA). A bot token is lower risk: rerun `--setup` and pick "Telegram bot".
* GitHub servers use datacenter IPs. Blinkit, Zepto or Instamart may block them more often; the log shows
  `BLOCKED`, and the platform backs off automatically. `--login` sessions aren't used in the cloud; every run is a guest.
* GitHub pauses scheduled workflows after **60 days without a commit** to the repo. When that happens you get an
  email; click "Enable workflow" or push any commit.
