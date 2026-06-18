"""
Output module — CSV + JSON writer with CSV-based checkpointing.

Phase 1: All leads are written to CSV with Visited=no (the CSV IS the checkpoint).
Phase 2: Unvisited rows are loaded, processed one-by-one, and updated in place.
Resume: CSV is read, rows with Visited=no are processed.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("maps_scraper.output")

FIELDS = ["Name", "Phone", "Email", "Website", "Address", "Rating", "Category", "URL", "Visited"]

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

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


def validate_email(email: str) -> bool:
    if not email or not _EMAIL_RE.match(email):
        return False
    email_lower = email.lower()
    for prefix in _REJECT_PREFIXES:
        if email_lower.startswith(prefix):
            return False
    domain = email_lower.split("@", 1)[1] if "@" in email_lower else ""
    for bad_domain in _REJECT_DOMAINS:
        if domain == bad_domain:
            return False
    if email_lower.endswith((".png", ".jpg", ".gif", ".svg", ".css", ".js", ".ico")):
        return False
    return True


def _extract_domain(url: str) -> str:
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        if domain.startswith("www."):
            domain = domain[4:]
        return domain.lower()
    except Exception:
        return url.lower()


class OutputWriter:
    """
    CSV-first output with built-in checkpointing via Visited column.

    Usage:
      writer = OutputWriter(csv_path="leads.csv")
      writer.open()

      # Phase 1: write all scraped leads
      writer.write_initial_leads(leads)

      # Phase 2: load unvisited, process each
      for i, lead in writer.iter_unvisited():
          ...  # fetch details, extract email
          writer.update_row(i, lead)

      writer.close()
    """

    def __init__(
        self,
        csv_path: str | None = None,
        json_path: str | None = None,
        dedupe: bool = False,
        resume: bool = False,
    ) -> None:
        self._csv_path = csv_path
        self._json_path = json_path
        self._dedupe = dedupe
        self._resume = resume

        self._csv_file = None
        self._csv_writer = None
        self._json_rows: list[dict[str, str]] = []

        # In-memory row store for Phase 2 updates
        self._rows: list[dict[str, str]] = []
        self._next_index: int = 0
        self._start_index: int = 0

        self.rows_written: int = 0
        self.rows_skipped_dupe: int = 0

    # ── File lifecycle ─────────────────────────────────────────────────────

    def open(self) -> None:
        """Open the CSV for writing."""
        if not self._csv_path:
            return
        mode = "a" if self._resume else "w"
        self._csv_file = open(self._csv_path, mode, newline="", encoding="utf-8")
        self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=FIELDS)
        self._csv_writer.writeheader()
        self._csv_file.flush()

    def close(self) -> None:
        """Close files and write JSON if configured."""
        if self._csv_file:
            self._csv_file.close()
            self._csv_file = None
            self._csv_writer = None
        if self._json_path and self._json_rows:
            Path(self._json_path).write_text(
                json.dumps(self._json_rows, indent=2, ensure_ascii=False)
            )
            log.info("JSON output: %s (%d rows)", self._json_path, len(self._json_rows))

    # ── Dedup helpers ──────────────────────────────────────────────────────

    def _build_dedup_sets(self) -> tuple[set[str], set[str]]:
        seen_domains: set[str] = set()
        seen_phones: set[str] = set()
        for row in self._rows:
            website = row.get("Website", "").strip()
            phone = row.get("Phone", "").strip()
            if website:
                seen_domains.add(_extract_domain(website))
            if phone:
                seen_phones.add(phone)
        return seen_domains, seen_phones

    def _is_duplicate(self, lead: dict[str, Any], seen_domains: set[str], seen_phones: set[str]) -> bool:
        if not self._dedupe:
            return False
        website = str(lead.get("Website", "")).strip()
        phone = str(lead.get("Phone", "")).strip()
        if website and _extract_domain(website) in seen_domains:
            return True
        if phone and phone in seen_phones:
            return True
        return False

    # ── Phase 1: Write initial leads ───────────────────────────────────────

    def write_initial_leads(self, leads: list[dict[str, Any]]) -> None:
        """
        Phase 1: Write all scraped leads to CSV with Visited=no.
        The CSV becomes the checkpoint for resuming.
        """
        row_count = 0
        seen_domains, seen_phones = set(), set()
        for lead in leads:
            if self._is_duplicate(lead, seen_domains, seen_phones):
                self.rows_skipped_dupe += 1
                continue
            website = str(lead.get("Website", "")).strip()
            phone = str(lead.get("Phone", "")).strip()
            if website:
                seen_domains.add(_extract_domain(website))
            if phone:
                seen_phones.add(phone)

            row = {k: str(lead.get(k, "")) for k in FIELDS[:-1]}
            row["Visited"] = "no"
            self._rows.append(row)
            self.rows_written += 1
            row_count += 1

            if self._csv_writer:
                self._csv_writer.writerow(row)
                if self._csv_file:
                    self._csv_file.flush()

        log.info("Phase 1: wrote %d leads to %s", row_count, self._csv_path)
        self._start_index = 0
        self._next_index = 0

    # ── Phase 2: Iterate unvisited leads ───────────────────────────────────

    def load_csv(self) -> int:
        """
        Load all rows from the existing CSV into memory.
        Returns the number of unvisited (remaining) leads.
        """
        if not self._csv_path or not Path(self._csv_path).exists():
            return 0

        self._rows = []
        with open(self._csv_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Normalise fields to match FIELDS
                normalised = {}
                for k in FIELDS:
                    normalised[k] = row.get(k, "")
                self._rows.append(normalised)

        unvisited_count = sum(1 for r in self._rows if r.get("Visited", "").lower() != "yes")
        total = len(self._rows)
        done = total - unvisited_count
        log.info("CSV loaded: %d total, %d done, %d remaining", total, done, unvisited_count)

        # Find first unvisited index
        self._next_index = next(
            (i for i, r in enumerate(self._rows) if r.get("Visited", "").lower() != "yes"),
            total,
        )
        self._start_index = self._next_index
        return unvisited_count

    def iter_unvisited(self):
        """
        Generator yielding (index, row) for each unvisited lead.
        Call update_row(index, data) after processing to mark visited.
        """
        while self._next_index < len(self._rows):
            row = self._rows[self._next_index]
            if row.get("Visited", "").lower() == "yes":
                self._next_index += 1
                continue
            yield self._next_index, dict(row)
            self._next_index += 1

    @property
    def remaining(self) -> int:
        return len(self._rows) - self._next_index

    # ── Update a row (mark visited + fill data) ────────────────────────────

    def update_row(self, index: int, data: dict[str, Any]) -> None:
        """
        Update a row with scraped data and mark it Visited=yes.
        Rewrites the entire CSV to disk as checkpoint.
        """
        if index >= len(self._rows):
            return

        for k in ("Phone", "Email", "Website", "Address"):
            if k in data and data[k]:
                self._rows[index][k] = str(data[k])

        self._rows[index]["Visited"] = "yes"

        self._flush_csv()

    def _flush_csv(self) -> None:
        """Rewrite entire CSV from in-memory rows (crash-safe checkpoint)."""
        if not self._csv_path:
            return

        # Close old handle before replacing
        if self._csv_file:
            try:
                self._csv_file.close()
            except Exception:
                pass
            self._csv_file = None
            self._csv_writer = None

        tmp = self._csv_path + ".tmp"
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(self._rows)
            os.replace(tmp, self._csv_path)

            # Re-open for future append writes
            self._csv_file = open(self._csv_path, "a", newline="", encoding="utf-8")
            self._csv_writer = csv.DictWriter(self._csv_file, fieldnames=FIELDS)
            self._csv_file.flush()
        except Exception as e:
            log.warning("Failed to flush CSV: %s", e)

    # ── Results ────────────────────────────────────────────────────────────

    def get_results(self) -> list[dict[str, str]]:
        """Return all written rows for report generation."""
        return self._rows

    def write_row(self, lead: dict[str, Any]) -> bool:
        """
        Single-row write (used by legacy paths).
        Writes leads with Visited=yes directly.
        """
        row = {k: str(lead.get(k, "")) for k in FIELDS[:-1]}
        row["Visited"] = "yes"
        self._rows.append(row)
        self.rows_written += 1
        if self._csv_writer:
            self._csv_writer.writerow(row)
            if self._csv_file:
                self._csv_file.flush()
        if self._json_path:
            self._json_rows.append(row)
        return True


def generate_report(
    query: str,
    results: list[dict[str, str]],
    key_pool_status: list[dict] | None,
    start_time: float,
    end_time: float,
    query_slug: str = "",
) -> str:
    """Generate a formatted post-run stats report."""
    import time
    from collections import Counter

    total = len(results)
    with_phone = sum(1 for r in results if r.get("Phone", "").strip())
    with_website = sum(1 for r in results if r.get("Website", "").strip())
    with_email = sum(1 for r in results if r.get("Email", "").strip())

    categories = Counter(r.get("Category", "").strip() for r in results if r.get("Category", "").strip())
    top_cats = categories.most_common(5)

    ratings = []
    for r in results:
        try:
            ratings.append(float(r.get("Rating", "0")))
        except (ValueError, TypeError):
            pass
    avg_rating = sum(ratings) / len(ratings) if ratings else 0.0

    elapsed = end_time - start_time
    h, rem = divmod(int(elapsed), 3600)
    m, s = divmod(rem, 60)
    elapsed_str = f"{h:02d}:{m:02d}:{s:02d}"

    throughput = total / (elapsed / 60) if elapsed > 0 else 0

    lines = [
        "=" * 60,
        "  📊 SCRAPER REPORT",
        "=" * 60,
        f"  Query:          {query}",
        f"  Total leads:    {total}",
        f"  With phone:     {with_phone} ({with_phone/total*100:.1f}%)" if total else "  With phone:     0",
        f"  With website:   {with_website} ({with_website/total*100:.1f}%)" if total else "  With website:   0",
        f"  With email:     {with_email} ({with_email/total*100:.1f}%)" if total else "  With email:     0",
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

    if query_slug:
        report_path = Path(f"{query_slug}_report.txt")
        report_path.write_text(report)

    return report
