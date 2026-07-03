# Maps Scraper — Full Upgrade Prompt for Antigravity

## Context

You are upgrading an existing Python Google Maps scraper built with Playwright + LangGraph + OpenAI-compatible LLM API. The existing repo is at: https://github.com/rimraf-adi/simple-maps-scraper

**Existing structure:**
```
├── agent/            LangGraph agent — perceive/plan/act loop for email extraction
├── browser/          Playwright BrowserManager
├── llm/              OpenAI-compatible LLM client (reads LLM_BASE_URL, LLM_API_KEY, LLM_MODEL from .env)
├── main.py           Entry point — orchestrates Maps search → detail fetch → email extraction
├── maps_scraper.py   Google Maps scraping: scroll feed, extract leads, fetch_place_details
├── extract_emails.py Email extraction pipeline using the LangGraph agent
└── pyproject.toml    uv-managed deps (playwright, langgraph, openai, python-dotenv, etc.)
```

**Current flow:**
1. `main.py` takes a CLI query arg, opens Playwright browser, calls `extract_leads()` to scroll Maps and collect business URLs/names/ratings/categories
2. For each lead, calls `fetch_place_details()` to get phone/website/address
3. Calls `extract_from_site()` which runs a LangGraph agent to navigate the business's website and extract emails
4. Writes results to a CSV

**Current problems being solved by this upgrade:**
- No terminal UI — hard to tell if it's running, no progress visibility
- Single API key — if rate-limited, the whole run dies
- No IP block / anti-detection measures
- No advanced search control (filters, result count cap, min rating, etc.)
- No session resume if crashed mid-run

---

## What to build

Implement ALL of the following changes. Do not skip any section.

---

### 1. Rich Terminal UI (NO web UI — terminal only)

Replace the plain `logging` output with a **Rich**-based terminal dashboard using `rich.live`, `rich.layout`, `rich.panel`, `rich.progress`, and `rich.table`.

The terminal UI must show a persistent live-updating dashboard with:

**Top panel — Run Summary:**
- Query string being scraped
- Current phase: `MAPS SEARCH` / `FETCHING DETAILS` / `EXTRACTING EMAILS` / `DONE`
- Total leads found, leads processed so far, leads with emails found
- Active API key index (e.g. `Key 3/12`) and current model name
- Elapsed time (HH:MM:SS live counter)
- Estimated time remaining (calculated from current pace)

**Middle panel — Progress Bars:**
- Phase 1 bar: Maps scroll progress (0–50 scrolls)
- Phase 2 bar: Per-lead details fetching (X of N leads)
- Phase 3 bar: Email extraction (X of N leads with websites)
- Each bar shows percentage, completed/total, and speed (leads/min)

**Bottom panel — Live Log Panel:**
- Scrolling log of the last 20 lines
- Color-coded by level:
  - `INFO` → white
  - `SUCCESS` (email found) → bright green with ✉ icon
  - `SKIP` (no website) → dim yellow
  - `WARNING` → yellow
  - `ERROR` → red
  - `API SWITCH` → bright cyan with ⚡ icon
  - `RETRY` → magenta
- Each line includes a timestamp (HH:MM:SS)
- Log lines include the current lead name and step context

**Right sidebar — Current Lead Card:**
- Shows the lead currently being processed:
  - Name, Category, Rating
  - Phone (if found)
  - Website URL being navigated
  - Email (as soon as found)
  - Status: SCRAPING / NAVIGATING / FOUND / SKIPPED / FAILED

Use `rich.live` with `refresh_per_second=4` to keep the dashboard smooth without flicker.

All logging throughout the codebase must pipe into the Rich log panel via a custom `RichHandler` or a thread-safe log queue — do NOT mix plain `print()` or standard logging output with the Rich live display.

---

### 2. Multi-API Key Pool with Auto-Rotation

**Config:** Support 1–15 API keys. Keys are defined in `.env` as:
```
LLM_API_KEY_1=sk-xxx
LLM_API_KEY_2=sk-yyy
LLM_API_KEY_3=sk-zzz
# ...up to LLM_API_KEY_15
```
Also still support the legacy `LLM_API_KEY` (single key) as fallback.

**Build a `KeyPool` class in `llm/key_pool.py`:**
```python
class KeyPool:
    def __init__(self, keys: list[str], base_url: str, model: str): ...
    def current_key(self) -> str: ...
    def rotate(self, reason: str = "") -> bool: ...  # returns False if all keys exhausted
    def reset_all(self): ...  # mark all keys as available again (new day / new run)
    def status(self) -> list[dict]: ...  # for UI: key index, masked key, call count, errors, status
```

**Rotation logic:**
- Rotate to next key immediately on any of these errors from the OpenAI client:
  - `RateLimitError`
  - `AuthenticationError` (bad key — mark as permanently bad, skip)
  - HTTP 429 response
  - HTTP 401 response
  - Any exception whose message contains: `rate limit`, `quota`, `limit exceeded`, `429`
- After rotating, log an `API SWITCH` event to the UI with the reason and new key index
- If ALL keys are exhausted (all rate-limited), implement exponential backoff: wait 30s, 60s, 120s while showing a countdown in the UI, then reset the pool and try again (up to 3 full pool cycles before aborting the run)
- Track per-key: total calls made, successful calls, failed calls, last error

**Inject `KeyPool` into the LLM client:** Modify `llm/client.py` so every LLM call uses `key_pool.current_key()` and wraps the call in a retry loop that calls `key_pool.rotate()` on the errors above.

---

### 3. Anti-Detection & IP Block Prevention

Implement the following anti-bot measures. These must be active by default and configurable via CLI flags or `.env`:

**Browser fingerprinting evasion (in `browser/manager.py`):**
- Launch Playwright with a realistic `user_agent` — rotate from a list of 10+ real Chrome/Firefox UA strings on each run
- Set `viewport` randomly within common resolutions: `1280x800`, `1366x768`, `1440x900`, `1920x1080`
- Pass these Playwright launch args: `--disable-blink-features=AutomationControlled`
- Use `stealth` script injection: on every new page, run JS to delete `navigator.webdriver`, spoof `navigator.plugins`, `navigator.languages`, and `window.chrome`
- Set `locale` and `timezone` args on the browser context (use `en-US`, `America/New_York` by default, configurable)
- Accept all cookies automatically to avoid cookie consent overlays breaking selectors

**Request pacing (in `maps_scraper.py` and `extract_emails.py`):**
- Replace all `asyncio.sleep(N)` with `human_delay(min_s, max_s)` — a utility that sleeps for a random duration between `min_s` and `max_s` seconds, using a normal distribution centered at the midpoint
- Default delays:
  - Between scroll steps: 1.5s–3.5s (currently hardcoded 2.0s)
  - After navigating to a Maps place page: 2.0s–4.0s
  - Between navigating website pages during email extraction: 1.0s–2.5s
- Add random mouse movement simulation between major actions: generate 3–5 random `page.mouse.move()` calls with smooth trajectory before clicking

**Session management:**
- Use Playwright's `browser_context` with a `user_data_dir` so cookies/localStorage persist across retries (avoids re-solving captchas)
- Store the user data dir in a `.scraper_session/` folder (gitignored)

**CAPTCHA / block detection:**
- After each navigation, check if the page URL or content contains known block signals: `google.com/sorry`, `unusual traffic`, `captcha`, `blocked`, `access denied`
- If detected: log a `WARNING` to the UI with instructions, pause the scraper (show a countdown in the UI), and wait for the user to manually solve it in the browser window (only works in non-headless mode)
- If running headless and blocked, emit an `ERROR` and skip the affected URL (do not crash the run)

---

### 4. Advanced Search Configuration

Replace the simple positional CLI argument with a full `argparse` config in `main.py`. Support ALL of the following flags:

```
uv run main.py "Business coaches in New York" [options]

Search options:
  --max-results N        Stop after collecting N leads from Maps (default: unlimited)
  --min-rating FLOAT     Skip leads with rating below this value (e.g. --min-rating 4.0)
  --require-website      Skip leads that have no website (don't attempt email extraction, just skip)
  --require-phone        Only output leads that have a phone number
  --require-email        Only output leads that found an email address
  --category KEYWORD     Filter leads whose Category field contains KEYWORD (case-insensitive)
  --exclude-chain        Skip leads whose name matches known chain brands (Starbucks, McDonald's, etc.)
                         Uses a configurable blocklist in config/chain_blocklist.txt

Browser options:
  --headless             Run browser in headless mode
  --no-stealth           Disable stealth/fingerprint evasion (for debugging)
  --slow-mo MS           Add slowmo delay to Playwright in milliseconds

Output options:
  --output PATH          Custom output CSV path (default: <query_slug>.csv)
  --append               Append to existing CSV instead of overwriting
  --format [csv|json|both]  Output format (default: csv)
  --dedupe               Before writing, deduplicate rows by website domain

Run control:
  --resume               Resume from last checkpoint file for this query (see section 5)
  --workers N            Number of parallel email extraction workers (default: 1, max: 3)
                         Note: parallel mode opens N separate browser contexts
```

Store all resolved config in a `ScraperConfig` dataclass and pass it through to all modules. Show the full resolved config in the terminal UI at startup.

---

### 5. Session Resume / Checkpoint System

Add crash-safe checkpointing so a run can be resumed:

- After completing phase 1 (Maps scrape), save `{query_slug}_checkpoint.json` with all leads data
- After each lead's details + email are resolved, update the checkpoint with that lead's final data and mark it `done: true`
- On `--resume`, load the checkpoint, skip all leads already marked `done`, and continue from where it left off
- Show in the terminal UI how many leads were restored from checkpoint vs fresh

Checkpoint file format:
```json
{
  "query": "Business coaches in New York",
  "started_at": "2026-06-10T14:23:00",
  "resumed_at": "...",
  "leads": [
    { "Name": "...", "done": true, "Email": "...", ... },
    { "Name": "...", "done": false, ... }
  ]
}
```

---

### 6. Extra Features (build all of these)

**A. Post-run stats report**
After the run completes, print (and optionally save to `{query_slug}_report.txt`) a formatted summary:
- Total leads scraped
- Leads with phone / website / email (absolute counts and percentages)
- Top 5 categories found
- Average rating of leads
- API keys used, calls per key, total LLM API calls
- Total run time
- Leads per minute throughput

**B. Duplicate detection**
Before adding a lead to CSV output, check if a lead with the same website domain or phone number already exists in the output (in-memory set). If duplicate, log a SKIP and don't write the row.

**C. Email validation**
After extracting an email, validate it with a regex that rejects obviously fake patterns like `noreply@`, `no-reply@`, `info@info`, `test@`, `example.com`. Log SKIPPED_EMAIL if rejected.

**D. Configurable chain blocklist**
Create `config/chain_blocklist.txt` — one brand name per line. When `--exclude-chain` is set, any lead whose name (case-insensitive) contains any entry is skipped. Pre-populate with 30 common chains (Starbucks, McDonald's, Subway, KFC, etc.).

**E. JSON output support**
When `--format json` or `--format both` is passed, write a `{query_slug}.json` file in addition to or instead of CSV. Each lead is a JSON object in a top-level array.

---

### 7. File & Module Structure After Upgrade

The final codebase must be organized as:
```
├── agent/
│   └── (existing LangGraph agent — minimal changes)
├── browser/
│   ├── manager.py         (upgraded — stealth, session persistence, block detection)
│   └── stealth.py         (new — JS stealth injection script + UA list)
├── llm/
│   ├── client.py          (upgraded — uses KeyPool)
│   └── key_pool.py        (new — KeyPool class)
├── ui/
│   ├── dashboard.py       (new — Rich live dashboard, all panels)
│   └── log_handler.py     (new — thread-safe log queue feeding into Rich)
├── config/
│   └── chain_blocklist.txt  (new)
├── main.py                (upgraded — argparse, ScraperConfig, orchestration)
├── maps_scraper.py        (upgraded — human_delay, anti-bot measures)
├── extract_emails.py      (upgraded — uses KeyPool-backed LLM client)
├── checkpoint.py          (new — save/load/update checkpoint JSON)
├── output.py              (new — CSV + JSON writer with dedup)
├── .env.example           (updated — shows LLM_API_KEY_1 through LLM_API_KEY_15 format)
└── pyproject.toml         (updated — add `rich` dependency)
```

---

### 8. Implementation Notes & Constraints

- **Do not use a web UI, Flask, FastAPI, or any HTTP server.** Everything is terminal-only.
- **Python 3.11+ syntax** — use `asyncio.TaskGroup` where appropriate for parallel workers.
- **`uv` as package manager** — update `pyproject.toml` to add `rich>=13.0` as a dependency. Do not use pip directly.
- **Zorin OS / Linux compatibility** — no Windows-specific APIs. All paths use `pathlib.Path`.
- **The existing LangGraph agent logic in `agent/` should remain intact** — only modify its LLM client calls to go through `KeyPool`.
- **Rich live display must not conflict with Playwright's output** — ensure stdout/stderr from Playwright and asyncio don't break the `rich.live` context. Route all Playwright log output through the log queue.
- **Error handling philosophy:** Never let a single lead failure crash the entire run. Every per-lead operation (`fetch_place_details`, `extract_from_site`) must be wrapped in `try/except` that logs the error and continues to the next lead.
- **The `.scraper_session/` directory and `*_checkpoint.json` files must be added to `.gitignore`.**
- **Type hints on all new functions.** Use `dataclasses` for `ScraperConfig`.

---

### 9. Startup Sequence

When the script starts, the Rich dashboard should initialize first, then show this startup checklist (tick each item green as it passes):

```
[ ] Loading config
[ ] Reading API keys (found N keys)
[ ] Testing LLM connectivity (key 1)
[ ] Launching browser
[ ] Loading stealth scripts
[ ] Checkpoint: [fresh run / resuming N leads]
[ ] Starting scrape...
```

If LLM connectivity fails on key 1, automatically try keys 2, 3, etc. and report which key succeeded. Only abort if all keys fail.

---

### 10. Example `.env.example` After Upgrade

```env
# LLM Configuration
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.3-70b-versatile

# API Keys — add as many as you have (1–15)
LLM_API_KEY_1=your_first_key_here
LLM_API_KEY_2=your_second_key_here
# LLM_API_KEY_3=...
# LLM_API_KEY_4=...
# ...up to LLM_API_KEY_15

# Legacy single-key support (used if no numbered keys are set)
# LLM_API_KEY=your_key_here

# Browser settings
HEADLESS=false
BROWSER_LOCALE=en-US
BROWSER_TIMEZONE=America/New_York
```

---

### Delivery

Produce the complete, working, runnable code for every file listed in section 7. Do not produce stubs or TODOs — every function must be fully implemented. Start with `pyproject.toml`, then `llm/key_pool.py`, then `ui/dashboard.py`, then `ui/log_handler.py`, then `browser/stealth.py`, then `browser/manager.py`, then `checkpoint.py`, then `output.py`, then `maps_scraper.py`, then `extract_emails.py`, then `main.py`. Output each file with its full path as a header.