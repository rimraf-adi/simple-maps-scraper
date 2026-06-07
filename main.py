"""
Maps Scraper — scrapes Google Maps + extracts emails from business websites.
"""
import asyncio
import csv
import logging
import re
import sys
from urllib.parse import urljoin, urlparse

from scrapling.fetchers import AsyncStealthySession, Fetcher


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
# Email extraction (from the other LLM's rewrite — kept as-is)
# ---------------------------------------------------------------------------

SKIP_DOMAINS = {
    "sentry.wixpress.com", "sentry-next.wixpress.com", "sentry.io", "wixpress.com",
    "example.com", "example.org", "test.com", "localhost",
    "google.com", "googletagmanager.com", "googleapis.com",
    "facebook.com", "twitter.com", "instagram.com",
    "amazonaws.com", "cloudfront.net",
}

SKIP_LOCAL_PARTS = {
    "user", "example", "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "bounce", "mailer-daemon", "postmaster", "webmaster",
    "slick-carousel", "bootstrap", "core", "rspack", "react", "react-dom",
    "lodash", "focus-within-polyfill", "intl-segmenter", "core-js-bundle",
    "simple-parallax-js", "embla-carousel", "embla-carousel-autoplay", "styles",
}

SKIP_LOCAL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "bounce", "notify",
    "alert", "automated", "mailer", "daemon",
)

SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".ico",
                 ".woff", ".woff2", ".ts", ".tsx", ".json", ".xml")

CONTACT_PATH_CANDIDATES = [
    "/contact", "/contact-us", "/contact_us",
    "/about", "/about-us", "/about_us",
    "/reach-us", "/get-in-touch", "/connect",
]

CONTACT_LINK_PATTERNS = re.compile(
    r"/(contact|about|reach|connect|get.in.touch|enquir|inquir)",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')

SCORE_RULES = [
    (re.compile(r'^info@'), +10),
    (re.compile(r'^hello@'), +8),
    (re.compile(r'^contact@'), +8),
    (re.compile(r'^sales@'), +6),
    (re.compile(r'^support@'), +5),
    (re.compile(r'^admin@'), +3),
    (re.compile(r'^mail@'), +3),
    (re.compile(r'\.(com|co\.\w{2}|net|org|io|biz)$'), +2),
]


def _is_version_string(s: str) -> bool:
    return bool(re.fullmatch(r'v?\d+(?:\.\d+)*', s))


def _is_uuid(s: str) -> bool:
    return bool(re.fullmatch(r'[\da-f]{32}', s))


def _is_hash(s: str) -> bool:
    return bool(re.fullmatch(r'[\da-f]{8,}', s))


def _looks_like_path(s: str) -> bool:
    return "." in s and s.split(".")[-1] in ("js", "min", "bundle", "chunk")


def _score_email(email: str) -> int:
    local, domain = email.split("@", 1)
    score = 0
    for pattern, bonus in SCORE_RULES:
        if pattern.search(email):
            score += bonus
    if len(local) > 20 and re.fullmatch(r'[\da-z_.+-]+', local):
        score -= 5
    return score


def _filter_email(email: str) -> bool:
    email = email.lower().strip()
    orig = email

    if email.endswith(SKIP_SUFFIXES):
        log.debug(f"  ✗ {orig} — suffix skipped")
        return False
    if not re.fullmatch(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', email):
        log.debug(f"  ✗ {orig} — malformed")
        return False
    try:
        local, domain = email.split("@", 1)
    except ValueError:
        log.debug(f"  ✗ {orig} — split failed")
        return False

    if domain in SKIP_DOMAINS:
        log.debug(f"  ✗ {orig} — domain in skip list")
        return False
    if _is_version_string(domain):
        log.debug(f"  ✗ {orig} — domain is version string")
        return False
    if _is_uuid(local):
        log.debug(f"  ✗ {orig} — local is UUID")
        return False
    if _is_hash(local):
        log.debug(f"  ✗ {orig} — local is hash")
        return False
    if local in SKIP_LOCAL_PARTS:
        log.debug(f"  ✗ {orig} — local in skip list")
        return False
    if _is_version_string(local):
        log.debug(f"  ✗ {orig} — local is version string")
        return False
    if _looks_like_path(local):
        log.debug(f"  ✗ {orig} — looks like file path")
        return False
    if local.startswith(("_", ".", "+")):
        log.debug(f"  ✗ {orig} — starts with special char")
        return False
    if local.lower().startswith(SKIP_LOCAL_PREFIXES):
        log.debug(f"  ✗ {orig} — noreply/bounce prefix")
        return False
    if any(c in local for c in ('"', "'", "(", ")", "{", "}", "[", "]", "\\", "<", ">")):
        log.debug(f"  ✗ {orig} — JS artifacts in local")
        return False
    if any(c in domain for c in ('"', "'", "(", ")", "{", "}")):
        log.debug(f"  ✗ {orig} — JS artifacts in domain")
        return False

    log.debug(f"  ✓ {orig} (score {_score_email(orig)})")
    return True


def extract_emails_from_html(html: str | bytes) -> list[str]:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")

    found: set[str] = set()

    for m in re.finditer(r'mailto:([^\s"\'?&<>]+)', html, re.IGNORECASE):
        candidate = m.group(1).strip().lower()
        candidate = candidate.split("?")[0].split("&")[0]
        found.add(candidate)
    log.debug(f"  mailto links: {len([e for e in found if _filter_email(e)])} keep")

    for m in EMAIL_RE.finditer(html):
        found.add(m.group().lower())

    good = [e for e in found if _filter_email(e)]
    seen: dict[str, str] = {}
    for e in good:
        key = e.lower()
        if key not in seen:
            seen[key] = e
    ranked = sorted(seen.values(), key=_score_email, reverse=True)
    log.debug(f"  ranked: {ranked}")
    return ranked


def _find_contact_links(html: str | bytes, base_url: str) -> list[str]:
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    urls: list[str] = []
    base = urlparse(base_url)
    for m in re.finditer(r'href=["\']([^"\']+)["\']', html, re.IGNORECASE):
        href = m.group(1).strip()
        if CONTACT_LINK_PATTERNS.search(href):
            full = urljoin(base_url, href)
            parsed = urlparse(full)
            if parsed.netloc == base.netloc:
                urls.append(full)
    urls = list(dict.fromkeys(urls))
    log.debug(f"  contact links: {urls}" if urls else "  no contact links found")
    return urls


def _sync_fetch(url: str, timeout: int = 15) -> str:
    try:
        log.debug(f"  sync fetch {url}")
        resp = Fetcher.get(url, timeout=timeout)
        body = resp.body
        decoded = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
        log.debug(f"  got {len(decoded)} bytes, status {resp.status}")
        return decoded
    except Exception as e:
        log.debug(f"  sync fetch failed: {e}")
        return ""


async def fetch_emails(website_url: str) -> list[str]:
    if not website_url:
        return []

    all_emails: dict[str, str] = {}

    def _merge(emails: list[str]):
        for e in emails:
            k = e.lower()
            if k not in all_emails:
                all_emails[k] = e

    pages_fetched = 0
    max_pages = 4

    async def _fetch_page_and_extract(url: str) -> str:
        nonlocal pages_fetched
        if pages_fetched >= max_pages:
            return ""
        pages_fetched += 1
        log.debug(f"  [{pages_fetched}/{max_pages}] {url}")
        html = await asyncio.to_thread(_sync_fetch, url)
        if html:
            _merge(extract_emails_from_html(html))
        return html

    log.debug(f"crawling {website_url}")
    homepage_html = await _fetch_page_and_extract(website_url)

    contact_links = _find_contact_links(homepage_html, website_url)
    if not contact_links:
        parsed = urlparse(website_url)
        contact_links = [
            f"{parsed.scheme}://{parsed.netloc}{path}"
            for path in CONTACT_PATH_CANDIDATES
        ]
        log.debug(f"  using {len(contact_links)} static fallback paths")

    for link in contact_links:
        if pages_fetched >= max_pages:
            break
        await _fetch_page_and_extract(link)
        ranked = sorted(all_emails.values(), key=_score_email, reverse=True)
        if ranked and _score_email(ranked[0]) >= 8:
            log.debug("  high-quality email found, stopping early")
            break

    ranked = sorted(all_emails.values(), key=_score_email, reverse=True)
    log.debug(f"final: {ranked}")
    return ranked


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
            for _ in range(5):
                await page.evaluate("document.querySelector('div[role=\"feed\"]').scrollBy(0, 1000)")
                await asyncio.sleep(2)

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

        log.info(f"Found {len(leads)} leads. Fetching contact details...")

        for i, lead in enumerate(leads):
            log.info(f"  [{i+1}/{len(leads)}] {lead['Name']}")
            phone, website, address = await fetch_place_details(session, lead["URL"])
            lead["Phone"] = phone
            lead["Website"] = website
            lead["Address"] = address
            lead["Email"] = "; ".join(await fetch_emails(website)) if website else ""

        log.info(f"\n{'='*80}")
        log.info(f"{'Name':28s} {'Phone':16s} {'Email':30s} {'Rating':6s}")
        log.info(f"{'='*80}")
        for lead in leads:
            email = lead.get("Email", "")[:28]
            log.info(f"{lead['Name'][:26]:28s} {lead['Phone'][:14]:16s} {email:30s} {lead['Rating']:6s}")

        safe_name = search_query.replace(" ", "_").lower()
        csv_file = f"{safe_name}.csv"
        with open(csv_file, "w", newline="") as f:
            clean = [{k: str(v) for k, v in lead.items()} for lead in leads]
            writer = csv.DictWriter(f, fieldnames=["Name", "Phone", "Email", "Website", "Address", "Rating", "Category", "URL"])
            writer.writeheader()
            writer.writerows(clean)
        log.info(f"\nSaved {len(leads)} leads to {csv_file}")


if __name__ == "__main__":
    asyncio.run(main())
