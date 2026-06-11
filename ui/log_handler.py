"""
Thread-safe log handler that routes Python logging into the Rich dashboard.

Intercepts all standard logging calls and forwards them to the Dashboard's
log panel so that Playwright, asyncio, and library output don't break
the Rich live display.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ui.dashboard import Dashboard


# Mapping from standard logging levels to Dashboard log levels
_LEVEL_MAP = {
    logging.DEBUG: "INFO",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "ERROR",
}


class DashboardLogHandler(logging.Handler):
    """
    Custom logging handler that routes log records into the Rich Dashboard.

    Usage:
        dashboard = Dashboard()
        handler = DashboardLogHandler(dashboard)
        logging.root.addHandler(handler)
    """

    def __init__(self, dashboard: "Dashboard") -> None:
        super().__init__()
        self._dashboard = dashboard

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            level = _LEVEL_MAP.get(record.levelno, "INFO")

            # Check for custom level markers in the message
            if hasattr(record, "dashboard_level"):
                level = record.dashboard_level
            elif "✉" in msg or "email found" in msg.lower():
                level = "SUCCESS"
            elif "⚡" in msg or "API key rotated" in msg:
                level = "API_SWITCH"
            elif "retry" in msg.lower() or "retrying" in msg.lower():
                level = "RETRY"
            elif "skip" in msg.lower():
                level = "SKIP"

            self._dashboard.log(msg, level=level)
        except Exception:
            # Never let logging errors crash the app
            pass


def setup_logging(dashboard: "Dashboard") -> None:
    """
    Configure all logging to route through the dashboard.
    Suppresses direct stdout/stderr output from loggers.
    """
    handler = DashboardLogHandler(dashboard)
    handler.setFormatter(logging.Formatter("%(message)s"))

    # Set up root logger
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.WARNING)

    # Set up our app logger at INFO level
    app_log = logging.getLogger("maps_scraper")
    app_log.handlers.clear()
    app_log.addHandler(handler)
    app_log.setLevel(logging.INFO)
    app_log.propagate = False

    # Suppress noisy loggers
    for name in ("urllib3", "httpx", "httpcore", "openai", "playwright", "asyncio"):
        noisy = logging.getLogger(name)
        noisy.setLevel(logging.WARNING)
        noisy.handlers.clear()
        noisy.addHandler(handler)
        noisy.propagate = False
