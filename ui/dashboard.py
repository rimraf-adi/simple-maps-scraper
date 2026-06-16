"""
Rich Terminal Dashboard — live-updating UI for the Maps Scraper.

Uses rich.live + rich.layout for a multi-panel terminal dashboard
with progress bars, log panel, lead card, and run summary.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    Progress,
    BarColumn,
    TextColumn,
    SpinnerColumn,
    TimeElapsedColumn,
    MofNCompleteColumn,
    TaskProgressColumn,
)
from rich.table import Table
from rich.text import Text


@dataclass
class LeadCard:
    """Current lead being processed."""
    name: str = ""
    category: str = ""
    rating: str = ""
    phone: str = ""
    website: str = ""
    email: str = ""
    status: str = "IDLE"  # SCRAPING / NAVIGATING / FOUND / SKIPPED / FAILED


class Dashboard:
    """Rich-based terminal dashboard for the Maps Scraper."""

    def __init__(self) -> None:
        self.console = Console(force_terminal=True, stderr=True)
        self._live: Live | None = None
        self._lock = Lock()

        # Run summary state
        self.query: str = ""
        self.phase: str = "INITIALIZING"
        self.total_leads: int = 0
        self.processed_leads: int = 0
        self.emails_found: int = 0
        self.active_key_index: int = 1
        self.total_keys: int = 1
        self.model_name: str = ""
        self.start_time: float = time.time()
        self.resumed_count: int = 0

        # Progress bars
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=30),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TextColumn("{task.fields[speed]}"),
            console=self.console,
            expand=True,
        )
        self._scroll_task = self._progress.add_task(
            "Maps Scroll", total=50, completed=0, speed=""
        )
        self._details_task = self._progress.add_task(
            "Fetch Details", total=0, completed=0, speed=""
        )
        self._email_task = self._progress.add_task(
            "Extract Emails", total=0, completed=0, speed=""
        )

        # Log lines
        self._log_lines: deque[Text] = deque(maxlen=20)

        # Current lead card
        self.lead_card = LeadCard()

        # Startup checklist
        self._checklist: list[tuple[str, bool]] = []
        self._showing_checklist: bool = False

    def start(self) -> None:
        """Start the live display."""
        self._live = Live(
            self._build_layout(),
            console=self.console,
            refresh_per_second=4,
            screen=False,
        )
        self._live.start()

    def stop(self) -> None:
        """Stop the live display."""
        if self._live:
            self._live.stop()
            self._live = None

    def _refresh(self) -> None:
        """Refresh the live display with current state."""
        if self._live:
            self._live.update(self._build_layout())

    def _build_layout(self) -> Layout:
        """Build the full dashboard layout."""
        layout = Layout()

        if self._showing_checklist:
            layout.update(self._build_checklist_panel())
            return layout

        layout.split_column(
            Layout(name="top", size=8),
            Layout(name="middle", size=8),
            Layout(name="bottom"),
        )

        # Top: run summary + lead card side by side
        layout["top"].split_row(
            Layout(self._build_summary_panel(), name="summary", ratio=3),
            Layout(self._build_lead_panel(), name="lead", ratio=2),
        )

        # Middle: progress bars
        layout["middle"].update(self._build_progress_panel())

        # Bottom: log panel
        layout["bottom"].update(self._build_log_panel())

        return layout

    def _build_summary_panel(self) -> Panel:
        """Build the run summary panel."""
        elapsed = time.time() - self.start_time
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        elapsed_str = f"{h:02d}:{m:02d}:{s:02d}"

        # Estimate remaining time
        eta_str = "--:--:--"
        if self.processed_leads > 0 and self.total_leads > 0:
            pace = elapsed / self.processed_leads
            remaining = (self.total_leads - self.processed_leads) * pace
            rh, rrem = divmod(int(remaining), 3600)
            rm, rs = divmod(rrem, 60)
            eta_str = f"{rh:02d}:{rm:02d}:{rs:02d}"

        phase_colors = {
            "INITIALIZING": "dim",
            "MAPS SEARCH": "cyan",
            "FETCHING DETAILS": "yellow",
            "EXTRACTING EMAILS": "magenta",
            "DONE": "bold green",
        }
        phase_style = phase_colors.get(self.phase, "white")

        table = Table.grid(padding=(0, 2))
        table.add_column(style="bold cyan", width=16)
        table.add_column()
        table.add_row("Query:", self.query or "—")
        table.add_row("Phase:", Text(self.phase, style=phase_style))
        table.add_row(
            "Leads:",
            f"{self.processed_leads}/{self.total_leads}  "
            f"[green]✉ {self.emails_found}[/green]"
        )
        table.add_row(
            "API Key:",
            f"Key {self.active_key_index}/{self.total_keys}  "
            f"[dim]{self.model_name}[/dim]"
        )
        table.add_row("Elapsed:", f"{elapsed_str}  [dim]ETA {eta_str}[/dim]")
        if self.resumed_count > 0:
            table.add_row("Resumed:", f"[cyan]{self.resumed_count} leads from checkpoint[/cyan]")

        return Panel(table, title="[bold]🗺️  Maps Scraper[/bold]", border_style="bright_blue")

    def _build_lead_panel(self) -> Panel:
        """Build the current lead card panel."""
        lc = self.lead_card
        status_styles = {
            "IDLE": "dim",
            "SCRAPING": "cyan",
            "NAVIGATING": "yellow",
            "FOUND": "bold green",
            "SKIPPED": "dim yellow",
            "FAILED": "red",
        }
        style = status_styles.get(lc.status, "white")

        table = Table.grid(padding=(0, 1))
        table.add_column(style="bold", width=10)
        table.add_column()
        table.add_row("Name:", lc.name or "—")
        table.add_row("Category:", lc.category or "—")
        table.add_row("Rating:", lc.rating or "—")
        table.add_row("Phone:", lc.phone or "—")
        table.add_row("Website:", (lc.website or "—")[:45])
        table.add_row("Email:", Text(lc.email or "—", style="green" if lc.email else "dim"))
        table.add_row("Status:", Text(lc.status, style=style))

        return Panel(table, title="[bold]Current Lead[/bold]", border_style="bright_yellow")

    def _build_progress_panel(self) -> Panel:
        """Build the progress bars panel."""
        return Panel(self._progress, title="[bold]Progress[/bold]", border_style="bright_magenta")

    def _build_log_panel(self) -> Panel:
        """Build the scrolling log panel."""
        if not self._log_lines:
            content = Text("Waiting for events...", style="dim")
        else:
            group_items = list(self._log_lines)
            content = Group(*group_items)

        return Panel(
            content,
            title="[bold]Log[/bold]",
            border_style="bright_green",
        )

    def _build_checklist_panel(self) -> Panel:
        """Build the startup checklist panel."""
        lines: list[Text] = []
        lines.append(Text(""))
        lines.append(Text("  🗺️  Maps Scraper — Starting Up", style="bold bright_blue"))
        lines.append(Text(""))
        for label, done in self._checklist:
            if done:
                icon = Text("  ✅ ", style="green")
            else:
                icon = Text("  ⬜ ", style="dim")
            line = icon + Text(label)
            lines.append(line)
        lines.append(Text(""))

        return Panel(Group(*lines), title="[bold]Startup[/bold]", border_style="bright_blue")

    # ── Public API for updating state ──────────────────────────────────────

    def show_checklist(self, items: list[str]) -> None:
        """Show startup checklist with all items unchecked."""
        with self._lock:
            self._checklist = [(item, False) for item in items]
            self._showing_checklist = True
            self._refresh()

    def check_item(self, index: int) -> None:
        """Mark a checklist item as done."""
        with self._lock:
            if 0 <= index < len(self._checklist):
                label, _ = self._checklist[index]
                self._checklist[index] = (label, True)
            self._refresh()

    def update_checklist_label(self, index: int, new_label: str) -> None:
        """Update a checklist item's label."""
        with self._lock:
            if 0 <= index < len(self._checklist):
                _, done = self._checklist[index]
                self._checklist[index] = (new_label, done)
            self._refresh()

    def hide_checklist(self) -> None:
        """Hide the checklist and show the main dashboard."""
        with self._lock:
            self._showing_checklist = False
            self._refresh()

    def set_phase(self, phase: str) -> None:
        """Set the current scraping phase."""
        with self._lock:
            self.phase = phase
            self._refresh()

    def set_query(self, query: str) -> None:
        """Set the query string."""
        with self._lock:
            self.query = query
            self._refresh()

    def set_key_info(self, index: int, total: int, model: str) -> None:
        """Update API key display info."""
        with self._lock:
            self.active_key_index = index
            self.total_keys = total
            self.model_name = model
            self._refresh()

    def set_total_leads(self, total: int) -> None:
        """Set total leads found."""
        with self._lock:
            self.total_leads = total
            self._progress.update(self._details_task, total=total)
            self._progress.update(self._email_task, total=total)
            self._refresh()

    def increment_processed(self) -> None:
        """Increment the processed leads counter."""
        with self._lock:
            self.processed_leads += 1
            self._refresh()

    def increment_emails(self) -> None:
        """Increment the emails found counter."""
        with self._lock:
            self.emails_found += 1
            self._refresh()

    def update_scroll_progress(self, completed: int, total: int = 50) -> None:
        """Update the maps scroll progress bar."""
        with self._lock:
            self._progress.update(self._scroll_task, completed=completed, total=total)
            if completed > 0:
                elapsed = time.time() - self.start_time
                speed = completed / (elapsed / 60) if elapsed > 0 else 0
                self._progress.update(self._scroll_task, speed=f"{speed:.1f}/min")
            self._refresh()

    def update_details_progress(self, completed: int, total: int | None = None) -> None:
        """Update the details fetching progress bar."""
        with self._lock:
            kwargs: dict[str, Any] = {"completed": completed}
            if total is not None:
                kwargs["total"] = total
            elapsed = time.time() - self.start_time
            if completed > 0 and elapsed > 0:
                speed = completed / (elapsed / 60)
                kwargs["speed"] = f"{speed:.1f}/min"
            self._progress.update(self._details_task, **kwargs)
            self._refresh()

    def update_email_progress(self, completed: int, total: int | None = None) -> None:
        """Update the email extraction progress bar."""
        with self._lock:
            kwargs: dict[str, Any] = {"completed": completed}
            if total is not None:
                kwargs["total"] = total
            elapsed = time.time() - self.start_time
            if completed > 0 and elapsed > 0:
                speed = completed / (elapsed / 60)
                kwargs["speed"] = f"{speed:.1f}/min"
            self._progress.update(self._email_task, **kwargs)
            self._refresh()

    def update_lead(
        self,
        name: str = "",
        category: str = "",
        rating: str = "",
        phone: str = "",
        website: str = "",
        email: str = "",
        status: str = "",
    ) -> None:
        """Update the current lead card."""
        with self._lock:
            lc = self.lead_card
            if name:
                lc.name = name
            if category:
                lc.category = category
            if rating:
                lc.rating = rating
            if phone:
                lc.phone = phone
            if website:
                lc.website = website
            if email:
                lc.email = email
            if status:
                lc.status = status
            self._refresh()

    def clear_lead(self) -> None:
        """Clear the current lead card."""
        with self._lock:
            self.lead_card = LeadCard()
            self._refresh()

    def log(self, message: str, level: str = "INFO") -> None:
        """Add a log line to the scrolling log panel."""
        ts = time.strftime("%H:%M:%S")

        level_styles = {
            "INFO": "white",
            "SUCCESS": "bold bright_green",
            "SKIP": "dim yellow",
            "WARNING": "yellow",
            "ERROR": "bold red",
            "API_SWITCH": "bold bright_cyan",
            "RETRY": "magenta",
        }
        style = level_styles.get(level, "white")

        icons = {
            "SUCCESS": "✉ ",
            "API_SWITCH": "⚡ ",
            "ERROR": "✖ ",
            "WARNING": "⚠ ",
            "SKIP": "⏭ ",
            "RETRY": "↻ ",
        }
        icon = icons.get(level, "")

        line = Text()
        line.append(f"[{ts}] ", style="dim cyan")
        line.append(f"{icon}{message}", style=style)

        with self._lock:
            self._log_lines.append(line)
            self._refresh()
