# ATLAS

**A voice-driven personal AI assistant that runs on your own computer.**

ATLAS lives in a browser tab as a heads-up display. You talk to it (or type), and it can brief you on the
news, look things up on the live web, check markets and weather, read and send your email, keep notes and
reminders, remember things about you, and operate your PC: opening apps, controlling media, finding files,
running commands. Anything risky waits for your approval first.

The thinking is done by the AI model you choose through [OpenRouter](https://openrouter.ai). The server,
your settings and your personal data all stay on your machine.

**Contents:** [Features](#what-atlas-can-do) · [Quick start](#quick-start) · [First run](#first-run) ·
[Things to say](#things-to-say) · [Email](#email-setup) · [Approvals](#how-approvals-work) ·
[Configuration](#configuration-reference) · [Models & cost](#choosing-a-model--cost) ·
[Privacy](#privacy--security) · [Voice](#voice-tips) · [Troubleshooting](#troubleshooting) ·
[Layout](#project-layout)

---

## What ATLAS can do

| Area | What it does | Powered by |
|---|---|---|
| **Live news** | Headlines by section (top, world, US, business, technology, science, health, sports, entertainment) or on any topic you name | Google News, with BBC, NPR, CNBC and Ars Technica as backups, plus Hacker News |
| **Web search** | Searches the live web and answers with cited sources. It can also read a full article from a link | OpenRouter's web plugin, falling back to DuckDuckGo |
| **Encyclopedia** | Quick summaries of people, places, organisations and events | Wikipedia |
| **Markets** | Stocks, ETFs, indices, crypto, currencies and futures: price, change over 1 day to 5 years, ticker lookup | Yahoo Finance |
| **Weather** | Current conditions and a forecast of up to 7 days, for home or any city | Open-Meteo (no key needed) |
| **Email** | List, search and read your inbox. Writes and sends email after you approve the draft | Your own mailbox over IMAP/SMTP |
| **PC control** | Open and close apps, open websites, play music or video, play/pause/skip, volume, find and open files, read/write text files, clipboard, type text and press shortcuts, run shell commands, take screenshots, **"look at my screen"**, lock the screen, sleep/restart/shut down | Your operating system |
| **Notes & contacts** | A notebook you can add to and search. Contacts so you can say "email Mom" | Stored locally in `data/` |
| **Reminders** | Timers and reminders, announced aloud with a chime and a desktop notification | Stored locally in `data/` |
| **Long-term memory** | Remembers preferences and facts you share, and uses them to personalise answers. You can review or delete them in Settings | Stored locally in `data/` |
| **Voice** | Speak instead of typing, with an optional wake word (the assistant's name). Replies are read aloud sentence by sentence as they arrive | Your browser's speech engine |
| **Utilities** | Exact math (percentages, compound growth, etc.) and a world clock | Built in |

The HUD also has live panels: weather, system telemetry (CPU, memory, disk, battery), upcoming reminders,
a news feed with category tabs, a scrolling market ticker, and a running count of this session's tokens and cost.
One-tap chips cover the everyday requests: **Daily briefing**, **Headlines**, **Markets**, **Inbox** and **System check**.

---

## Quick start

**You'll need:**
- **Python 3.10 or newer**
- **An OpenRouter API key** with a little credit on it. Create one at [openrouter.ai/keys](https://openrouter.ai/keys).
- **Chrome or Edge** if you want to use voice. Other browsers work for typing.

### Windows

1. Install Python from [python.org/downloads](https://www.python.org/downloads/). On the installer's first
   screen, tick **"Add python.exe to PATH"**.
2. Open the `atlas` folder and **double-click `start.bat`**.
3. The first run takes a minute or two while it sets up. After that, your browser opens ATLAS at
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
3. Install `requirements.txt`, but only on the first run or after the file changes. That keeps every
   later start fast, and after an update the new requirements are installed automatically.
4. Try the optional `requirements-extras.txt`, which lets ATLAS type and press shortcuts for you. If that
   fails, ATLAS still starts and only those two abilities are missing.
5. Start ATLAS with `python run.py`.

If something gets into a bad state, delete the `.venv` folder. The next start rebuilds it from scratch.

### Manual start

```bash
cd atlas
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-extras.txt   # optional: typing and keyboard shortcuts
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

**Email**
- "Do I have any unread email?"
- "Read me the latest email from Sam, then draft a polite reply saying Thursday works."
- "Save Mom's email address as mom@example.com." Then: "Email Mom that I'll be late for dinner."

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

## How approvals work

Some actions are hard to undo, so ATLAS **always shows an Approve / Deny card first** for these:

| Tool | What the card shows you |
|---|---|
| `send_email` | Recipients, subject and the full message |
| `run_command` | The exact command, and the folder if one was given |
| `read_file` | The file path. Its contents will be sent to the AI model |
| `write_file` | The path, overwrite or append, and the content |
| `close_application` | The app. Unsaved work may be lost |
| `power_action` | Shut down, restart, sleep or log out |
| `type_text` | The text it will type into the focused window |
| `press_keys` | The key combination |

- Click **Approve** or **Deny**, or say *"yes / approve / confirm / send it / do it / go ahead"* or
  *"no / deny / cancel / stop"* while a card is waiting.
- If you don't answer within **5 minutes**, the action is automatically denied.
- When you deny something, ATLAS won't retry it unless you ask.

Everything else runs without a card, because it only reads information or is easy to reverse. That covers
opening apps, sites and files, media and volume, the clipboard, screenshots and "look at my screen", reading
email, notes, reminders and memory, and locking the screen.

**Skipping approvals.** `AUTO_APPROVE` takes a comma-separated list of the tool names above that should run
without asking, for example `AUTO_APPROVE=type_text,press_keys`. **This is at your own risk.** The model can
misunderstand you, and web pages or emails it reads could try to trick it. To turn shell commands off
completely, set `ALLOW_SHELL=false`.

---

## Configuration reference

Settings live in `atlas/.env`. [`.env.example`](.env.example) lists every key with a comment. The
**Settings** drawer covers the everyday keys (names, city, units, news region, models, email, API key);
it writes them to `.env` and they take effect immediately. For the other keys, edit `.env` and restart ATLAS.

A **real environment variable with the same name overrides** `.env`. A blank value means "use the default".
For true/false keys, `1`, `true`, `yes` and `on` mean true, and anything else means false.

| Key | Default | What it does |
|---|---|---|
| `OPENROUTER_API_KEY` | *(none, required)* | Your OpenRouter key (`sk-or-v1-…`). The setup screen asks for it if it's missing. |
| `ATLAS_MODEL` | *(automatic)* | Main model, as an OpenRouter id such as `anthropic/claude-sonnet-4.5`. See [Choosing a model](#choosing-a-model--cost). |
| `ATLAS_SEARCH_MODEL` | *(automatic)* | Model for the web-search research step. Automatic picks the first available of `google/gemini-2.5-flash`, `openai/gpt-4o-mini`, `anthropic/claude-haiku-4.5`, else the main model. |
| `ASSISTANT_NAME` | `ATLAS` | The assistant's name, which is also the wake word. |
| `USER_NAME` | *(blank)* | Your name, used to personalise replies and sign emails. |
| `USER_ADDRESS_AS` | *(your name)* | How ATLAS addresses you ("sir", "boss", "Dr. Lee"…). |
| `HOME_CITY` | *(IP location)* | Home city for weather, e.g. `Austin, Texas`. If blank, ATLAS estimates your location from your IP address. |
| `UNITS` | `imperial` | `imperial` (°F, mph) or `metric` (°C, km/h). |
| `NEWS_REGION` | `US` | Google News country code, such as `GB`, `CA`, `AU` or `IN`. |
| `NEWS_LANGUAGE` | `en` | Google News language code. The region and language pair must be one Google News offers. |
| `MARKET_WATCHLIST` | `^GSPC,^IXIC,^DJI,^VIX,BTC-USD,ETH-USD,GC=F,CL=F,^TNX` | Comma-separated Yahoo Finance tickers for the market ticker. |
| `EMAIL_ADDRESS` | *(blank)* | Your email address. Email features are off until this and the password are set. |
| `EMAIL_PASSWORD` | *(blank)* | The **app password** for that address. See [Email setup](#email-setup). |
| `SMTP_HOST` | *(from address)* | Outgoing mail server. |
| `SMTP_PORT` | *(from address)* | 465 = SSL, other ports use STARTTLS. Defaults to the provider's port, or 465. |
| `IMAP_HOST` | *(from address)* | Incoming mail server. |
| `IMAP_PORT` | *(from address)* | IMAP over SSL, normally 993. |
| `AUTO_APPROVE` | *(blank)* | Comma-separated tool names that skip the approval card. See [approvals](#how-approvals-work). |
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
tasks like a full briefing or a careful email. Exact ids change over time, so pick from the Settings list.

**"Look at my screen" needs a vision model**, one that accepts images. The Settings list marks these.

**What a request costs.** Every call to the model sends ATLAS's ~40 tool definitions plus its instructions,
your saved memories and recent conversation. That is a **few thousand prompt tokens per call** (around 5,000
to start) before you've said anything. A request that uses tools makes a call per round, re-sending
everything along with the tool results. So a daily briefing might use 15,000–25,000 prompt tokens in total.
For example, at $3 per million input tokens that's 5–8 cents. A model priced at $0.30 per million costs a tenth of that.
Older tool output is trimmed automatically to save tokens. The HUD shows this session's tokens and cost.

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
- Your **API key and email password** live in `atlas/.env`. When ATLAS saves that file, it makes it readable
  only by your user on macOS/Linux.
  Your **memories, notes, contacts and reminders** live in `atlas/data/`. Both are **git-ignored**, so they
  never get committed, and they stay on your machine.
- Your conversation and voice settings are kept in your browser's local storage.
  **Settings → Clear conversation** wipes the conversation.
- Screenshots are saved to `Pictures/ATLAS` in your home folder.

**What leaves your computer**
- **To OpenRouter and the model provider:** your messages and ATLAS's instructions, which include your name,
  home city, saved memories and upcoming reminders. Also the results of any tools ATLAS uses to answer you:
  headlines, web pages, emails it reads, files you approve, command output, and the screenshot when you ask it
  to look at your screen. OpenRouter's [privacy settings](https://openrouter.ai/settings/privacy) let you avoid
  providers that train on your data.
- **To the data sources** when you use them: search queries to OpenRouter/DuckDuckGo; news requests to Google News,
  BBC, NPR, CNBC, Ars Technica and Hacker News; tickers to Yahoo Finance; your city to Open-Meteo. If no
  home city is set, your IP address goes to ipapi.co / ipwho.is to estimate your location. Email goes to your mail provider.
- **Voice recognition** in Chrome and Edge is normally done by Google's or Microsoft's online speech service.

**Untrusted content.** ATLAS treats web pages, news, emails, files, screen text and command output as
*data*, never as instructions. It's told not to follow instructions it finds there and to flag phishing or
prompt-injection attempts. Because no model is perfect at this, the risky actions still need your approval.

**Your OpenRouter key is a password.** Don't paste it into chats, screenshots or shared files. If it was
ever shared, even by accident, **delete it at [openrouter.ai/keys](https://openrouter.ai/keys), create a new one,
and paste the new key in Settings**.

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

**The microphone doesn't work.**
- Use Chrome or Edge, and allow the microphone when asked.
- The permission is per address *including the port*. If ATLAS moved to another port, allow it again.
- Check the operating system's setting too. On Windows: Settings → Privacy & security → Microphone. On macOS:
  System Settings → Privacy & Security → Microphone, for your browser.
- Speech recognition needs an internet connection in most browsers.

**Reminders didn't go off.** The ATLAS page announces reminders, so keep the tab open (it can be in the
background). Reminders that came due while it was closed are announced the next time you open it.

**Typing or shortcuts fail with "needs pyautogui".** The optional extra didn't install. Run
`.venv/bin/python -m pip install -r requirements-extras.txt` (on Windows, `.venv\Scripts\python -m pip install -r requirements-extras.txt`).
On **macOS**, give your Terminal app **Accessibility** permission (System Settings → Privacy & Security).
On **Linux**, this only works in an X11 session, not on Wayland.

**macOS permissions.** Screenshots and "look at my screen" need **Screen Recording** permission for your
Terminal. Volume, media and quitting apps use AppleScript, and macOS asks once for **Automation** permission.

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

**"I've reached my limit of actions for one request".** ATLAS stops after 10 rounds of tool use per request.
Say "continue".

**An approval card stopped responding.** Cards expire after 5 minutes and count as denied. Ask again.

**Starting fresh.** Deleting `.venv` reinstalls the Python packages. Deleting `data/` **permanently erases**
memories, notes, contacts and reminders. Deleting `.env` resets all settings, and the setup screen returns.

---

## Project layout

```
atlas/
├── start.bat               Windows launcher (double-click)
├── start.sh                macOS / Linux launcher
├── run.py                  Entry point: python run.py
├── requirements.txt        Required packages (FastAPI, Uvicorn, httpx, psutil, Pillow, tzdata)
├── requirements-extras.txt Optional: pyautogui for typing and shortcuts
├── .env.example            Every setting, documented. Copy to .env
├── .env                    Your settings and secrets (created by the setup screen, git-ignored)
├── app/
│   ├── server.py           Local web server and API (127.0.0.1 only, token + host checks)
│   ├── agent.py            Persona, conversation handling and the tool-use loop
│   ├── llm.py              OpenRouter client: model discovery and streaming chat
│   ├── config.py           Settings, defaults, model preferences, approval list
│   ├── util.py             Shared helpers
│   └── tools/
│       ├── news.py         Google News, BBC, NPR, CNBC, Ars Technica, Hacker News
│       ├── web.py          Web search, page reader, Wikipedia
│       ├── markets.py      Yahoo Finance quotes and ticker lookup
│       ├── weather.py      Open-Meteo forecast and geocoding
│       ├── email_tools.py  IMAP reading, SMTP sending
│       ├── pc.py           Apps, media, volume, files, clipboard, keyboard, shell, screen, power
│       ├── memory.py       Long-term memory, contacts, notes, reminders
│       └── general.py      Calculator and world clock
├── web/                    The browser HUD (HTML, CSS, JavaScript)
├── data/                   Your memories, notes, contacts, reminders (JSON, git-ignored)
└── tests/                  Automated tests
```
