"""
Output module — CSV + JSON writer with duplicate detection and email validation.

Handles writing scraper results to CSV and/or JSON formats,
with in-memory deduplication by website domain and phone number.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("maps_scraper.output")

FIELDS = ["Name", "Phone", "Email", "Website", "Address", "Rating", "Category"]

# ── Email validation patterns (reject obviously fake emails) ───────────────

_REJECT_PREFIXES = [
    "noreply@", "no-reply@", "no_reply@",
    "donotreply@", "do-not-reply@",
    "test@", "example@", "admin@admin",
    "info@info", "webmaster@webmaster",
    "support@support", "mail@mail",
    "postmaster@", "mailer-daemon@",
]

_REJECT_DOMAINS = [
    "example.com", "example.org", "example.net",
    "test.com", "test.org",
    "sentry.io", "wixpress.com",
    "googleapis.com", "google.com",
]

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


def validate_email(email: str) -> bool:
    """
    Validate an extracted email address.
    Returns True if the email looks legitimate, False if it should be rejected.
    """
    if not email or not _EMAIL_RE.match(email):
        return False

    email_lower = email.lower()

    # Check rejected prefixes
    for prefix in _REJECT_PREFIXES:
        if email_lower.startswith(prefix):
            return False

    # Check rejected domains
    domain = email_lower.split("@", 1)[1] if "@" in email_lower else ""
    for bad_domain in _REJECT_DOMAINS:
        if domain == bad_domain:
            return False

    # Reject emails with file extensions (image/css/js filenames)
    if email_lower.endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js", ".ico")):
        return False

    return True


def _extract_domain(url: str) -> str:
    """Extract the domain from a URL for deduplication."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        # Remove www. prefix
        if domain.startswith("www."):
            domain = domain[4:]
        return domain.lower()
    except Exception:
        return url.lower()


class OutputWriter:
    """
    Writes scraped leads to CSV and/or JSON with deduplication.

    Supports append mode and in-memory duplicate detection
    by website domain and phone number.
    """

    def __init__(
        self,
        csv_path: str | None = None,
        json_path: str | None = None,
        append: bool = False,
        dedupe: bool = False,
    ) -> None:
        self._csv_path = csv_path
        self._json_path = json_path
        self._append = append
        self._dedupe = dedupe

        # In-memory dedup sets
        self._seen_domains: set[str] = set()
        self._seen_phones: set[str] = set()

        # JSON accumulator
        self._json_rows: list[dict[str, str]] = []

        # CSV file handle
        self._csv_file = None
        self._csv_writer = None

        # Results tracking
        self.rows_written: int = 0
        self.rows_skipped_dupe: int = 0

    def open(self) -> None:
        """Open output files for writing."""
        if self._csv_path:
            mode = "a" if self._append else "w"
            self._csv_file = open(self._csv_path, mode, newline="", encoding="utf-8")
            self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=FIELDS)
            if not self._append:
                self._csv_writer.writeheader()
            self._csv_file.flush()

            # If appending, load existing rows for dedup
            if self._append and self._dedupe:
                self._load_existing_for_dedup()

        # If appending JSON, load existing data
        if self._json_path and self._append:
            json_file = Path(self._json_path)
            if json_file.exists():
                try:
                    self._json_rows = json.loads(json_file.read_text())
                except Exception:
                    self._json_rows = []

    def _load_existing_for_dedup(self) -> None:
        """Load existing CSV rows to populate dedup sets."""
        if not self._csv_path:
            return
        try:
            with open(self._csv_path, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    website = row.get("Website", "").strip()
                    phone = row.get("Phone", "").strip()
                    if website:
                        self._seen_domains.add(_extract_domain(website))
                    if phone:
                        self._seen_phones.add(phone)
        except Exception:
            pass

    def is_duplicate(self, lead: dict[str, Any]) -> bool:
        """Check if a lead is a duplicate by website domain or phone."""
        if not self._dedupe:
            return False

        website = str(lead.get("Website", "")).strip()
        phone = str(lead.get("Phone", "")).strip()

        if website:
            domain = _extract_domain(website)
            if domain and domain in self._seen_domains:
                return True

        if phone and phone in self._seen_phones:
            return True

        return False

    def write_row(self, lead: dict[str, Any]) -> bool:
        """
        Write a single lead row to output files.
        Returns True if written, False if skipped (duplicate).
        """
        # Check for duplicates
        if self.is_duplicate(lead):
            self.rows_skipped_dupe += 1
            log.info("Skipped duplicate: %s", lead.get("Name", "?"))
            return False

        # Track for future dedup
        website = str(lead.get("Website", "")).strip()
        phone = str(lead.get("Phone", "")).strip()
        if website:
            self._seen_domains.add(_extract_domain(website))
        if phone:
            self._seen_phones.add(phone)

        # Build the row dict
        row = {k: str(lead.get(k, "")) for k in FIELDS}

        # Write to CSV
        if self._csv_writer:
            self._csv_writer.writerow(row)
            if self._csv_file:
                self._csv_file.flush()

        # Accumulate for JSON
        if self._json_path:
            self._json_rows.append(row)

        self.rows_written += 1
        return True

    def close(self) -> None:
        """Close output files and write JSON if needed."""
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None

        if self._json_path and self._json_rows:
            Path(self._json_path).write_text(
                json.dumps(self._json_rows, indent=2, ensure_ascii=False)
            )
            log.info("JSON output: %s (%d rows)", self._json_path, len(self._json_rows))

    def get_results(self) -> list[dict[str, str]]:
        """Return all written rows (for report generation)."""
        if self._csv_path:
            try:
                with open(self._csv_path, "r", newline="", encoding="utf-8") as f:
                    return list(csv.DictReader(f))
            except Exception:
                pass
        return self._json_rows


def generate_report(
    query: str,
    results: list[dict[str, str]],
    key_pool_status: list[dict] | None,
    start_time: float,
    end_time: float,
    query_slug: str = "",
) -> str:
    """
    Generate a formatted post-run stats report.
    Returns the report as a string.
    """
    import time
    from collections import Counter

    total = len(results)
    with_phone = sum(1 for r in results if r.get("Phone", "").strip())
    with_website = sum(1 for r in results if r.get("Website", "").strip())
    with_email = sum(1 for r in results if r.get("Email", "").strip())

    # Top 5 categories
    categories = Counter(r.get("Category", "").strip() for r in results if r.get("Category", "").strip())
    top_cats = categories.most_common(5)

    # Average rating
    ratings = []
    for r in results:
        try:
            ratings.append(float(r.get("Rating", "0")))
        except (ValueError, TypeError):
            pass
    avg_rating = sum(ratings) / len(ratings) if ratings else 0.0

    # Run time
    elapsed = end_time - start_time
    h, rem = divmod(int(elapsed), 3600)
    m, s = divmod(rem, 60)
    elapsed_str = f"{h:02d}:{m:02d}:{s:02d}"

    # Throughput
    throughput = total / (elapsed / 60) if elapsed > 0 else 0

    lines = [
        "=" * 60,
        "  📊 SCRAPER REPORT",
        "=" * 60,
        f"  Query:          {query}",
        f"  Total leads:    {total}",
        f"  With phone:     {with_phone} ({with_phone/total*100:.1f}%)" if total else f"  With phone:     0",
        f"  With website:   {with_website} ({with_website/total*100:.1f}%)" if total else f"  With website:   0",
        f"  With email:     {with_email} ({with_email/total*100:.1f}%)" if total else f"  With email:     0",
        "",
        "  Top categories:",
    ]
    for cat, count in top_cats:
        lines.append(f"    • {cat}: {count}")
    if not top_cats:
        lines.append("    (none)")

    lines.extend([
        "",
        f"  Avg rating:     {avg_rating:.2f}",
        f"  Run time:       {elapsed_str}",
        f"  Throughput:     {throughput:.1f} leads/min",
    ])

    # API key stats
    if key_pool_status:
        lines.append("")
        lines.append("  API key usage:")
        total_api_calls = 0
        for ks in key_pool_status:
            total_api_calls += ks["total_calls"]
            status = "🟢" if not ks["is_rate_limited"] and not ks["is_permanently_bad"] else "🔴"
            lines.append(
                f"    {status} Key {ks['index']}: "
                f"{ks['total_calls']} calls ({ks['successful_calls']} ok, "
                f"{ks['failed_calls']} fail)"
            )
        lines.append(f"  Total API calls: {total_api_calls}")

    lines.append("=" * 60)

    report = "\n".join(lines)

    # Save report to file
    if query_slug:
        report_path = Path(f"{query_slug}_report.txt")
        report_path.write_text(report)

    return report
