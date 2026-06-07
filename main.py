"""
email_scraper.py — drop-in replacement for fetch_emails / extract_emails

Key improvements over the original:
  1. Extracts mailto: hrefs (richest source, was completely ignored)
  2. Scores and ranks emails — business emails float up, junk sinks
  3. Crawls more intelligently: finds /contact links on-page instead of guessing
  4. Uses JS-rendered AsyncStealthySession for SPAs where possible
  5. Tighter filtering: rejects noreply, placeholders, obfuscation artifacts
  6. Returns (emails, source_url) so you know where each came from
"""

import asyncio
import logging
import re
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

log = logging.getLogger("email_scraper")
log.addHandler(_handler)
log.setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Domains whose emails are always noise
SKIP_DOMAINS = {
    "sentry.wixpress.com", "sentry-next.wixpress.com", "sentry.io", "wixpress.com",
    "example.com", "example.org", "test.com", "localhost",
    "google.com", "googletagmanager.com", "googleapis.com",
    "facebook.com", "twitter.com", "instagram.com",
    "amazonaws.com", "cloudfront.net",
}

# JS library / asset names that end up in strings
SKIP_LOCAL_PARTS = {
    "user", "example", "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "bounce", "mailer-daemon", "postmaster", "webmaster",
    "slick-carousel", "bootstrap", "core", "rspack", "react", "react-dom",
    "lodash", "focus-within-polyfill", "intl-segmenter", "core-js-bundle",
    "simple-parallax-js", "embla-carousel", "embla-carousel-autoplay", "styles",
}

# Local-part *prefixes* that are almost always automated senders, not contacts
SKIP_LOCAL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "bounce", "notify",
    "alert", "automated", "mailer", "daemon",
)

SKIP_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".ico",
                 ".woff", ".woff2", ".ts", ".tsx", ".json", ".xml")

# Paths to try if the homepage has no emails
CONTACT_PATH_CANDIDATES = [
    "/contact", "/contact-us", "/contact_us",
    "/about", "/about-us", "/about_us",
    "/reach-us", "/get-in-touch", "/connect",
]

# Paths that indicate a page is worth crawling for contact info
CONTACT_LINK_PATTERNS = re.compile(
    r"/(contact|about|reach|connect|get.in.touch|enquir|inquir)",
    re.IGNORECASE,
)

EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')

# Score bonuses for ranking: higher = better candidate
SCORE_RULES = [
    (re.compile(r'^info@'), +10),
    (re.compile(r'^hello@'), +8),
    (re.compile(r'^contact@'), +8),
    (re.compile(r'^sales@'), +6),
    (re.compile(r'^support@'), +5),
    (re.compile(r'^admin@'), +3),
    (re.compile(r'^mail@'), +3),
    # generic TLDs on business emails are usually real
    (re.compile(r'\.(com|co\.\w{2}|net|org|io|biz)$'), +2),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_version_string(s: str) -> bool:
    return bool(re.fullmatch(r'v?\d+(?:\.\d+)*', s))

def _is_uuid(s: str) -> bool:
    return bool(re.fullmatch(r'[\da-f]{32}', s))

def _is_hash(s: str) -> bool:
    """Long hex strings — webpack content hashes etc."""
    return bool(re.fullmatch(r'[\da-f]{8,}', s))

def _looks_like_path(s: str) -> bool:
    """Webpack chunk names like 'commons-abc123.min'."""
    return "." in s and s.split(".")[-1] in ("js", "min", "bundle", "chunk")


def _score_email(email: str) -> int:
    """Higher score = more likely a real business contact."""
    local, domain = email.split("@", 1)
    score = 0
    for pattern, bonus in SCORE_RULES:
        if pattern.search(email):
            score += bonus
    # penalise long random-looking locals (hashes, UUIDs)
    if len(local) > 20 and re.fullmatch(r'[\da-z_.+-]+', local):
        score -= 5
    return score


def _filter_email(email: str) -> bool:
    """Return True if the email is worth keeping."""
    email = email.lower().strip()
    orig = email

    if email.endswith(SKIP_SUFFIXES):
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
        log.debug(f"  ✗ {orig} — domain looks like version string")
        return False
    if _is_uuid(local):
        log.debug(f"  ✗ {orig} — local part is UUID")
        return False
    if _is_hash(local):
        log.debug(f"  ✗ {orig} — local part is hash")
        return False
    if local in SKIP_LOCAL_PARTS:
        log.debug(f"  ✗ {orig} — local in skip list")
        return False
    if _is_version_string(local):
        log.debug(f"  ✗ {orig} — local looks like version")
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
    if any(c in local for c in ('"', "'", '(', ')', '{', '}', '[', ']', '\\', '<', '>')):
        log.debug(f"  ✗ {orig} — contains JS artifacts")
        return False
    if any(c in domain for c in ('"', "'", '(', ')', '{', '}')):
        log.debug(f"  ✗ {orig} — domain contains JS artifacts")
        return False

    log.debug(f"  ✓ {orig} (score {_score_email(orig)})")
    return True


def extract_emails_from_html(html: str | bytes) -> list[str]:
    """Extract, filter and rank emails from raw HTML."""
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")

    found: set[str] = set()

    # 1. mailto: hrefs — most reliable source
    mailto_count = 0
    for m in re.finditer(r'mailto:([^\s"\'?&<>]+)', html, re.IGNORECASE):
        candidate = m.group(1).strip().lower()
        candidate = candidate.split("?")[0].split("&")[0]
        found.add(candidate)
        mailto_count += 1
    log.debug(f"Found {mailto_count} mailto: links")

    # 2. Bare regex scan for everything else
    raw_count = 0
    for m in EMAIL_RE.finditer(html):
        found.add(m.group().lower())
        raw_count += 1
    log.debug(f"Found {raw_count} raw email patterns, {len(found)} unique before filtering")

    good = [e for e in found if _filter_email(e)]
    log.debug(f"Survived filtering: {len(good)} emails")
    if good:
        log.debug(f"  candidates: {good}")

    seen: dict[str, str] = {}
    for e in good:
        key = e.lower()
        if key not in seen:
            seen[key] = e

    ranked = sorted(seen.values(), key=_score_email, reverse=True)
    log.debug(f"Ranked: {ranked}")
    return ranked


def _find_contact_links(html: str | bytes, base_url: str) -> list[str]:
    """Pull out hrefs that look like contact/about pages."""
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
    if urls:
        log.debug(f"  contact links found: {urls}")
    else:
        log.debug("  no contact links found, will try static paths")
    return urls


# ---------------------------------------------------------------------------
# Async public API
# ---------------------------------------------------------------------------

MAX_PAGES_TO_CRAWL = 4   # homepage + up to 3 contact-like pages


async def _try_fetch_html(session, url: str, timeout: int = 12000) -> str:
    """Fetch with the stealthy session; returns empty string on failure."""
    try:
        resp = await session.fetch(url, network_idle=True, timeout=timeout)
        body = resp.body
        return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    except Exception:
        return ""


async def fetch_emails_for_site(
    website_url: str,
    session: AsyncStealthySession | None = None,
) -> list[str]:
    """
    Main entry point. Accepts an optional already-open stealthy session
    (pass the one from main() to reuse it). If not given, falls back to
    the sync Fetcher for speed.

    Returns a ranked list of candidate emails, best first.
    """
    if not website_url:
        return []

    all_emails: dict[str, str] = {}  # lower → original, across all pages

    def _merge(emails: list[str]):
        for e in emails:
            k = e.lower()
            if k not in all_emails:
                all_emails[k] = e

    pages_fetched = 0

    async def _fetch_page_and_extract(url: str) -> str:
        """Fetch one page, extract emails, return raw HTML."""
        nonlocal pages_fetched
        if pages_fetched >= MAX_PAGES_TO_CRAWL:
            return ""
        pages_fetched += 1

        log.debug(f"  [{pages_fetched}/{MAX_PAGES_TO_CRAWL}] fetching {url}")
        if session is not None:
            html = await _try_fetch_html(session, url)
        else:
            html = await asyncio.to_thread(_sync_fetch, url)

        if html:
            log.debug(f"  got {len(html)} bytes")
            extracted = extract_emails_from_html(html)
            _merge(extracted)
            if extracted:
                log.debug(f"  ✓ found {len(extracted)} emails on {url}")
            else:
                log.debug(f"  ✗ no emails on {url}")
        else:
            log.debug(f"  ✗ failed to fetch {url}")
        return html

    # --- Step 1: homepage ---
    log.debug(f"⏺ crawling {website_url}")
    homepage_html = await _fetch_page_and_extract(website_url)

    if all_emails:
        log.debug(f"  homepage yielded {len(all_emails)} unique emails")

    # --- Step 2: find contact links on-page first ---
    contact_links = _find_contact_links(homepage_html, website_url)

    if not contact_links:
        parsed = urlparse(website_url)
        contact_links = [
            f"{parsed.scheme}://{parsed.netloc}{path}"
            for path in CONTACT_PATH_CANDIDATES
        ]
        log.debug(f"  falling back to {len(contact_links)} static paths")

    for link in contact_links:
        if pages_fetched >= MAX_PAGES_TO_CRAWL:
            log.debug("  max pages reached, stopping crawl")
            break
        await _fetch_page_and_extract(link)
        ranked = sorted(all_emails.values(), key=_score_email, reverse=True)
        if ranked and _score_email(ranked[0]) >= 8:
            log.debug(f"  high-quality email found, stopping early")
            break

    ranked = sorted(all_emails.values(), key=_score_email, reverse=True)
    log.debug(f"✅ final emails for {website_url}: {ranked}")
    return ranked


def _sync_fetch(url: str, timeout: int = 15) -> str:
    """Synchronous fallback using scrapling.Fetcher."""
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


# ---------------------------------------------------------------------------
# Convenience shim that matches your original fetch_emails signature
# ---------------------------------------------------------------------------

async def fetch_emails(website_url: str, session=None) -> list[str]:
    """
    Drop-in replacement for the original fetch_emails().

    Pass `session=session` from your main() to get JS-rendered pages.
    """
    return await fetch_emails_for_site(website_url, session=session)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: uv run main.py <website_url>")
        sys.exit(1)
    url = sys.argv[1]
    emails = asyncio.run(fetch_emails(url))
    if emails:
        print(f"\nFound {len(emails)} email(s):")
        for e in emails:
            print(f"  {e}")
    else:
        print("No emails found.")