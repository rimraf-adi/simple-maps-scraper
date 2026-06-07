"""
Maps Scraper — scrapes Google Maps + extracts emails from business websites.
"""
import asyncio
import csv
import logging
import re
import sys
from urllib.parse import urljoin, urlparse

from scrapling.fetchers import AsyncStealthySession


# ---------------------------------------------------------------------------
# Colorful logging
# ---------------------------------------------------------------------------

class ColorFormatter(logging.Formatter):
    grey = "\x1b[38;20m"
    blue = "\x1b[34;20m"
    cyan = "\x1b[36;20m"
    green = "\x1b[32;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"

    FORMATS = {
        logging.DEBUG: f"{grey}%(asctime)s [DBUG] %(message)s{reset}",
        logging.INFO: f"{blue}%(asctime)s{reset} {green}[INFO]{reset} %(message)s",
        logging.WARNING: f"{yellow}%(asctime)s [WARN] %(message)s{reset}",
        logging.ERROR: f"{red}%(asctime)s [ERR!] %(message)s{reset}",
        logging.CRITICAL: f"{bold_red}%(asctime)s [CRIT] %(message)s{reset}",
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno, self.grey)
        formatter = logging.Formatter(log_fmt, datefmt="%H:%M:%S")
        return formatter.format(record)


_handler = logging.StreamHandler()
_handler.setFormatter(ColorFormatter())

log = logging.getLogger("maps_scraper")
log.addHandler(_handler)
log.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Google Maps place-page details
# ---------------------------------------------------------------------------

async def fetch_place_details(session, url):
    resp = await session.fetch(url, network_idle=True, timeout=30000)

    phone_btn = resp.css('button[data-item-id*="phone"]').first
    phone = phone_btn.attrib.get("aria-label", "").replace("Phone: ", "").strip() if phone_btn else ""

    website_link = resp.css('a[data-item-id*="authority"]').first
    website = website_link.attrib.get("href", "").strip() if website_link else ""

    addr_btn = resp.css('button[data-item-id="address"]').first
    address = addr_btn.attrib.get("aria-label", "").replace("Address: ", "").strip() if addr_btn else ""

    return phone, website, address


# ---------------------------------------------------------------------------
# Email extraction — explicit stealth crawl with AsyncStealthySession
#
# Strategy:
#   1. Open homepage with dedicated AsyncStealthySession
#        disable_resources=True  (no images/fonts/media)
#        network_idle=True       (wait for JS to finish)
#   2. Extract mailto: hrefs from rendered DOM  ← primary source
#   3. Discover contact-like links from rendered DOM
#   4. Fetch each discovered contact page (same browser context)
#   5. If mailto: empty → regex fallback on rendered HTML, hard-filtered
#   6. Score and return ranked results
# ---------------------------------------------------------------------------

# ── Tunables ─────────────────────────────────────────────────────────────────

MAX_PAGES    = 4
PAGE_TIMEOUT = 20000   # ms
NETWORK_WAIT = True

# ── Junk filters ─────────────────────────────────────────────────────────────

JUNK_DOMAINS = {
    "sentry.io", "sentry.wixpress.com", "sentry-next.wixpress.com", "wixpress.com",
    "example.com", "example.org", "test.com", "localhost",
    "googletagmanager.com", "googleapis.com", "google.com",
    "facebook.com", "twitter.com", "instagram.com", "linkedin.com",
    "cloudfront.net", "amazonaws.com", "akamaihd.net",
    "w3.org", "schema.org", "ogp.me",
}

JUNK_LOCALS_EXACT = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "notification", "alerts", "alert",
    "bounce", "bounces", "mailer-daemon", "mailer",
    "postmaster", "hostmaster", "webmaster", "abuse",
    "unsubscribe", "optout", "opt-out",
    "user", "example", "test", "sample", "demo",
    "email", "mail", "name", "domain",
    "styles", "core", "bootstrap", "react", "lodash",
}

JUNK_LOCAL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "bounce",
    "notify", "alert", "automated", "mailer", "daemon", "unsubscribe",
)

# ── Scoring ───────────────────────────────────────────────────────────────────

SCORE_RULES: list[tuple[re.Pattern, int]] = [
    (re.compile(r'^info@'),    +10),
    (re.compile(r'^hello@'),   +8),
    (re.compile(r'^contact@'), +8),
    (re.compile(r'^enquir'),   +7),
    (re.compile(r'^sales@'),   +6),
    (re.compile(r'^support@'), +5),
    (re.compile(r'^hi@'),      +4),
    (re.compile(r'^admin@'),   +3),
    (re.compile(r'^mail@'),    +3),
    (re.compile(r'^team@'),    +2),
]

# Contact page URL heuristic
CONTACT_HREF_RE = re.compile(
    r'/(contact|about|reach|connect|get.in.touch|enquir|inquir|touch|team|staff|people)',
    re.IGNORECASE,
)
CONTACT_PATH_FALLBACKS = [
    "/contact", "/contact-us", "/contact_us",
    "/about",   "/about-us",
    "/reach-us", "/get-in-touch",
]

EMAIL_RE = re.compile(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _bad_chars(s: str) -> bool:
    return any(c in s for c in '"\'(){}[]\\<>`)#$%^*=|~;,')

def _is_version(s: str) -> bool:
    return bool(re.fullmatch(r'v?\d+(\.\d+)*', s))

def _is_hex_hash(s: str) -> bool:
    return bool(re.fullmatch(r'[0-9a-f]{8,}', s, re.IGNORECASE))

def _is_uuid(s: str) -> bool:
    return bool(re.fullmatch(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        s, re.IGNORECASE
    ))

def _js_artifact(local: str) -> bool:
    if re.search(r'\.(js|min|bundle|chunk|css|ts|tsx)$', local):
        return True
    if re.search(r'[A-Z]{2,}', local):
        return True
    if re.search(r'[0-9a-f]{6,}', local):
        return True
    return False

def _score(email: str) -> int:
    s = 0
    for pat, bonus in SCORE_RULES:
        if pat.search(email):
            s += bonus
    local = email.split("@")[0]
    if len(local) > 20:
        s -= 4
    return s

def _keep(email: str, *, from_mailto: bool) -> bool:
    email = email.strip().lower()
    orig = email

    if not email or "@" not in email:
        log.debug(f"  ✗ {orig} — no @")
        return False

    email = re.split(r'[\s>)\]"\']+', email)[0].split("?")[0].rstrip(".,;:")

    if not re.fullmatch(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}', email):
        log.debug(f"  ✗ {orig} — malformed")
        return False

    local, domain = email.split("@", 1)

    if domain in JUNK_DOMAINS:
        log.debug(f"  ✗ {orig} — junk domain"); return False
    if _is_version(domain) or _bad_chars(domain):
        log.debug(f"  ✗ {orig} — bad domain"); return False
    parts = domain.split(".")
    if len(parts) < 2 or len(parts[-1]) < 2:
        log.debug(f"  ✗ {orig} — no real TLD"); return False

    if _bad_chars(local):
        log.debug(f"  ✗ {orig} — bad chars"); return False
    if local in JUNK_LOCALS_EXACT:
        log.debug(f"  ✗ {orig} — junk local"); return False
    if local.startswith(JUNK_LOCAL_PREFIXES):
        log.debug(f"  ✗ {orig} — prefix skip"); return False
    if local.startswith(("_", ".", "+")):
        log.debug(f"  ✗ {orig} — special start"); return False
    if _is_version(local) or _is_uuid(local) or _is_hex_hash(local):
        log.debug(f"  ✗ {orig} — version/uuid/hash"); return False
    if not from_mailto and _js_artifact(local):
        log.debug(f"  ✗ {orig} — JS artifact"); return False

    log.debug(f"  ✓ {orig} (score {_score(orig)})")
    return True

def _merge_ranked(*buckets: list[str]) -> list[str]:
    seen: dict[str, str] = {}
    for bucket in buckets:
        for e in bucket:
            k = e.lower()
            if k not in seen:
                seen[k] = e
    return sorted(seen.values(), key=_score, reverse=True)


# ── DOM extraction ────────────────────────────────────────────────────────────

def _extract_mailto(html: str) -> list[str]:
    out = []
    for m in re.finditer(r'mailto:([^\s"\'?&<>]+)', html, re.IGNORECASE):
        raw = m.group(1).strip().split("?")[0].split("&")[0].rstrip(".,;:")
        if _keep(raw, from_mailto=True):
            out.append(raw.lower())
    return out

def _extract_regex(html: str) -> list[str]:
    out = []
    for m in EMAIL_RE.finditer(html):
        raw = m.group().lower()
        if _keep(raw, from_mailto=False):
            out.append(raw)
    return out

def _find_contact_links(html: str, base_url: str) -> list[str]:
    base = urlparse(base_url)
    seen: set[str] = set()
    out: list[str] = []
    for m in re.finditer(r'href=["\']([^"\'#]+)["\']', html, re.IGNORECASE):
        href = m.group(1).strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:")):
            continue
        if CONTACT_HREF_RE.search(href):
            full = urljoin(base_url, href)
            p = urlparse(full)
            if p.netloc == base.netloc and full not in seen:
                seen.add(full)
                out.append(full)
    return out


# ── Core stealth fetch ────────────────────────────────────────────────────────

async def _stealth_fetch(session: AsyncStealthySession, url: str) -> tuple[str, str]:
    """Fetch a URL and return (resolved_url, html).

    Returns the final URL after redirects so callers can build correct
    fallback paths (e.g. when the input URL is a bit.ly shortener).
    """
    async def process_page(page):
        # 1. Check for shortener warning/preview pages (e.g. Bitly interstitial)
        try:
            target_url = await page.evaluate('''() => {
                const btn = document.getElementById("action:continue") || 
                            document.getElementById("action:continue_sm") || 
                            document.querySelector("a.preview__continue") ||
                            document.querySelector("a[id*='action:']");
                return btn ? btn.href : null;
            }''')
            if target_url:
                log.debug(f"  ↳ Shortener preview page detected. Navigating to {target_url}...")
                await page.goto(target_url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
        except Exception:
            pass

        # 2. Adaptive scrolling & button clicking on the final page
        script = """
        (async () => {
            // 1. Fully adaptive scrolling to load lazy-loaded elements/footers
            let lastHeight = document.body.scrollHeight;
            let sameHeightCount = 0;
            for (let i = 0; i < 20; i++) {
                window.scrollBy(0, window.innerHeight);
                await new Promise(r => setTimeout(r, 400));
                let currentHeight = document.body.scrollHeight;
                let isAtBottom = window.innerHeight + window.scrollY >= currentHeight - 100;
                if (currentHeight === lastHeight) {
                    sameHeightCount++;
                    if (sameHeightCount >= 2 && isAtBottom) {
                        break;
                    }
                } else {
                    sameHeightCount = 0;
                }
                lastHeight = currentHeight;
            }

            // 2. Click contact, about, or email reveal buttons to trigger modals/dynamic DOM changes
            const keywords = [
                'contact', 'about', 'enquiry', 'inquiry', 'get in touch', 
                'reach us', 'email', 'mail', 'write to us', 'show email', 'reveal email'
            ];
            const elements = Array.from(document.querySelectorAll('button, a, [role="button"], .btn, .button'));
            let clickedCount = 0;

            for (const el of elements) {
                const text = (el.innerText || el.textContent || '').toLowerCase().trim();
                const ariaLabel = (el.getAttribute('aria-label') || '').toLowerCase();
                const title = (el.getAttribute('title') || '').toLowerCase();
                const id = (el.getAttribute('id') || '').toLowerCase();
                const combined = `${text} ${ariaLabel} ${title} ${id}`;

                const matchesKeyword = keywords.some(kw => combined.includes(kw));
                if (!matchesKeyword) continue;

                const href = el.getAttribute('href');
                const isRealLink = href && !href.startsWith('javascript:') && !href.startsWith('#') && !href.startsWith('mailto:') && !href.startsWith('tel:');

                if (!isRealLink) {
                    try {
                        el.click();
                        clickedCount++;
                        await new Promise(r => setTimeout(r, 500));
                    } catch (e) {
                        // Ignore click/action errors
                    }
                }
            }
            return clickedCount;
        })()
        """
        try:
            clicked = await page.evaluate(script)
            if clicked and clicked > 0:
                log.debug(f"  ↳ clicked {clicked} contact/reveal button(s)")
                await asyncio.sleep(0.5)
        except Exception as e:
            # Silence evaluation errors
            pass

    try:
        resp = await session.fetch(
            url,
            page_action=process_page,
            network_idle=NETWORK_WAIT,
            disable_resources=True,
            timeout=PAGE_TIMEOUT,
        )

        resolved_url = getattr(resp, "url", url) or url

        # Skip error responses — their HTML is useless for email extraction
        status = getattr(resp, "status", 200)
        if status >= 400:
            log.debug(f"  ✗ HTTP {status} for {url} — skipping")
            return resolved_url, ""

        body = resp.body
        if isinstance(body, bytes):
            html = body.decode("utf-8", errors="replace")
        else:
            html = body or ""

        return resolved_url, html
    except Exception as exc:
        # Log the error so we know *why* extraction failed
        short = str(exc).split("\n")[0][:120]
        log.debug(f"  ✗ fetch failed for {url}: {short}")
        return url, ""



# ── Public API ────────────────────────────────────────────────────────────────

async def fetch_emails(website_url: str, session: AsyncStealthySession | None = None) -> list[str]:
    if not website_url:
        return []

    async def _crawl(sess: AsyncStealthySession) -> list[str]:
        pages_fetched = 0
        fetched: dict[str, str] = {}

        async def get(url: str) -> tuple[str, str]:
            """Returns (resolved_url, html). Deduplicates by input URL."""
            nonlocal pages_fetched
            if pages_fetched >= MAX_PAGES or url in fetched:
                return url, fetched.get(url, "")
            pages_fetched += 1
            log.debug(f"  [{pages_fetched}/{MAX_PAGES}] {url}")
            resolved_url, html = await _stealth_fetch(sess, url)
            fetched[url] = html
            # If the URL redirected (e.g. bit.ly → real domain), also cache
            # the resolved URL so we don't re-fetch it later.
            if resolved_url != url:
                fetched[resolved_url] = html
                log.debug(f"  ↳ resolved to {resolved_url}")
            return resolved_url, html

        # 1. Fetch the homepage (may be a shortener → follow redirect)
        resolved_home, homepage_html = await get(website_url)
        mailto_home = _extract_mailto(homepage_html)

        # 2. Build the real base URL from the *resolved* domain, not the
        #    original (which could be bit.ly, tinyurl, etc.)
        real_base_url = resolved_home if resolved_home != website_url else website_url
        contact_links = _find_contact_links(homepage_html, real_base_url)
        if not contact_links:
            parsed = urlparse(real_base_url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            contact_links = [base + p for p in CONTACT_PATH_FALLBACKS]

        mailto_pages: list[str] = []
        for link in contact_links:
            if pages_fetched >= MAX_PAGES:
                break
            _, html = await get(link)
            if html:
                mailto_pages.extend(_extract_mailto(html))
            best = _merge_ranked(mailto_home, mailto_pages)
            if best and _score(best[0]) >= 8:
                return best

        all_mailto = _merge_ranked(mailto_home, mailto_pages)
        if all_mailto:
            return all_mailto

        regex_hits: list[str] = []
        for html in fetched.values():
            regex_hits.extend(_extract_regex(html))
        return _merge_ranked(regex_hits)

    if session is not None:
        return await _crawl(session)

    async with AsyncStealthySession(headless=True, disable_resources=True) as sess:
        return await _crawl(sess)


# ---------------------------------------------------------------------------
# Google Maps scraper
# ---------------------------------------------------------------------------

async def main():
    if len(sys.argv) < 2:
        print("Usage: uv run main.py <search query>")
        print("Example: uv run main.py Dentists in Miami")
        return

    search_query = " ".join(sys.argv[1:])
    async with AsyncStealthySession(headless=True) as session:
        url = f"https://www.google.com/maps/search/{search_query.replace(' ', '+')}"
        feed_sel = 'div[role="feed"]'

        async def scroll_feed(page):
            await page.wait_for_selector(feed_sel, timeout=15000)
            last_height = 0
            same_height_count = 0
            for scroll_idx in range(50):
                await page.evaluate("document.querySelector('div[role=\"feed\"]').scrollBy(0, 1500)")
                await asyncio.sleep(2.0)
                current_height = await page.evaluate("document.querySelector('div[role=\"feed\"]').scrollHeight")
                
                reached_end = await page.evaluate('''() => {
                    const el = Array.from(document.querySelectorAll('span, div')).find(
                        e => e.textContent && e.textContent.includes("You've reached the end of the list")
                    );
                    return !!el;
                }''')
                if reached_end:
                    log.debug("  ↳ Google Maps feed reached the end (text indicator found)")
                    break
                    
                if current_height == last_height:
                    same_height_count += 1
                    if same_height_count >= 3:
                        log.debug("  ↳ Google Maps feed height unchanged - stopping scroll")
                        break
                else:
                    same_height_count = 0
                last_height = current_height

        resp = await session.fetch(
            url,
            page_action=scroll_feed,
            wait_selector=feed_sel,
            network_idle=True,
            timeout=60000,
        )

        feed = resp.css(feed_sel).first
        leads = []

        for article in feed.css('div[role="article"]'):
            link_elem = article.css("a.hfpxzc").first
            if not link_elem:
                continue
            href = link_elem.attrib.get("href", "")
            if "/maps/place/" not in href:
                continue

            name = article.css(".qBF1Pd::text").get(default="").strip()
            rating = article.css(".MW4etd::text").get(default="").strip()
            category = article.css(".W4Efsd span span:first-child::text").get(default="").strip()

            leads.append({"Name": name, "Rating": rating, "Category": category, "URL": href})

        safe_name = search_query.replace(" ", "_").lower()
        csv_file = f"{safe_name}.csv"
        fields = ["Name", "Phone", "Email", "Website", "Address", "Rating", "Category", "URL"]
        fout = open(csv_file, "w", newline="")
        writer = csv.DictWriter(fout, fieldnames=fields)
        writer.writeheader()
        fout.flush()

        log.info(f"Found {len(leads)} leads  →  {csv_file}")

        for i, lead in enumerate(leads):
            log.info(f"  [{i+1}/{len(leads)}] {lead['Name']}")
            phone, website, address = await fetch_place_details(session, lead["URL"])
            lead["Phone"] = phone
            lead["Website"] = website
            lead["Address"] = address
            lead["Email"] = "; ".join(await fetch_emails(website, session)) if website else ""
            if lead["Email"]:
                log.info(f"  \033[36m\u2709 {lead['Email']}\033[0m")  # cyan email
            writer.writerow({k: str(lead.get(k, "")) for k in fields})
            fout.flush()

        fout.close()

        log.info(f"\n{'='*80}")
        log.info(f"{'Name':28s} {'Phone':16s} {'Email':30s} {'Rating':6s}")
        log.info(f"{'='*80}")
        for lead in leads:
            email = lead.get("Email", "")[:28]
            log.info(f"{lead['Name'][:26]:28s} {lead['Phone'][:14]:16s} {email:30s} {lead['Rating']:6s}")


if __name__ == "__main__":
    asyncio.run(main())
