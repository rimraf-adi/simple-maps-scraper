"""
Maps Scraper — Desktop application entry point.

Launches the CustomTkinter GUI.
"""
from __future__ import annotations

def main():
    from ui.gui import ScraperApp
    app = ScraperApp()
    app.mainloop()

if __name__ == "__main__":
    main()
