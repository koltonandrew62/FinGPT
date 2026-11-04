# ATLAS

**A voice-driven personal AI assistant that runs on your own computer.**

ATLAS lives in a browser tab as a heads-up display. You talk to it (or type), and it can brief you on the
news, look things up on the live web, dig into live market, company, crypto, economic and sports data, check
the weather, read and send your email, book restaurant
tables and fill in websites in its own browser window, read your calendar and keep a to-do list, send texts and
start calls, control your smart home, plan errands and trips, keep notes and reminders, remember things about
you, and operate your PC: opening apps, controlling media, finding files, running commands. Anything risky
waits for your approval first.

The thinking is done by the AI model you choose through [OpenRouter](https://openrouter.ai). The server,
your settings and your personal data all stay on your machine.

**Contents:** [Features](#what-atlas-can-do) · [Quick start](#quick-start) · [First run](#first-run) ·
[Things to say](#things-to-say) · [Live data](#live-data-sources) · [Email](#email-setup) · [Your details](#your-details-phone--addresses) ·
[Reservations & web tasks](#reservations--web-tasks) · [Calendar & to-dos](#calendar--to-dos) ·
[Texts & calls](#texts--calls) · [Smart home](#smart-home-home-assistant) · [Errands](#errands--travel) ·
[Approvals](#how-approvals-work) · [Configuration](#configuration-reference) ·
[Models & cost](#choosing-a-model--cost) · [Privacy](#privacy--security) · [Voice](#voice-tips) ·
[Troubleshooting](#troubleshooting) · [Layout](#project-layout)

---

## What ATLAS can do

| Area | What it does | Powered by |
|---|---|---|
| **Live news** | Headlines by section (top, world, US, business, technology, science, health, sports, entertainment) or on any topic you name | Google News, with BBC, NPR, CNBC and Ars Technica as backups, plus Hacker News |
| **Web search** | Searches the live web and answers with cited sources. It can also read a full article from a link | OpenRouter's web plugin, falling back to DuckDuckGo |
| **Encyclopedia** | Quick summaries of people, places, organisations and events | Wikipedia |
| **Markets** | Stocks, ETFs, indices, crypto, currencies and futures: price, change over 1 day to 5 years, ticker lookup. Top gainers, losers, most active and trending tickers. Technicals (moving averages, RSI, MACD, volatility, support and resistance) | Yahoo Finance |
| **Company research** | One stock in depth: valuation (P/E, EPS, PEG, P/B), dividend, margins and growth, analyst consensus and price targets, next earnings date, pre-market and after-hours prices, ticker news. Reported quarterly and annual results, SEC filings with document links, and the earnings calendar | Yahoo Finance, SEC EDGAR, Nasdaq |
| **Crypto** | Prices, 1h/24h/7d change, market cap, rank, total market cap, BTC dominance and trending coins, 24/7 | CoinGecko, with Yahoo Finance as a backup |
| **Economy & rates** | CPI and PCE inflation, unemployment, payrolls, Fed funds, GDP, mortgage rates, jobless claims and more. The Treasury yield curve and whether it's inverted. CNN's Fear & Greed index and the VIX | FRED (St. Louis Fed), U.S. Treasury, CNN, Yahoo Finance |
| **Sports** | Live scores and results, schedules, standings and news for the NFL, NBA, WNBA, MLB, NHL, college football and basketball, MLS, NWSL, the Premier League and other top soccer leagues, the Champions League, F1, golf and UFC | ESPN |
| **Weather** | Current conditions and a forecast of up to 7 days, for home or any city | Open-Meteo (no key needed) |
| **Email** | List, search and read your inbox. Writes and sends email after you approve the draft | Your own mailbox over IMAP/SMTP |
| **Reservations & web tasks** | Books restaurant tables and operates websites for you (forms, orders, account pages) in its own browser window. Final Book / Pay / Submit clicks wait for your approval | Chrome or Edge, driven by Playwright |
| **Calendar & to-dos** | Reads your Google, iCloud or Outlook calendars, checks when you're free, adds events (you click Save), keeps a to-do list | Your calendars' secret iCal links |
| **Texts & calls** | Sends texts to numbers or saved contacts. Starts calls in your calling app | Twilio, or Messages on a Mac |
| **Smart home** | Device status and control: lights, switches, climate, media, scenes, locks and more. Security-relevant actions ask first | Home Assistant |
| **Errands & travel** | Directions with time estimates, nearby places, Uber/Lyft, food delivery, package tracking, currency conversion | OpenStreetMap, Google Maps links, Yahoo Finance |
| **PC control** | Open and close apps, open websites, play music or video, play/pause/skip, volume, find and open files, read/write text files, clipboard, type text and press shortcuts, run shell commands, take screenshots, **"look at my screen"**, lock the screen, sleep/restart/shut down | Your operating system |
| **Notes & contacts** | A notebook you can add to and search. Contacts with email and phone, so you can say "email Mom" or "text Mom" | Stored locally in `data/` |
| **Reminders** | Timers and reminders, announced aloud with a chime and a desktop notification | Stored locally in `data/` |
| **Long-term memory** | Remembers preferences and facts you share, and uses them to personalise answers. You can review or delete them in Settings | Stored locally in `data/` |
| **Voice** | Speak instead of typing, with an optional wake word (the assistant's name). Replies are read aloud sentence by sentence as they arrive | Your browser's speech engine |
| **Utilities** | Exact math (percentages, compound growth, etc.) and a world clock | Built in |

The HUD also has live panels: weather, system telemetry (CPU, memory, disk, battery), upcoming reminders,
a news feed with category tabs, a scrolling market ticker, and a running count of this session's tokens and cost.
One-tap chips cover the everyday requests: **Daily briefing**, **Headlines**, **Markets**, **Inbox** and **System check**.

Email, the calendar, texting and smart home are optional: each one switches on once you add its settings.
The live market, economic and sports data needs no setup. See [Live data sources](#live-data-sources).

---

## Quick start

**You'll need:**
- **Python 3.10 or newer**
- **An OpenRouter API key** with a little credit on it. Create one at [openrouter.ai/keys](https://openrouter.ai/keys).
- **Chrome or Edge.** Needed for voice (other browsers work for typing), and ATLAS's own browser window uses
  whichever one is installed for bookings and web tasks.

### Easiest: install with a Desktop shortcut

The installer downloads ATLAS, sets everything up, asks what you'd like to call your assistant, adds a
**Desktop shortcut** (and a Start-menu / Applications entry) with the ATLAS icon, and opens it. ATLAS then runs
in its own app window — no browser tabs — and shuts down when you close the window. Run the installer again any
time to update; your settings and data are kept.

**Windows** — open PowerShell (Start menu → type *PowerShell*) and paste:

```powershell
irm https://raw.githubusercontent.com/koltonandrew62/FinGPT/refs/heads/claude/eloquent-einstein-uqi56u/atlas/installer/install-windows.ps1 | iex
```

If Python isn't installed, the installer installs it with `winget`. Already downloaded the ZIP? Double-click
`atlas\installer\Install ATLAS (Windows).bat` instead.

**macOS / Linux** — open Terminal and paste:

```bash
curl -fsSL https://raw.githubusercontent.com/koltonandrew62/FinGPT/refs/heads/claude/eloquent-einstein-uqi56u/atlas/installer/install-mac-linux.sh | bash
```

Where things go: Windows `%LOCALAPPDATA%\ATLAS`; macOS `~/Applications/ATLAS-files` (app in `~/Applications`);
Linux `~/.local/share/atlas`. Server logs are in `data/atlas.log` there. To keep ATLAS running after you close
its window (so reminders still fire), set `ATLAS_KEEP_RUNNING=true` in `.env`.

### Windows

1. Install Python from [python.org/downloads](https://www.python.org/downloads/). On the installer's first
   screen, tick **"Add python.exe to PATH"**.
2. Open the `atlas` folder and **double-click `start.bat`**.
3. The first run takes a few minutes while it sets up. After that, your browser opens ATLAS at
   `http://127.0.0.1:8765/`.

Keep the black window open while you use ATLAS. To shut it down, press **Ctrl+C** in that window or close it.
If Windows shows a security warning about `start.bat`, choose **Run** (or **More info → Run anyway**). It's the
launcher in this folder.

### macOS / Linux

```bash
cd atlas
./start.sh
```

If you get "permission denied", run `chmod +x start.sh` once, or start it with `sh start.sh`.

- **macOS:** get Python with `brew install python` or the installer from python.org.
- **Debian/Ubuntu:** `sudo apt install python3 python3-venv`
- **Fedora:** `sudo dnf install python3`
- Have several Pythons? Pick one with `ATLAS_PYTHON=/path/to/python3.12 ./start.sh`.
- Don't want a browser window to open? Use `ATLAS_OPEN_BROWSER=false ./start.sh`.

### What the launchers do

1. Find Python 3.10+. On Windows the `py` launcher is tried first, then `python`.
2. Create a private virtual environment in `.venv`, which leaves your system Python untouched.
3. Install `requirements.txt` (which includes Playwright and the calendar libraries), but only on the first run
   or after the file changes. That keeps every later start fast, and after an update the new requirements are
   installed automatically.
4. Try the optional `requirements-extras.txt`, which lets ATLAS type and press shortcuts for you. If that
   fails, ATLAS still starts and only those two abilities are missing.
5. Start ATLAS with `python run.py`.

The launchers don't download a browser: ATLAS uses your installed Chrome or Edge. Only if you have neither,
see [Reservations & web tasks](#reservations--web-tasks).

If something gets into a bad state, delete the `.venv` folder. The next start rebuilds it from scratch.

### Manual start

```bash
cd atlas
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-extras.txt   # optional: typing and keyboard shortcuts
python -m playwright install chromium    # only if neither Chrome nor Edge is installed
python run.py
```

---

## First run

1. **Click the start button** on the intro screen. Browsers only let a page use the microphone and speak
   after you've clicked on it. ATLAS also asks permission to show notifications, which it uses for reminders.
2. **Paste your OpenRouter key** on the setup screen. You can also give your name and home city here.
   ATLAS saves these to `atlas/.env`.

   If you'd rather do it by hand, copy the example file and edit it **before** starting:

   ```bash
   cp .env.example .env        # Windows: copy .env.example .env
   ```

   Replace the placeholder `sk-or-v1-your-key-here` with your real key. If you leave the placeholder in,
   ATLAS assumes a key is set and you'll see *"OpenRouter rejected the API key"*. You can fix that in Settings.
3. **Say hello.** Then open **Settings** (the gear icon) and fill in how ATLAS should address you, your
   units and news region, your model and voice, and your email if you want it.
4. **Optional extras.** Add [your phone and addresses](#your-details-phone--addresses), then whichever of
   [calendar](#calendar--to-dos), [texting](#texts--calls) and [smart home](#smart-home-home-assistant) you want.

---

## Things to say

**Briefings & news**
- "Give me my daily briefing." (or tap the chip)
- "What's happening in the world today?"
- "Any news on the Federal Reserve this week?"
- "What's trending on Hacker News?"

**Research**
- "Search the web for the latest jobs report and give me the key numbers."
- "Who was Ada Lovelace?"
- "Read this article and summarise it: https://…"
- "What's 15% of 2,340?" · "What will $10,000 be worth at 7% for 20 years?"
- "What time is it in Tokyo?"

**Markets & weather**
- "How are the markets doing?"
- "How has Nvidia done over the past month compared with the Nasdaq?"
- "What's the ticker for Vanguard Total Stock Market?"
- "Will I need an umbrella this week?" · "What's the weather in Chicago this weekend?"

**Stocks, crypto & the economy**
- "How's Nvidia looking?" · "What's Apple's P/E and when do they report next?"
- "What are today's top gainers?" · "What's trending on the market?" · "Why is Tesla moving today?"
- "Is the S&P 500 above its 200-day moving average?" · "What's Microsoft's RSI?"
- "Apple's last 10-Q." · "Show me Amazon's revenue for the last eight quarters."
- "Who reports earnings this week?"
- "What's Bitcoin at?" · "Top 5 coins."
- "What's the fear and greed index?" · "Is the yield curve still inverted?"
- "Latest CPI?" · "Give me an economic dashboard." · "What's the unemployment rate?"

**Sports**
- "Who won the Lakers game?" · "What's the score of the Chiefs game?"
- "When do the Yankees play next?" · "Premier League table."
- "Who won the last F1 race?" · "Any NBA trade news?"

**Email**
- "Do I have any unread email?"
- "Read me the latest email from Sam, then draft a polite reply saying Thursday works."
- "Save Mom's email address as mom@example.com." Then: "Email Mom that I'll be late for dinner."

**Reservations & websites**
- "Book a table for two at an Italian place near me Friday at 7."
- "Find a sushi place in Brooklyn for four tomorrow at 8, then put it on my calendar."
- "Open OpenTable in your browser so I can sign in."
- "Order pad thai on DoorDash." (the final *Place order* click asks first)

**Calendar & to-dos**
- "What's on my calendar tomorrow?"
- "Am I free Thursday at 3?" · "When's my dentist appointment?"
- "Put lunch with Sam on Friday at 12:30 on my calendar."
- "Add 'renew passport' to my to-do list." · "What's on my to-do list?"

**Texts & calls**
- "Text Mom I'm running late."
- "Save Mom's number as (555) 555-0123."
- "Call the dentist." (opens your calling app; you press Call)

**Smart home**
- "Turn off the living room lights."
- "Set the thermostat to 70." · "Is the front door locked?"
- "Lock the front door." (asks first)

**Errands & travel**
- "How long to drive to work?"
- "Find a pharmacy near me." · "Directions to the airport."
- "Get me an Uber to the airport."
- "Track this package: 1Z…" · "Where are my packages?"
- "How much is 250 euros in dollars?"

**Your computer**
- "Open Spotify." · "Play some lo-fi beats on YouTube." · "Pause the music." · "Set the volume to 30%."
- "Find my resume in Downloads and open it."
- "Save this grocery list to a file on my Desktop."
- "Copy that to my clipboard."
- "Look at my screen. What does this error mean?" · "Take a screenshot."
- "What's my IP address?" (runs a command after you approve it)
- "How's my computer doing?"
- "Lock my computer." · "Put the PC to sleep."

**Personal**
- "Remind me in 20 minutes to take the laundry out."
- "Remind me tomorrow at 9 to call the dentist."
- "Make a note: gift ideas, a book for Dad and a scarf for Maya."
- "Remember that I prefer metric units." · "What do you know about me?"

---

## Live data sources

ATLAS answers market, company, crypto, economy and sports questions from live data rather than from the
model's memory, and says where each figure came from and how current it is. **No setup or account is needed.**
Three optional settings raise limits or keep a source happy (see the table and the
[configuration reference](#configuration-reference)).

| Source | What ATLAS gets from it | Key needed? | Reliability |
|---|---|---|---|
| **Yahoo Finance** | Quotes and price history; one ticker in depth (valuation, dividend, financial ratios, analyst targets, earnings dates, pre-market and after-hours); top gainers, losers, most active and trending; ticker news; the daily bars behind technicals; Treasury yield and VIX quotes | No | **Unofficial** endpoints (the ones finance.yahoo.com uses). Best-effort: Yahoo can rate-limit or, in the EU, ask for cookie consent. ATLAS then falls back to its basic chart data and backs off for a while. Equity quotes may be **delayed about 15 minutes** |
| **U.S. Treasury** | The official daily par yield curve (1 month to 30 years), used to fill gaps and on request | No | Official (home.treasury.gov) |
| **CoinGecko** | Crypto prices, 1h/24h/7d change, market cap, volume, rank, total market cap, BTC/ETH dominance, trending coins | Optional: `COINGECKO_API_KEY` (free "demo" key) for about 30 calls a minute | Official API. Keyless limits are tight; when CoinGecko is busy, major coins fall back to Yahoo Finance |
| **SEC EDGAR** | Filings (10-K, 10-Q, 8-K, Form 4, proxies, 13D/13G…) with direct document links; reported revenue, net income, EPS and operating cash flow for the last 8 quarters and 3 fiscal years | No, but set `SEC_CONTACT_EMAIL` | Official. SEC's fair-access policy asks for a contact email in each request, and it may refuse requests without one. ATLAS stays under SEC's 10-requests-a-second limit |
| **Nasdaq** | The earnings calendar: who reports on a day or week, before or after the bell, EPS forecasts | No | **Unofficial** endpoint, best-effort |
| **CNN** | The Fear & Greed index: score, rating, a week, month and year ago, and its seven components | No | **Unofficial** endpoint. CNN sometimes refuses automated requests; the VIX is still returned |
| **FRED** (St. Louis Fed) | US inflation (CPI, core CPI, PCE, core PCE), unemployment, payrolls, Fed funds and target range, GDP growth, 10-year Treasury, 30-year mortgage, consumer sentiment, jobless claims, retail sales, housing starts, or any FRED series id | Optional: `FRED_API_KEY` (free) | Official. Without a key ATLAS uses FRED's public CSV downloads, which give the same figures. Macro data is released with a lag, and ATLAS says which period a figure covers |
| **ESPN** | Scores, game status and live situation, schedules, standings and news for the leagues listed under [Features](#what-atlas-can-do) | No | **Unofficial** public site API, best-effort. Live scores can lag the broadcast by about a minute |
| **Google News** | Backup for stock and sports news when Yahoo or ESPN has nothing | No | Public RSS feeds |

**Good to know**
- ATLAS gives facts and context, not personal buy or sell recommendations.
- Results are cached briefly (30 seconds for live quotes and scores, up to an hour or more for slow-moving data
  such as economic releases and filings), and each source is rate-limited, so repeated questions don't hammer anyone. "Unofficial" endpoints can change without
  notice; if one stops working, ATLAS says which source failed and carries on with the others.
- Technicals are worked out on your computer from daily prices. Their returns are total returns
  (dividends included), so they can differ slightly from the price-only change Yahoo shows.
- Game times are shown in your computer's time zone, or in `SPORTS_TIMEZONE` if you set one.
- For the HUD, the server also offers two panel feeds: `GET /api/market_pulse` (top movers and trending
  tickers, crypto and sentiment, each part reporting its own errors) and `GET /api/sports` (today's scores for
  the leagues in `SPORTS_PANEL_LEAGUES`). Like every API route, they need the page's access token.

---

## Email setup

ATLAS signs in to your mailbox with your address and an **app password**, a separate password made just for
apps. **Your normal password won't work** with most providers.

Open **Settings → Email**, enter your address and the app password, and save. ATLAS works out the mail
servers from your address for Gmail, Outlook/Hotmail/Live, Yahoo, iCloud and AOL.

### Gmail

1. Turn on **2-Step Verification** for your Google account. App passwords require it.
2. Go to **[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)**, create an app
   password (name it "ATLAS") and copy the 16-character code.
3. Paste it into the **App password** field. The spaces Google shows are fine to include.

Gmail search syntax works when you ask about mail, as in "emails from amazon newer than 7 days".
If reading fails, check that IMAP is on in Gmail → Settings → *Forwarding and POP/IMAP*; newer accounts have it
always on. Some work or school accounts and accounts under Advanced Protection can't create app passwords.

### iCloud, Yahoo, AOL

All three require two-factor authentication and an **app-specific password**:
- **iCloud:** [account.apple.com](https://account.apple.com) → Sign-In and Security → App-Specific Passwords. Use your `@icloud.com` (or `@me.com`) address.
- **Yahoo:** Account Info → Account Security → Generate app password.
- **AOL:** Account Info → Account Security → Generate app password.

### Outlook / Hotmail / Live

Microsoft has been retiring password-based IMAP/SMTP sign-in for personal Outlook.com accounts in favour of
OAuth, which ATLAS doesn't support. **It may not work at all.** If sign-in is rejected, use another account,
such as Gmail or iCloud.

### Other providers & custom domains

For other addresses ATLAS guesses `smtp.<your-domain>` on port 465 and `imap.<your-domain>` on port 993.
If that's wrong, fill in the **Advanced** fields in Settings (or `SMTP_HOST`, `SMTP_PORT`, `IMAP_HOST`,
`IMAP_PORT` in `.env`) with the values your provider publishes:

- SMTP port **465** uses SSL. Any other SMTP port (usually **587**) uses STARTTLS.
- IMAP always uses SSL/TLS, usually on port **993**. Plain IMAP on port 143 isn't supported.
- Example for an iCloud+ custom domain: `smtp.mail.me.com` / `587`, `imap.mail.me.com` / `993`.

Leaving the password field blank in Settings keeps the current one.

---

## Your details (phone & addresses)

Three optional settings make bookings and errands smoother. Add them in `.env` (or in Settings, where the
drawer has a field for them):

| Key | Used for |
|---|---|
| `USER_PHONE` | Restaurant bookings and other forms that ask for a phone number. If it's blank, ATLAS asks when a booking needs it. |
| `HOME_ADDRESS` | The default starting point for directions, and what "home" means in "directions home" or "an Uber home". |
| `WORK_ADDRESS` | What "work" means, as in "How long to drive to work?" |

Your name and email address for bookings come from `USER_NAME` and `EMAIL_ADDRESS`. If an address contains
`#` (as in `Apt #4`), wrap the whole value in double quotes in `.env`.

---

## Reservations & web tasks

ATLAS has its **own browser window**: a real Chrome or Edge window that it operates step by step. It reads
the page, clicks, picks dates and time slots, and fills in forms. You can watch it work, and step in at any time.

### Setup

- **Nothing to install if you have Google Chrome or Microsoft Edge.** ATLAS tries Chrome, then Edge, then
  Playwright's own Chromium.
- **Neither installed?** Download Playwright's Chromium once:
  `.venv/bin/python -m playwright install chromium`
  (Windows: `.venv\Scripts\python -m playwright install chromium`).
  On Linux it may also need system libraries: add `--with-deps`, which asks for your password.
- **Sign in once.** The window uses its own profile in `data/browser-profile`, separate from your everyday
  browser, and keeps you signed in between runs. Say *"Open OpenTable in your browser so I can sign in"*, then
  sign in to OpenTable, Resy, Google (for *Reserve with Google*) and any shops you want ATLAS to use.

### How a booking goes

1. *"Book a table for two at an Italian place near me Friday at 7."* ATLAS checks your calendar is free, then
   opens an OpenTable search with the date, time and party size filled in. If the restaurant isn't on
   OpenTable, it tries Resy, Google Maps, Yelp or the restaurant's own site. "Near me" means your home city.
2. It picks the time slot closest to what you asked for and fills in your name, phone and email from your
   details and memory. It asks only for what's missing.
3. **The final click waits for your approval.** *Complete reservation*, *Book*, *Reserve*, *Confirm*, *Pay*,
   *Place order*, *Submit*, *Send* and similar buttons show a card with the site, the page title, the button,
   why it's asking, what was filled in and an excerpt of the page. Approve it and ATLAS clicks.
4. It reads the confirmation page and tells you the restaurant, date, time, party size and confirmation number,
   then offers to add it to your calendar and set a reminder.

If a restaurant has no online booking, ATLAS gives you its phone number and offers to call or email it.

### What ATLAS won't do in its browser

- **It never types passwords, card numbers or security codes.** Those fields are refused. When a site needs
  you to sign in, enter a card for a deposit, type a 2FA code or solve a CAPTCHA, ATLAS asks you to do that
  part in its window, then carries on.
- It only opens public websites, not pages on your own computer or home network.
- It never says yes to a site's pop-up "Are you sure?" or input prompts. It dismisses them and tells you.
- Picking a time slot, searching and following ordinary links don't need approval, because they commit nothing.
  The booking itself only happens on the final click you approve.

The same window handles other web tasks: filling in forms, checking an order or account page, reading a
parcel's tracking page, or finishing a food order.

### Browser options

| Key | What it does |
|---|---|
| `BROWSER_CHANNEL` | Prefer one installed browser: `chrome` or `msedge` (Playwright channel names such as `chrome-beta` work too). Blank tries Chrome, then Edge, then Playwright's Chromium. |
| `BROWSER_EXECUTABLE` | Full path to a Chromium-based browser to use instead, e.g. `/usr/bin/chromium`. On macOS give the program inside the app: `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`. `~` and `%VARIABLES%` are expanded. Overrides `BROWSER_CHANNEL`. |
| `BROWSER_HEADLESS` | `true` runs the browser with no window. That's only worth doing on a computer with no screen: you can't watch, sign in or solve a CAPTCHA in a window you can't see. Default `false`. |
| `BROWSER_VISION_MODEL` | The model ATLAS uses when it takes a screenshot to work out a date picker, seating map or other visual widget. It must accept images. Blank uses the main model. |

Changes apply the next time ATLAS opens its browser window. Say "close your browser" to close it.

---

## Calendar & to-dos

ATLAS **reads** your calendars through their **secret iCal (ICS) addresses**, which are read-only links your
calendar provider gives you. It **adds** events by opening a pre-filled "new event" page that you save.

### Connect your calendars

Put the link in `CALENDAR_ICS_URLS`. For several calendars, separate them with commas. You can name each one,
as in `Work=https://…,Family=https://…`.

- **Google Calendar:** on a computer, open [calendar.google.com](https://calendar.google.com) → ⚙ **Settings**
  → under *Settings for my calendars* pick the calendar → **Integrate calendar** → copy
  **Secret address in iCal format**. Some work or school accounts have this turned off by their admin.
- **iCloud:** in Calendar on a Mac, or at [icloud.com/calendar](https://www.icloud.com/calendar), share the
  calendar, tick **Public Calendar** and copy the link. It starts with `webcal://`, which ATLAS accepts as-is.
  This link is public: anyone who has it can view that calendar.
- **Outlook.com / Microsoft 365:** in Outlook on the web, go to **Settings → Calendar → Shared calendars →
  Publish a calendar**. Choose the calendar and *Can view all details*, click **Publish** and copy the **ICS** link.

**Treat these links like passwords**: anyone with one can read that calendar. ATLAS stores them as secrets
(see [Privacy](#privacy--security)). If a link leaks, reset or unpublish it in your calendar's settings.

ATLAS re-downloads each calendar at most every 5 minutes, expands recurring events and shows times in your
computer's time zone. Events from these calendars are read-only. To change or delete them, use your calendar app.

### Adding events

ATLAS can't write to Google or Outlook directly. Instead, `CALENDAR_PROVIDER` decides where a new event goes:

| Value | What happens |
|---|---|
| `google` *(default)* | Google Calendar opens in your browser with the event (and any invitees) filled in. **Click Save.** |
| `outlook` | The Outlook.com new-event page opens, filled in. **Click Save.** Add invitees there. |
| `office365` | The same, for work or school Microsoft 365 accounts (outlook.office.com). |
| `ics` | ATLAS writes an `.ics` file and opens it in your default calendar app, such as Apple Calendar or desktop Outlook. **Click Add or Save.** |
| `local` | The event stays in ATLAS's own calendar only. |

ATLAS also keeps its own copy of every event it adds, so it shows up in ATLAS's answers straight away. Saving
it in your calendar app is up to you.

### Availability

Before suggesting or booking a time, ATLAS checks your calendar. It reports clashes, the free slots that day
and the nearest free alternatives. Your usual day is `CALENDAR_DAY_HOURS` (default `08:00-20:00`). When you ask
about a time outside those hours, such as a late dinner, ATLAS widens the search to include it.

### To-do list

Add, list, complete and delete to-dos, with an optional due date and priority (low, normal, high). Overdue
items are flagged. For an alert at a set time, ATLAS uses a reminder instead (or as well).

---

## Texts & calls

### Texting

ATLAS sends texts through **Twilio** when it's set up. Otherwise, **on a Mac**, it sends them as iMessages
through the Messages app with no setup. Every text shows an approval card first, with the recipient, the
number, how it will be sent and the full message.

**Twilio setup**

1. Create an account at [twilio.com](https://www.twilio.com) and get a Twilio phone number that can send SMS.
2. From the **Twilio Console home page**, copy the **Account SID** (34 characters, starts with `AC`) and the
   **Auth Token**.
3. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_FROM_NUMBER` (your Twilio number, such as
   `+15555550123`). You need all three. A Messaging Service SID (`MG…`) also works as the sender.

**Trial accounts can only text numbers you've verified.** Add each one in the Twilio Console under
*Phone Numbers → Manage → Verified Caller IDs*, or upgrade the account. Trial texts also start with a short
Twilio notice. To text other countries, enable them under *Messaging → Settings → Geo permissions*. For texts to
US numbers from a paid account, Twilio may also ask you to register how you'll use the number. Follow the
Console's prompts.

**The Mac fallback** needs Messages signed in to iMessage, and macOS asks once for **Automation** permission.
People who don't use iMessage, such as Android users, won't get these texts. Use Twilio for them.

Without Twilio, Windows and Linux can't send texts.

### Calls

*"Call the dentist"* opens your computer's calling app with the number filled in: **Phone Link**, Teams or
Skype on Windows, **FaceTime through your iPhone** on a Mac, or your desktop's phone-link handler on Linux.
You press Call and talk on your own device. **ATLAS can't speak or listen on calls.** Calls also show an
approval card first.

### Numbers and contacts

- Say a saved contact's name or a number. If a contact has no phone number, ATLAS asks and can save it.
- Numbers without a leading `+` are read as numbers in `DEFAULT_COUNTRY_CODE` (default `+1`, US/Canada).
  Set it to `+44`, `+61` and so on if you're elsewhere.
- **Emergency and other short service numbers are refused.** Dial those yourself, directly from your phone.

---

## Smart home (Home Assistant)

ATLAS controls your devices through [Home Assistant](https://www.home-assistant.io), which you run on your
own network.

1. **`HOME_ASSISTANT_URL`**: the address you open Home Assistant at, such as `http://homeassistant.local:8123`
   or `http://192.168.1.20:8123`. A remote `https://` address works too.
2. **`HOME_ASSISTANT_TOKEN`**: in Home Assistant, click your name at the bottom left, open the **Security**
   tab, and under **Long-lived access tokens** choose **Create token**. Copy it straight away, because Home
   Assistant shows it only once.

ATLAS looks up the exact device before acting, so *"Turn off the living room lights"* finds the right light.

**Everyday actions run straight away:** lights, switches, fans, climate, media players, scenes, scripts,
vacuums and similar.

**These always show an approval card first:**
- Locks (locking as well as unlocking), alarm panels, covers such as garage doors and gates (and blinds; only
  *stop* skips the card), valves, sirens and cameras.
- Any unlock, disarm or open action.
- Switches, buttons, scripts, scenes and helpers whose id or name points at a garage, gate, door, lock, alarm,
  valve, buzzer or intercom (ATLAS checks names live in Home Assistant), and ones that look like a factory reset
  or firmware flash.
- "All devices" commands, except for lights, media players, fans, climate, humidifiers and vacuums.
- Anything beyond device control: notifications, announcements, automations, updates, groups, shell or web
  commands, and restarting Home Assistant.
- Anything on your own list in **`HOME_ASSISTANT_CONFIRM_DOMAINS`**. List domains (`light`), services
  (`climate.set_temperature`) or devices (`switch.space_heater`), separated by commas, or `*` to approve every
  smart-home action.

If ATLAS can't check what a device is connected to, for example because Home Assistant didn't answer, it asks.
Codes and PINs are masked on the card.

---

## Errands & travel

| Ask for | What happens |
|---|---|
| **Directions** | Google Maps opens with the route filled in, and ATLAS estimates distance and time for driving, walking or cycling from OpenStreetMap routing (OSRM). **Estimates use typical speeds, with no live traffic.** Transit times are only on the Maps page. The trip starts from your home address, then your home city, then your current location. |
| **Places** | Real places by type or name ("pharmacy", "Costco") near your home city or anywhere you say, with address and a map link. OpenStreetMap has no ratings, reviews or opening hours, so ATLAS searches the web for those. |
| **Rides** | Uber opens with the drop-off (and pickup) filled in, or lyft.com opens for Lyft. **You choose the ride and confirm it in the app.** ATLAS can't request or pay for rides. |
| **Food delivery** | DoorDash, Uber Eats or Grubhub search results for a dish or restaurant. ATLAS can also carry on in its own browser, where the final *Place order* click waits for your approval. |
| **Packages** | Spots the carrier from the tracking number (UPS, USPS, FedEx, DHL Express, Amazon) and opens the official tracking page. For other carriers, name them and ATLAS searches for their tracking page. ATLAS remembers your numbers, with a label if you give one, so you can ask "where are my packages?" To read the status, it opens the page in its browser. |
| **Currency** | Converts at the current market rate (Yahoo Finance, mid-market), including crypto. Banks and cards add a spread on top. |

Set [your home and work addresses](#your-details-phone--addresses) so "home" and "work" work door to door.

---

## How approvals work

Some actions are hard to undo, so ATLAS **always shows an Approve / Deny card first** for these:

| Tool | What the card shows you |
|---|---|
| `send_email` | Recipients, subject and the full message |
| `send_text_message` | The recipient, the number, how it's sent (Twilio or Messages) and the full message |
| `make_phone_call` | Who, the number (and any extension), and which app will open |
| `run_command` | The exact command, and the folder if one was given |
| `read_file` | The file path. Its contents will be sent to the AI model |
| `write_file` | The path, overwrite or append, and the content |
| `close_application` | The app. Unsaved work may be lost |
| `power_action` | Shut down, restart, sleep or log out |
| `type_text` | The text it will type into the focused window |
| `press_keys` | The key combination |

**Some tools ask only when a particular action is risky.** ATLAS checks each call just before it runs:

| Tool | Asks when | The card shows |
|---|---|---|
| `browser_click` | The button or link looks final: book, reserve, confirm, pay, buy, checkout, place order, submit, send, sign up, subscribe, delete, transfer, donate, post… or it submits a booking, order or payment form | Site, page, the button, why it's asking, the values filled in (card numbers masked) and a page excerpt |
| `browser_type` (with Enter), `browser_press` (Enter or Space) | The key would submit such a form or send a message | The same |
| `smart_home_control` | The action is [security-relevant](#smart-home-home-assistant) | The devices, the action, its settings (codes masked) and why it's asking |

An approval covers that exact button or device action. If the page changes, or ATLAS picks a different
element, it asks again. If it can't inspect the action in time, it asks rather than guessing.

- Click **Approve** or **Deny**, or say *"yes / approve / confirm / send it / do it / go ahead"* or
  *"no / deny / cancel / stop"* while a card is waiting.
- If you don't answer within **5 minutes**, the action is automatically denied.
- When you deny something, ATLAS won't retry it unless you ask.

Everything else runs without a card, because it only reads information or is easy to reverse. That covers
opening apps, sites and files, media and volume, the clipboard, screenshots and "look at my screen", reading
email, notes, reminders and memory, locking the screen, reading calendars, adding events (you still click
Save), to-dos, browsing and filling in ordinary form fields, everyday smart-home control, directions, places,
ride and food links, package tracking, currency, and market, economic and sports data.

**Skipping approvals.** `AUTO_APPROVE` takes a comma-separated list of tool names that should run without
asking, for example `AUTO_APPROVE=type_text,press_keys`. It can only be set in `.env` (or an environment
variable), not from the Settings screen. **This is at your own risk.** The model can misunderstand you, and
web pages, emails or calendar invites it reads could try to trick it.
- Listing `send_text_message` or `make_phone_call` means texts go out, and calls open, without you seeing them first.
- Listing `browser_click`, `browser_type`, `browser_press` or `smart_home_control` **switches off their
  checks completely**: ATLAS could then book, pay, submit, unlock a door or open the garage without asking.
  Don't.
- Some limits hold whatever you set: ATLAS never types passwords or card numbers, and never texts or calls
  emergency numbers.

To turn shell commands off completely, set `ALLOW_SHELL=false`.

---

## Configuration reference

Settings live in `atlas/.env`. [`.env.example`](.env.example) lists every key with a comment. The
**Settings** drawer covers the everyday keys (names, city, units, news region, models, email, API key). It
writes them to `.env` and they take effect immediately. Put every other key in `.env` (the Settings screen can
save those too, where it has a field for them).

After editing `.env` by hand, restart ATLAS. Keys marked **†** are read each time they're used, so they take
effect without a restart.

A **real environment variable with the same name overrides** `.env`. A blank value means "use the default".
For true/false keys, `1`, `true`, `yes` and `on` mean true, and anything else means false. Keys marked
**(secret)** are never sent back to the browser page.

**AI model**

| Key | Default | What it does |
|---|---|---|
| `OPENROUTER_API_KEY` (secret) | *(none, required)* | Your OpenRouter key (`sk-or-v1-…`). The setup screen asks for it if it's missing. |
| `ATLAS_MODEL` | *(automatic)* | Main model, as an OpenRouter id such as `anthropic/claude-sonnet-4.5`. See [Choosing a model](#choosing-a-model--cost). |
| `ATLAS_SEARCH_MODEL` | *(automatic)* | Model for the web-search research step. Automatic picks the first available of `google/gemini-2.5-flash`, `openai/gpt-4o-mini`, `anthropic/claude-haiku-4.5`, else the main model. |

**About you**

| Key | Default | What it does |
|---|---|---|
| `ASSISTANT_NAME` | `ATLAS` | The assistant's name, which is also the wake word. |
| `USER_NAME` | *(blank)* | Your name, used to personalise replies, sign emails and fill in bookings. |
| `USER_ADDRESS_AS` | *(your name)* | How ATLAS addresses you ("sir", "boss", "Dr. Lee"…). |
| `USER_PHONE` | *(blank)* | Your phone number, for bookings and forms. |
| `HOME_ADDRESS` | *(blank)* | Your home address: the start point for directions, and what "home" means. |
| `WORK_ADDRESS` | *(blank)* | Your work address: what "work" means for directions and rides. |
| `HOME_CITY` | *(IP location)* | Home city for weather, "near me" searches and bookings, e.g. `Austin, Texas`. If blank, weather estimates your location from your IP address. |
| `UNITS` | `imperial` | `imperial` (°F, mph) or `metric` (°C, km/h). |

**News & markets**

| Key | Default | What it does |
|---|---|---|
| `NEWS_REGION` | `US` | Google News country code, such as `GB`, `CA`, `AU` or `IN`. |
| `NEWS_LANGUAGE` | `en` | Google News language code. The region and language pair must be one Google News offers. |
| `MARKET_WATCHLIST` | `^GSPC,^IXIC,^DJI,^VIX,BTC-USD,ETH-USD,GC=F,CL=F,^TNX` | Comma-separated Yahoo Finance tickers for the market ticker. |

**Live data** (all optional; see [Live data sources](#live-data-sources))

| Key | Default | What it does |
|---|---|---|
| `FINANCE_REGION` † | `US` | Two-letter region for Yahoo Finance's trending tickers and stock news: `GB`, `CA`, `AU`, `IN`, `DE`… Unknown values fall back to `US`. Top gainers, losers and most active are always US. |
| `FINANCE_USER_AGENT` † | *(built in)* | Advanced: the browser User-Agent sent to Yahoo Finance and the US Treasury. Only change it if Yahoo starts refusing requests. |
| `COINGECKO_API_KEY` † (secret) | *(blank)* | Free CoinGecko "demo" key for higher crypto rate limits. Works without one. |
| `SEC_CONTACT_EMAIL` † | *(blank)* | Your email, sent to SEC EDGAR with filing and financials lookups as its fair-access policy asks. Without it SEC may refuse requests. |
| `FRED_API_KEY` † (secret) | *(blank)* | Free FRED key for economic data. Without one, ATLAS uses FRED's public CSV downloads. |
| `SPORTS_TIMEZONE` † | *(this computer's)* | IANA time zone for game times, e.g. `America/Chicago`. Unknown names fall back to the computer's zone. |
| `SPORTS_PANEL_LEAGUES` † | `nfl,nba,nhl,mlb,epl` | Comma-separated leagues for the sports scores panel (at most 8), e.g. `nfl,nba,epl,f1`. |

**Email**

| Key | Default | What it does |
|---|---|---|
| `EMAIL_ADDRESS` | *(blank)* | Your email address. Email features are off until this and the password are set. |
| `EMAIL_PASSWORD` (secret) | *(blank)* | The **app password** for that address. See [Email setup](#email-setup). |
| `SMTP_HOST` | *(from address)* | Outgoing mail server. |
| `SMTP_PORT` | *(from address)* | 465 = SSL, other ports use STARTTLS. Defaults to the provider's port, or 465. |
| `IMAP_HOST` | *(from address)* | Incoming mail server. |
| `IMAP_PORT` | *(from address)* | IMAP over SSL, normally 993. |

**Browser** (see [Reservations & web tasks](#browser-options); changes apply when the window next opens)

| Key | Default | What it does |
|---|---|---|
| `BROWSER_CHANNEL` † | *(Chrome, then Edge, then Chromium)* | `chrome` or `msedge` to prefer one installed browser. |
| `BROWSER_EXECUTABLE` † | *(blank)* | Full path to a Chromium-based browser. Overrides `BROWSER_CHANNEL`. |
| `BROWSER_HEADLESS` † | `false` | `true` hides the window. Only for computers with no screen. |
| `BROWSER_VISION_MODEL` † | *(main model)* | Image-capable model for reading screenshots of pages. |

**Calendar** (see [Calendar & to-dos](#calendar--to-dos))

| Key | Default | What it does |
|---|---|---|
| `CALENDAR_ICS_URLS` † (secret) | *(blank)* | Secret iCal links, comma-separated, each optionally named (`Work=https://…`). |
| `CALENDAR_PROVIDER` † | `google` | Where new events go: `google`, `outlook`, `office365`, `ics` or `local`. |
| `CALENDAR_DAY_HOURS` † | `08:00-20:00` | Your usual day, for finding free slots. |

**Texts & calls** (see [Texts & calls](#texts--calls))

| Key | Default | What it does |
|---|---|---|
| `TWILIO_ACCOUNT_SID` † | *(blank)* | Twilio Account SID (`AC…`). |
| `TWILIO_AUTH_TOKEN` † (secret) | *(blank)* | Twilio Auth Token. |
| `TWILIO_FROM_NUMBER` † | *(blank)* | Your Twilio number, e.g. `+15555550123`, or a Messaging Service SID (`MG…`). |
| `DEFAULT_COUNTRY_CODE` † | `+1` | Country for numbers written without a leading `+`. |

**Smart home** (see [Smart home](#smart-home-home-assistant))

| Key | Default | What it does |
|---|---|---|
| `HOME_ASSISTANT_URL` † | *(blank)* | Your Home Assistant address, e.g. `http://homeassistant.local:8123`. |
| `HOME_ASSISTANT_TOKEN` † (secret) | *(blank)* | A long-lived access token. |
| `HOME_ASSISTANT_CONFIRM_DOMAINS` † | *(blank)* | Extra domains, services or devices that must be approved first; `*` approves everything. |

**Safety & server**

| Key | Default | What it does |
|---|---|---|
| `AUTO_APPROVE` | *(blank)* | Comma-separated tool names that skip approval. `.env` only. See [approvals](#how-approvals-work). |
| `ALLOW_SHELL` | `true` | `false` disables `run_command` entirely. |
| `ATLAS_HOST` | `127.0.0.1` | Only `127.0.0.1` or `localhost` are accepted. Any other value is ignored, so ATLAS can't be exposed to your network. |
| `ATLAS_PORT` | `8765` | Web page port. If it's busy, ATLAS tries the next 19 ports and prints the one it picked. |
| `ATLAS_OPEN_BROWSER` | `true` | Open the browser automatically on start. |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Advanced: the OpenRouter API endpoint, for example a local test server. |

Notes on `.env`: put comments on their own lines. A ` #` after an unquoted value starts a comment, so wrap
values that contain `#` in double quotes. Quotes are otherwise optional.

---

## Choosing a model & cost

**The default.** If `ATLAS_MODEL` is blank, or names a model OpenRouter doesn't offer with tool support,
ATLAS uses the first one available from this list:

1. `anthropic/claude-sonnet-5.5`
2. `anthropic/claude-sonnet-4.5`
3. `openai/gpt-5`
4. `google/gemini-2.5-pro`
5. `openai/gpt-4o`

**Switching.** Open **Settings → Model**. The list only shows models that can use tools, with their price
per million input and output tokens and whether they can see images. The change applies from your next message.

**Cheaper, faster options:** `google/gemini-2.5-flash`, `openai/gpt-4o-mini` and `anthropic/claude-haiku-4.5`
cost a fraction of the flagship models and respond quicker. They are somewhat less reliable at multi-step
tasks like a full briefing, a careful email or a booking in the browser. Exact ids change over time, so pick
from the Settings list.

**"Look at my screen" needs a vision model**, one that accepts images. The Settings list marks these. ATLAS's
browser also uses one for date pickers and seating maps (`BROWSER_VISION_MODEL`, or the main model).

**What a request costs.** Every call to the model sends ATLAS's ~85 tool definitions plus its instructions,
your saved memories and recent conversation. That is **roughly 15,000–18,000 prompt tokens per call** before
you've said anything. A request that uses tools makes a call per round, re-sending everything along with the
tool results. So a daily briefing might use 40,000–60,000 prompt tokens in total: at $3 per million input
tokens that's about 12–18 cents. **Bookings in the browser cost the most:** 10–20 steps, each re-sending the
pages read so far, can add up to a few hundred thousand prompt tokens (very roughly $0.50–$1.50 at $3 per
million). A model priced at $0.30 per million costs a tenth of that. Tool output from earlier requests is
trimmed automatically to save tokens. The HUD shows this session's tokens and cost.

**Web search** goes through OpenRouter's web plugin, which adds a **small per-search fee** on top of the
search model's tokens. See OpenRouter's pricing for current rates. Track spending at
[openrouter.ai/activity](https://openrouter.ai/activity). Setting a credit limit on your key is a good safety net.

---

## Privacy & security

**What stays on your computer**
- The server listens on **127.0.0.1 only**, so other devices on your network can't reach it, whatever
  `ATLAS_HOST` says.
- Each launch creates a **random access token** that's embedded in the page. Every API call must carry it,
  so other websites you visit can't make ATLAS do anything.
- ATLAS checks the **Host header** and only answers to `localhost` / `127.0.0.1`. This blocks DNS-rebinding
  attacks. The page also refuses to be embedded in other sites.
- Your **secrets** (OpenRouter key, email password, Twilio auth token, Home Assistant token, secret
  calendar links and the optional CoinGecko and FRED keys) live in `atlas/.env`. When ATLAS saves that file, it makes it readable only by your user on
  macOS/Linux. **Secrets are never sent back to the browser page:** the Settings screen only learns whether
  each one is set (plus the first and last few characters of the OpenRouter key), and leaving a secret field
  blank keeps the current value. Secret calendar links are also blanked out of error messages before anything
  reaches the AI model.
- Your **memories, notes, contacts, reminders, ATLAS's calendar copy, to-dos and saved packages** live in
  `atlas/data/`. So does **ATLAS's browser profile** (`data/browser-profile`), which holds the cookies and
  sign-ins for sites you log into in ATLAS's window. Anyone who can use your computer account could use those
  sign-ins. Delete the folder to sign ATLAS's browser out everywhere. `.env` and `data/` are **git-ignored**,
  so they never get committed, and they stay on your machine.
- Your conversation and voice settings are kept in your browser's local storage.
  **Settings → Clear conversation** wipes the conversation.
- Screenshots are saved to `Pictures/ATLAS` in your home folder.

**What leaves your computer**
- **To OpenRouter and the model provider:** your messages and ATLAS's instructions, which include your name,
  home city, phone number and addresses, saved memories and upcoming reminders. Also the results of any tools
  ATLAS uses to answer you: headlines, web pages, emails it reads, calendar events, files you approve, command
  output, the text and screenshots of pages in ATLAS's browser, and the screenshot when you ask it to look at
  your screen. OpenRouter's [privacy settings](https://openrouter.ai/settings/privacy) let you avoid
  providers that train on your data.
- **To the data sources** when you use them: search queries to OpenRouter/DuckDuckGo; news requests to Google News,
  BBC, NPR, CNBC, Ars Technica and Hacker News; tickers and currency pairs to Yahoo Finance; your city to
  Open-Meteo. If no home city is set, your IP address goes to ipapi.co / ipwho.is to estimate your location.
  Email goes to your mail provider.
- **For live data:** tickers and news searches to Yahoo Finance (with its usual cookies); coin names to
  CoinGecko; company tickers or names, plus your `SEC_CONTACT_EMAIL` if set, to SEC EDGAR; dates and tickers
  to Nasdaq; indicator names to FRED (with your `FRED_API_KEY` if set); team and league names to ESPN. CNN and
  the US Treasury only receive plain page requests. These sources never see your conversation.
- **For the newer abilities:** websites ATLAS's browser visits see an ordinary Chrome or Edge visit, including
  the details ATLAS fills in. Calendar links are fetched from Google, iCloud or Microsoft, and a new event's
  details are passed to Google Calendar or Outlook in the page link. Texts go to Twilio (number and message) or
  to Apple through Messages. Home Assistant requests go to the address you set, usually on your own network.
  Addresses and place searches go to OpenStreetMap's Nominatim and routing servers. Maps, Uber, Lyft, delivery
  and carrier pages open in your browser with the details filled in.
- **Voice recognition** in Chrome and Edge is normally done by Google's or Microsoft's online speech service.

**Untrusted content.** ATLAS treats web pages, news, emails, calendar events, messages, files, screen text and
command output as *data*, never as instructions. Anyone can send you a calendar invite or write a web page.
ATLAS is told not to follow instructions it finds there and to flag phishing or prompt-injection attempts.
Because no model is perfect at this, the risky actions still need your approval.

**Your OpenRouter key is a password.** Don't paste it into chats, screenshots or shared files. If it was
ever shared, even by accident, **delete it at [openrouter.ai/keys](https://openrouter.ai/keys), create a new one,
and paste the new key in Settings**. The same goes for the Twilio and Home Assistant tokens and calendar
links: revoke or reset them where you created them.

---

## Voice tips

- **Use Chrome or Edge** for speech recognition. Firefox doesn't support it. Safari's support is limited.
  You can always type.
- **The mic button** toggles listening, and so does **Ctrl+Space**. Your words appear in the command bar
  as you speak and are sent when you pause.
- **Wake-word mode** (in Settings) keeps listening for the assistant's name. Say *"ATLAS, what's the
  weather?"*, or just *"ATLAS"*. It chimes, and then you speak your request. Renaming the assistant changes the
  wake word, and a distinctive name works best. Listening pauses while ATLAS is speaking.
- **To stop ATLAS talking**, press **Esc** or click the central core.
- **Choosing a voice.** In Settings, pick a voice and adjust rate and pitch, or turn "speak replies" off.
  By default ATLAS looks for an English (UK) male voice and falls back to any English voice.
  - **Edge** has very natural "Online (Natural)" voices.
  - **Windows:** add more voices in Settings → Time & language → Speech.
  - **macOS:** System Settings → Accessibility → Spoken Content → System voice → Manage Voices.
  - **Linux:** browsers usually offer only basic speech-dispatcher/eSpeak voices.

---

## Troubleshooting

**"Python not found" / the launcher closes immediately.** Install Python 3.10+ (see [Quick start](#quick-start)).
On Windows, tick "Add python.exe to PATH" in the installer, then run `start.bat` again. If you opened `start.bat`
from inside a `.zip`, extract the whole folder first.

**Port already in use.** You don't need to do anything. ATLAS picks the next free port and prints the address,
for example `http://127.0.0.1:8766/`. To choose one yourself, set `ATLAS_PORT`.

**"Session expired — reload the page".** ATLAS was restarted, and each launch has a new access token.
Reload the tab, or open the address shown in the terminal.

**"OpenRouter rejected the API key" / the setup screen keeps coming back.** Paste the key again in Settings.
If it still doesn't stick, an environment variable called `OPENROUTER_API_KEY` is probably overriding `.env`.
*"Out of credits"* means you need to top up at [openrouter.ai/settings/credits](https://openrouter.ai/settings/credits).
*"This model can't use tools"* means you should pick another model.

**Panels say "FEED OFFLINE".** The source couldn't be reached. Usually the cause is no internet, a firewall
or VPN, or a source rate-limiting you (Yahoo Finance does this sometimes). Panels retry on their own every
minute for markets and every 5–10 minutes for news and weather. News falls back to the BBC/NPR/CNBC/Ars feeds
when Google News is unavailable. If weather can't find you, set your home city in Settings.

**A market, economic or sports answer says a source is unavailable.** Several of these sources are
unofficial and best-effort (see [Live data sources](#live-data-sources)). Yahoo Finance sometimes rate-limits
or, in the EU, asks for cookie consent; ATLAS then falls back to basic prices and tries again after a short
pause. If SEC EDGAR *"refused the request"*, set `SEC_CONTACT_EMAIL`. Crypto limits are higher with a free
`COINGECKO_API_KEY`. If a team name matches several teams ("New York"), ATLAS asks which one you mean.

**The microphone doesn't work.**
- Use Chrome or Edge, and allow the microphone when asked.
- The permission is per address *including the port*. If ATLAS moved to another port, allow it again.
- Check the operating system's setting too. On Windows: Settings → Privacy & security → Microphone. On macOS:
  System Settings → Privacy & Security → Microphone, for your browser.
- Speech recognition needs an internet connection in most browsers.

**Reminders didn't go off.** The ATLAS page announces reminders, so keep the tab open (it can be in the
background). Reminders that came due while it was closed are announced the next time you open it.

**"Couldn't start a browser for ATLAS".** Install Google Chrome or Microsoft Edge, or download Playwright's
Chromium with `.venv/bin/python -m playwright install chromium` (Windows:
`.venv\Scripts\python -m playwright install chromium`; on Linux add `--with-deps` if it's missing libraries).
If your browser is somewhere unusual, set `BROWSER_EXECUTABLE`.

**"ATLAS's browser profile is already open in another window".** Another ATLAS browser window (perhaps from a
second copy of ATLAS) is using `data/browser-profile`. Close it and try again.

**"No screen is available for a visible browser window".** You're running ATLAS on a computer without a
desktop. Set `BROWSER_HEADLESS=true`, and sign in to sites on a computer with a screen.

**A booking stalls on a sign-in, CAPTCHA or card form.** That's by design: finish that part in ATLAS's browser
window, then say "continue".

**The calendar only shows events ATLAS added.** No calendar link is set, or it couldn't be read. Check
`CALENDAR_ICS_URLS` (see [Calendar & to-dos](#calendar--to-dos)). New events can take a few minutes to appear,
because ATLAS re-reads each calendar at most every 5 minutes and providers update these links on their own schedule.

**Texts fail.** *"isn't verified on your Twilio trial account"*: verify the number in the Twilio Console or
upgrade. *"rejected the credentials"*: copy the Account SID and Auth Token again from the Console home page.
On a Mac without Twilio, allow ATLAS's app (your Terminal) to control **Messages** under System Settings →
Privacy & Security → Automation.

**Calls don't open anything.** On Windows, set up Phone Link (or install Teams or Skype) and choose it for
TEL links in Settings → Apps → Default apps. On a Mac, turn on *Calls from iPhone* in FaceTime settings and
*Calls on Other Devices* on the iPhone.

**Smart home doesn't respond.** Check that `HOME_ASSISTANT_URL` opens Home Assistant in your browser from
this computer, and create a new token if the old one was deleted.

**Typing or shortcuts fail with "needs pyautogui".** The optional extra didn't install. Run
`.venv/bin/python -m pip install -r requirements-extras.txt` (on Windows, `.venv\Scripts\python -m pip install -r requirements-extras.txt`).
On **macOS**, give your Terminal app **Accessibility** permission (System Settings → Privacy & Security).
On **Linux**, this only works in an X11 session, not on Wayland.

**macOS permissions.** Screenshots and "look at my screen" need **Screen Recording** permission for your
Terminal. Volume, media, quitting apps and the Messages app use AppleScript, and macOS asks once for
**Automation** permission.

**Linux helpers.** Some features use standard tools. Install the ones you need:

| Feature | Needs | Debian/Ubuntu | Fedora |
|---|---|---|---|
| Clipboard | `xclip` (X11) or `wl-clipboard` (Wayland) | `sudo apt install xclip wl-clipboard` | `sudo dnf install xclip wl-clipboard` |
| Play/pause/next | `playerctl` | `sudo apt install playerctl` | `sudo dnf install playerctl` |
| Volume | `pactl` (or `amixer`) | `sudo apt install pulseaudio-utils` | `sudo dnf install pulseaudio-utils` |

Opening files uses `xdg-open`. Locking uses `loginctl` or `xdg-screensaver`.

**Screenshots on Wayland** may fail or come out black. When capture through X11 doesn't work, ATLAS falls
back to `gnome-screenshot` (GNOME), `grim` (Sway, Hyprland) or `spectacle` (KDE), so install the one for your
desktop. Alternatively, log in to an "Xorg"/X11 session.

**"I've reached my limit of actions for one request".** ATLAS stops after 30 rounds of tool use per request.
Say "continue".

**An approval card stopped responding.** Cards expire after 5 minutes and count as denied. Ask again.

**Starting fresh.** Deleting `.venv` reinstalls the Python packages. Deleting `data/` **permanently erases**
memories, notes, contacts, reminders, to-dos and ATLAS's calendar, and signs its browser out of every site.
Deleting `.env` resets all settings, and the setup screen returns.

---

## Project layout

```
atlas/
├── start.bat               Windows launcher (double-click)
├── start.sh                macOS / Linux launcher
├── run.py                  Entry point: python run.py
├── requirements.txt        Required packages (FastAPI, Uvicorn, httpx, psutil, Pillow, tzdata,
│                           icalendar, recurring-ical-events, Playwright)
├── requirements-extras.txt Optional: pyautogui for typing and shortcuts
├── .env.example            Every setting, documented. Copy to .env
├── .env                    Your settings and secrets (created by the setup screen, git-ignored)
├── app/
│   ├── server.py           Local web server and API (127.0.0.1 only, token + host checks)
│   ├── agent.py            Persona, conversation handling, the tool-use loop and approvals
│   ├── llm.py              OpenRouter client: model discovery and streaming chat
│   ├── config.py           Settings, defaults, model preferences, approval list
│   ├── util.py             Shared helpers
│   └── tools/
│       ├── news.py         Google News, BBC, NPR, CNBC, Ars Technica, Hacker News
│       ├── web.py          Web search, page reader, Wikipedia
│       ├── markets.py      Yahoo Finance quotes and ticker lookup
│       ├── finance_data.py Stock details, movers, stock news, technicals, yield curve (Yahoo, Treasury)
│       ├── data_sources.py Crypto, SEC filings and financials, earnings calendar, sentiment, FRED data
│       ├── sports.py       ESPN scores, schedules, standings and sports news
│       ├── weather.py      Open-Meteo forecast and geocoding
│       ├── email_tools.py  IMAP reading, SMTP sending
│       ├── browser.py      ATLAS's own browser window and restaurant reservations
│       ├── calendar_tools.py  Calendar feeds, availability, new events, to-do list
│       ├── comms.py        Texts (Twilio / Messages) and phone calls
│       ├── smart_home.py   Home Assistant devices and control
│       ├── errands.py      Directions, places, rides, food delivery, packages, currency
│       ├── pc.py           Apps, media, volume, files, clipboard, keyboard, shell, screen, power
│       ├── memory.py       Long-term memory, contacts, notes, reminders
│       └── general.py      Calculator and world clock
├── web/                    The browser HUD (HTML, CSS, JavaScript)
├── data/                   Your memories, notes, contacts, reminders, calendar, to-dos, packages
│                           and ATLAS's browser profile (git-ignored)
└── tests/                  Automated tests
```
