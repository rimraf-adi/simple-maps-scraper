"""
Checkpoint system — crash-safe session save/load/update for resumable runs.

Saves progress to {query_slug}_checkpoint.json so a run can be resumed
with --resume if it crashes or is interrupted mid-run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("maps_scraper.checkpoint")


def _checkpoint_path(query_slug: str) -> Path:
    """Return the checkpoint file path for a given query slug."""
    return Path(f"{query_slug}_checkpoint.json")


def save_checkpoint(
    query_slug: str,
    query: str,
    leads: list[dict[str, Any]],
    started_at: str | None = None,
) -> None:
    """
    Save a full checkpoint file with all leads.
    Called after phase 1 (Maps scrape) completes.
    """
    path = _checkpoint_path(query_slug)
    data = {
        "query": query,
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "resumed_at": None,
        "leads": leads,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    log.info("Checkpoint saved: %s (%d leads)", path.name, len(leads))


def update_checkpoint(
    query_slug: str,
    lead_index: int,
    lead_data: dict[str, Any],
) -> None:
    """
    Update a single lead in the checkpoint and mark it as done.
    Called after each lead's details + email are resolved.
    """
    path = _checkpoint_path(query_slug)
    if not path.exists():
        return

    try:
        data = json.loads(path.read_text())
        if 0 <= lead_index < len(data["leads"]):
            data["leads"][lead_index].update(lead_data)
            data["leads"][lead_index]["done"] = True
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    except Exception as e:
        log.warning("Failed to update checkpoint: %s", e)


def load_checkpoint(query_slug: str) -> dict[str, Any] | None:
    """
    Load a checkpoint file for resuming.
    Returns the checkpoint data dict, or None if no checkpoint exists.
    """
    path = _checkpoint_path(query_slug)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
        # Mark as resumed
        data["resumed_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        done_count = sum(1 for lead in data.get("leads", []) if lead.get("done"))
        total = len(data.get("leads", []))
        log.info("Checkpoint loaded: %d/%d leads already done", done_count, total)
        return data
    except Exception as e:
        log.warning("Failed to load checkpoint: %s", e)
        return None


def count_done(checkpoint_data: dict[str, Any]) -> int:
    """Count how many leads are marked as done in a checkpoint."""
    return sum(1 for lead in checkpoint_data.get("leads", []) if lead.get("done"))


def count_remaining(checkpoint_data: dict[str, Any]) -> int:
    """Count how many leads are not yet done."""
    return sum(1 for lead in checkpoint_data.get("leads", []) if not lead.get("done"))
