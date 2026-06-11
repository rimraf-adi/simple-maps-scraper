"""Desktop GUI for Maps Scraper using CustomTkinter."""
from __future__ import annotations
import asyncio, os, queue, threading, time, logging
from dataclasses import dataclass
from dotenv import load_dotenv
import customtkinter as ctk

log = logging.getLogger("maps_scraper")

# ── Thread-safe dashboard adapter ──────────────────────────────────────────
class GUIDashboard:
    def __init__(self, q: queue.Queue):
        self._q = q
        self.query = ""
        self.phase = ""
        self.total_leads = 0
        self.processed_leads = 0
        self.emails_found = 0
        self.start_time = time.time()
        self.resumed_count = 0
    def _put(self, t, **d): self._q.put((t, d))
    def log(self, msg, level="INFO"): self._put("log", msg=msg, level=level)
    def set_phase(self, p):
        self.phase = p; self._put("phase", phase=p)
    def set_query(self, q): self.query = q
    def set_total_leads(self, n):
        self.total_leads = n; self._put("total", n=n)
    def increment_processed(self):
        self.processed_leads += 1; self._put("processed", n=self.processed_leads)
    def increment_emails(self):
        self.emails_found += 1; self._put("emails", n=self.emails_found)
    def set_key_info(self, index, total, model):
        self._put("key", index=index, total=total, model=model)
    def update_lead(self, **kw): self._put("lead", **kw)
    def clear_lead(self): self._put("lead_clear")
    def update_scroll_progress(self, completed, total=50):
        self._put("scroll", completed=completed, total=total)
    def update_details_progress(self, completed, total=None):
        self._put("details_prog", completed=completed, total=total)
    def update_email_progress(self, completed, total=None):
        self._put("email_prog", completed=completed, total=total)
    # checklist stubs (startup steps just go to log)
    def show_checklist(self, items): pass
    def check_item(self, i): pass
    def update_checklist_label(self, i, lbl): pass
    def hide_checklist(self): pass
    def start(self): pass
    def stop(self): pass

# ── Color constants ────────────────────────────────────────────────────────
BG = "#eceeef"      # Greyish off-white background
CARD = "#f4f5f6"    # Lighter grey for cards
ACCENT = "#2f855a"  # Clean green accent to match the grey theme
GREEN = "#2f855a"   # Muted green
RED = "#c53030"     # Muted red
YELLOW = "#d69e2e"  # Muted yellow
CYAN = "#2b6cb0"    # Muted blue for logs
DIM = "#718096"
TEXT = "#1a202c"

LOG_COLORS = {
    "INFO": TEXT, "SUCCESS": GREEN, "SKIP": YELLOW,
    "WARNING": YELLOW, "ERROR": RED, "API_SWITCH": CYAN, "RETRY": "#9333ea",
}
LOG_ICONS = {
    "SUCCESS": "✉ ", "ERROR": "✖ ", "WARNING": "⚠ ",
    "SKIP": "⏭ ", "API_SWITCH": "⚡ ", "RETRY": "↻ ",
}

class ToolTip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tw = None
        self.widget.bind("<Enter>", self.enter)
        self.widget.bind("<Leave>", self.leave)

    def enter(self, event=None):
        x = self.widget.winfo_rootx() + 20
        y = self.widget.winfo_rooty() + 20
        import tkinter as tk
        self.tw = tk.Toplevel(self.widget)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry(f"+{x}+{y}")
        self.tw.attributes("-topmost", True)
        label = tk.Label(self.tw, text=self.text, justify='left',
                         background="#1a202c", foreground="#f8fafc", 
                         relief='solid', borderwidth=0,
                         font=("Segoe UI", 10), padx=8, pady=4)
        label.pack()

    def leave(self, event=None):
        if self.tw:
            self.tw.destroy()
            self.tw = None

class ScraperApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("light")
        self.title("Maps Lead Scraper")
        self.geometry("1050x720")
        self.minsize(900, 650)
        self.configure(fg_color=BG)
        self._q: queue.Queue = queue.Queue()
        self._running = False
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._build()
        self._poll()

    # ── Build GUI ──────────────────────────────────────────────────────────
    def _build(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # Title
        hdr = ctk.CTkFrame(self, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=20, pady=(15,5))
        ctk.CTkLabel(hdr, text="🗺️  Maps Lead Scraper", font=("Segoe UI", 22, "bold"),
                     text_color=TEXT).pack(side="left")

        # Search bar
        sf = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        sf.grid(row=1, column=0, sticky="ew", padx=20, pady=8)
        sf.grid_columnconfigure(0, weight=1)
        self._search = ctk.CTkEntry(sf, placeholder_text="Enter search query, e.g. 'Plumbers in Chicago'",
                                    height=42, font=("Segoe UI", 14), corner_radius=8)
        self._search.grid(row=0, column=0, padx=(12,6), pady=12, sticky="ew")
        self._search.bind("<Return>", lambda e: self._on_start())
        self._start_btn = ctk.CTkButton(sf, text="▶  Start", width=110, height=42,
                                        font=("Segoe UI", 14, "bold"), corner_radius=8,
                                        fg_color="#22c55e", hover_color="#16a34a", text_color="#ffffff",
                                        command=self._on_start)
        self._start_btn.grid(row=0, column=1, padx=(0,6), pady=12)
        self._resume_btn = ctk.CTkButton(sf, text="↻  Resume", width=110, height=42,
                                         font=("Segoe UI", 14, "bold"), corner_radius=8,
                                         fg_color="#3b82f6", hover_color="#2563eb", text_color="#ffffff",
                                         command=lambda: self._on_start(resume=True))
        self._resume_btn.grid(row=0, column=2, padx=(0,6), pady=12)
        self._stop_btn = ctk.CTkButton(sf, text="■  Stop", width=90, height=42,
                                       font=("Segoe UI", 14, "bold"), corner_radius=8,
                                       fg_color="#ef4444", hover_color="#dc2626", text_color="#ffffff", state="disabled",
                                       command=self._on_stop)
        self._stop_btn.grid(row=0, column=3, padx=(0,12), pady=12)

        # Options
        of = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        of.grid(row=2, column=0, sticky="ew", padx=20, pady=4)
        # Row 1
        r1 = ctk.CTkFrame(of, fg_color="transparent")
        r1.pack(fill="x", padx=12, pady=(10,2))
        ctk.CTkLabel(r1, text="Max Leads:", font=("Segoe UI", 12), text_color=DIM).pack(side="left")
        self._max_res = ctk.CTkEntry(r1, width=55, height=30, placeholder_text="∞")
        self._max_res.pack(side="left", padx=(4,14))
        ctk.CTkLabel(r1, text="Min Rating:", font=("Segoe UI", 12), text_color=DIM).pack(side="left")
        self._min_rat = ctk.CTkEntry(r1, width=55, height=30, placeholder_text="0")
        self._min_rat.pack(side="left", padx=(4,14))
        ctk.CTkLabel(r1, text="Format:", font=("Segoe UI", 12), text_color=DIM).pack(side="left")
        self._fmt = ctk.CTkOptionMenu(r1, values=["csv","json","both"], width=80, height=30)
        self._fmt.pack(side="left", padx=(4,14))
        ctk.CTkLabel(r1, text="Workers:", font=("Segoe UI", 12), text_color=DIM).pack(side="left")
        self._workers = ctk.CTkOptionMenu(r1, values=[str(i) for i in range(1, 11)], width=55, height=30)
        self._workers.pack(side="left", padx=(4,0))
        # Row 2 (Settings Grid)
        r2 = ctk.CTkFrame(of, fg_color="transparent")
        r2.pack(fill="x", padx=12, pady=(4,8))
        
        # Configure columns to be equal weight for perfect symmetry
        for i in range(4):
            r2.grid_columnconfigure(i, weight=1)
        
        def make_checkbox_with_tooltip(parent, text, tooltip_text, r, c):
            frame = ctk.CTkFrame(parent, fg_color="transparent")
            cb = ctk.CTkCheckBox(frame, text=text, font=("Segoe UI", 12), text_color=TEXT, checkbox_width=20, checkbox_height=20, hover_color=ACCENT, fg_color=ACCENT)
            cb.select() # Turn ON by default
            cb.pack(side="left")
            info = ctk.CTkLabel(frame, text="ℹ️", font=("Segoe UI", 12), text_color=DIM, cursor="hand2")
            info.pack(side="left", padx=(4,0))
            ToolTip(info, tooltip_text)
            frame.grid(row=r, column=c, sticky="w", pady=6)
            return cb

        self._headless = make_checkbox_with_tooltip(r2, "Headless", "Run browser in background without showing the UI.", 0, 0)
        self._stealth_cb = make_checkbox_with_tooltip(r2, "Stealth", "Use anti-bot tactics to prevent blocks from Cloudflare/Google.", 0, 1)
        self._req_email = make_checkbox_with_tooltip(r2, "Require Email", "Only save leads that have a successfully extracted email.", 0, 2)
        self._req_phone = make_checkbox_with_tooltip(r2, "Require Phone", "Only save leads that have a phone number.", 0, 3)
        
        self._excl_chain = make_checkbox_with_tooltip(r2, "Exclude Chains", "Skip large global corporations (McDonald's, Starbucks, etc).", 1, 0)
        self._dedupe_cb = make_checkbox_with_tooltip(r2, "Deduplicate", "Filter out previously scraped duplicate leads.", 1, 1)
        self._auto_expand = make_checkbox_with_tooltip(r2, "Auto-Expand Query", "Bypass 120-lead Maps limit by letting AI generate sub-regions.", 1, 2)
        self._verify_emails = make_checkbox_with_tooltip(r2, "Verify Emails", "Test if emails are active without sending a real message.", 1, 3)

        # Row 3 (Output Path)
        r3 = ctk.CTkFrame(of, fg_color="transparent")
        r3.pack(fill="x", padx=12, pady=(2,10))
        ctk.CTkLabel(r3, text="Save As:", font=("Segoe UI", 12), text_color=DIM).pack(side="left")
        self._output_path = ctk.CTkEntry(r3, height=30, placeholder_text="Default: /home/vengeance/Documents/Leads_Scraper/<query>.csv")
        self._output_path.pack(side="left", fill="x", expand=True, padx=(8, 8))
        self._browse_btn = ctk.CTkButton(r3, text="Browse...", width=80, height=30, font=("Segoe UI", 12),
                                         command=self._on_browse)
        self._browse_btn.pack(side="left")

        # Progress
        pf = ctk.CTkFrame(self, fg_color=CARD, corner_radius=12)
        pf.grid(row=3, column=0, sticky="ew", padx=20, pady=4)
        pr1 = ctk.CTkFrame(pf, fg_color="transparent")
        pr1.pack(fill="x", padx=12, pady=(10,4))
        self._phase_lbl = ctk.CTkLabel(pr1, text="Phase: IDLE", font=("Segoe UI", 13, "bold"),
                                       text_color=ACCENT)
        self._phase_lbl.pack(side="left")
        self._elapsed_lbl = ctk.CTkLabel(pr1, text="Elapsed: 00:00:00", font=("Segoe UI", 12),
                                         text_color=DIM)
        self._elapsed_lbl.pack(side="right")
        self._pbar = ctk.CTkProgressBar(pf, height=14, corner_radius=7,
                                        progress_color=ACCENT, fg_color="#e2e8f0")
        self._pbar.pack(fill="x", padx=12, pady=4)
        self._pbar.set(0)
        self._stats_lbl = ctk.CTkLabel(pf, text="Total: 0  |  Processed: 0  |  Emails: 0  |  Key: –",
                                       font=("Segoe UI", 12), text_color=DIM)
        self._stats_lbl.pack(padx=12, pady=(0,10), anchor="w")

        # Bottom area: lead card + log
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=4, column=0, sticky="nsew", padx=20, pady=(4,15))
        bottom.grid_columnconfigure(1, weight=1)
        bottom.grid_rowconfigure(0, weight=1)

        # Lead card
        lf = ctk.CTkFrame(bottom, fg_color=CARD, corner_radius=12, width=260)
        lf.grid(row=0, column=0, sticky="ns", padx=(0,8))
        lf.grid_propagate(False)
        ctk.CTkLabel(lf, text="Current Lead", font=("Segoe UI", 13, "bold"),
                     text_color=ACCENT).pack(padx=12, pady=(12,8), anchor="w")
        self._lead_labels = {}
        for key, icon in [("name",""), ("rating","★"), ("category",""), ("phone","📞"),
                          ("website","🌐"), ("email","✉"), ("status","●")]:
            row = ctk.CTkFrame(lf, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=2)
            lbl_text = f"{icon} {key.title()}:" if icon else f"{key.title()}:"
            ctk.CTkLabel(row, text=lbl_text, font=("Segoe UI", 11), text_color=DIM, width=75,
                         anchor="w").pack(side="left")
            val = ctk.CTkLabel(row, text="—", font=("Segoe UI", 11), text_color=TEXT, anchor="w")
            val.pack(side="left", fill="x", expand=True)
            self._lead_labels[key] = val

        # Log panel
        logf = ctk.CTkFrame(bottom, fg_color=CARD, corner_radius=12)
        logf.grid(row=0, column=1, sticky="nsew")
        ctk.CTkLabel(logf, text="Activity Log", font=("Segoe UI", 13, "bold"),
                     text_color=ACCENT).pack(padx=12, pady=(12,4), anchor="w")
        self._log_box = ctk.CTkTextbox(logf, font=("Consolas", 11), fg_color="#f1f5f9",
                                       corner_radius=8, wrap="word", state="disabled",
                                       text_color=TEXT)
        self._log_box.pack(fill="both", expand=True, padx=12, pady=(0,12))
        # Configure log tag colors
        for level, color in LOG_COLORS.items():
            self._log_box._textbox.tag_configure(level, foreground=color)
        self._log_box._textbox.tag_configure("TS", foreground=DIM)

        # Status bar - used as "Current Process" indicator
        sf2 = ctk.CTkFrame(self, fg_color=CARD, corner_radius=8)
        sf2.grid(row=5, column=0, sticky="ew", padx=20, pady=(0,10))
        ctk.CTkLabel(sf2, text="Current Process:", font=("Segoe UI", 12, "bold"), text_color=DIM,
                     anchor="w").pack(side="left", padx=(12, 6), pady=8)
        self._status = ctk.CTkLabel(sf2, text="Ready", font=("Segoe UI", 12, "bold"), text_color=ACCENT,
                                    anchor="w")
        self._status.pack(side="left", fill="x", expand=True, padx=(0, 12), pady=8)
    def _on_browse(self):
        from customtkinter import filedialog
        fmt = self._fmt.get()
        exts = [("CSV File", "*.csv"), ("JSON File", "*.json"), ("All Files", "*.*")] if fmt != "both" else [("All Files", "*.*")]
        
        path = filedialog.asksaveasfilename(
            defaultextension=".csv" if fmt == "csv" else ".json",
            filetypes=exts,
            title="Save Leads As"
        )
        if path:
            self._output_path.delete(0, "end")
            self._output_path.insert(0, path)

    # ── Polling / thread-safe updates ──────────────────────────────────────
    def _poll(self):
        try:
            while True:
                t, d = self._q.get_nowait()
                self._apply(t, d)
        except queue.Empty:
            pass
        if self._running:
            self._update_elapsed()
        self.after(100, self._poll)

    def _apply(self, t, d):
        if t == "log":
            msg = d["msg"]
            self._append_log(msg, d.get("level", "INFO"))
            # Update Current Process for INFO level logs that describe actions
            if d.get("level", "INFO") in ["INFO", "SUCCESS", "API_SWITCH", "RETRY"]:
                self._status.configure(text=str(msg)[:100])
        elif t == "phase":
            self._phase_lbl.configure(text=f"Phase: {d['phase']}")
            self._status.configure(text=f"Starting: {d['phase']}")
        elif t == "total":
            self._update_stats(total=d["n"])
        elif t == "processed":
            self._update_stats(processed=d["n"])
        elif t == "emails":
            self._update_stats(emails=d["n"])
        elif t == "key":
            self._update_stats(key_str=f"{d['index']}/{d['total']}")
        elif t == "lead":
            for k, v in d.items():
                if k in self._lead_labels and v is not None:
                    color = GREEN if k == "email" and v else (
                        self._status_color(v) if k == "status" else TEXT)
                    self._lead_labels[k].configure(text=str(v)[:40] if v else "—", text_color=color)
        elif t == "lead_clear":
            for lbl in self._lead_labels.values():
                lbl.configure(text="—", text_color=TEXT)
        elif t == "scroll":
            pct = d["completed"] / d["total"] if d["total"] else 0
            self._pbar.set(min(pct, 1.0))
        elif t == "done":
            self._on_done(d.get("report", ""))
        elif t == "error":
            self._append_log(d["msg"], "ERROR")
            self._status.configure(text="Error occurred.")
            self._on_done("")

    def _status_color(self, s):
        return {"FOUND": GREEN, "FAILED": RED, "SKIPPED": YELLOW,
                "NAVIGATING": CYAN, "SCRAPING": ACCENT}.get(s, TEXT)

    def _append_log(self, msg, level="INFO"):
        ts = time.strftime("%H:%M:%S")
        icon = LOG_ICONS.get(level, "▸ ")
        self._log_box.configure(state="normal")
        tb = self._log_box._textbox
        tb.insert("end", f"[{ts}] ", "TS")
        tb.insert("end", f"{icon}{msg}\n", level)
        tb.see("end")
        self._log_box.configure(state="disabled")

    _total = 0; _processed = 0; _emails = 0; _key_str = "–"
    def _update_stats(self, total=None, processed=None, emails=None, key_str=None):
        if total is not None: self._total = total
        if processed is not None: self._processed = processed
        if emails is not None: self._emails = emails
        if key_str is not None: self._key_str = key_str
        self._stats_lbl.configure(
            text=f"Total: {self._total}  |  Processed: {self._processed}  |  "
                 f"Emails: {self._emails}  |  Key: {self._key_str}")
        if self._total > 0:
            self._pbar.set(self._processed / self._total)

    def _update_elapsed(self):
        if not hasattr(self, "_start_ts"): return
        e = int(time.time() - self._start_ts)
        h, r = divmod(e, 3600); m, s = divmod(r, 60)
        self._elapsed_lbl.configure(text=f"Elapsed: {h:02d}:{m:02d}:{s:02d}")

    # ── Start / Stop ───────────────────────────────────────────────────────
    def _on_start(self, resume=False):
        q = self._search.get().strip()
        if not q:
            self._append_log("Please enter a search query", "WARNING")
            return
        if self._running:
            return
        self._running = True
        self._cancel.clear()
        self._start_ts = time.time()
        self._total = self._processed = self._emails = 0
        self._pbar.set(0)
        self._start_btn.configure(state="disabled")
        self._resume_btn.configure(state="disabled")
        self._stop_btn.configure(state="normal")
        self._search.configure(state="disabled")
        self._status.configure(text=f"{'Resuming' if resume else 'Scraping'}: {q}")
        self._append_log(f"{'Resuming' if resume else 'Starting'} scrape: {q}", "INFO")
        self._thread = threading.Thread(target=self._run_scraper, args=(q, resume), daemon=True)
        self._thread.start()

    def _on_stop(self):
        if self._running:
            self._cancel.set()
            self._append_log("Stopping… (finishing current lead)", "WARNING")
            self._stop_btn.configure(state="disabled")

    def _on_done(self, report=""):
        self._running = False
        self._start_btn.configure(state="normal")
        self._resume_btn.configure(state="normal")
        self._stop_btn.configure(state="disabled")
        self._search.configure(state="normal")
        self._phase_lbl.configure(text="Phase: DONE")
        self._status.configure(text="Ready")
        if report:
            self._append_log(report, "INFO")

    # ── Scraper thread ─────────────────────────────────────────────────────
    def _run_scraper(self, query: str, resume: bool = False):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._scrape(query, resume))
        except Exception as e:
            self._q.put(("error", {"msg": f"Fatal: {e}"}))
        finally:
            loop.close()

    async def _scrape(self, query: str, resume: bool = False):
        load_dotenv()
        from browser.manager import BrowserManager
        from llm.key_pool import KeyPool
        from maps_scraper import extract_leads, fetch_place_details
        from extract_emails import extract_from_site
        from output import OutputWriter, validate_email, generate_report
        from checkpoint import save_checkpoint, update_checkpoint, load_checkpoint

        dash = GUIDashboard(self._q)
        import re
        slug = re.sub(r'[^a-z0-9]', '_', query.lower())
        slug = re.sub(r'_+', '_', slug).strip('_')

        # Parse GUI options
        max_res = int(self._max_res.get() or 0)
        min_rat = float(self._min_rat.get() or 0)
        fmt = self._fmt.get()
        workers = int(self._workers.get())
        headless = bool(self._headless.get())
        stealth = bool(self._stealth_cb.get())
        req_email = bool(self._req_email.get())
        req_phone = bool(self._req_phone.get())
        excl_chain = bool(self._excl_chain.get())
        dedupe = bool(self._dedupe_cb.get())
        auto_expand = bool(self._auto_expand.get())
        verify = bool(self._verify_emails.get())
        custom_out = self._output_path.get().strip()

        # Key pool
        dash.log("Loading API keys…")
        try:
            pool = KeyPool.from_env()
            dash.set_key_info(pool.current_index + 1, pool.total_keys, pool.current_model())
            dash.log(f"Found {pool.total_keys} API key(s), model: {pool.current_model()}", "INFO")
        except Exception as e:
            dash.log(f"KeyPool error: {e}", "ERROR")
            self._q.put(("done", {}))
            return

        # Test LLM
        dash.log("Testing LLM connectivity…")
        try:
            from openai import AsyncOpenAI
            c = AsyncOpenAI(base_url=pool.current_base_url(), api_key=pool.current_key())
            await c.models.list(timeout=10)
            dash.log(f"LLM connected: {pool.current_base_url()}", "SUCCESS")
        except Exception as e:
            dash.log(f"LLM unavailable: {e}", "ERROR")
            self._q.put(("done", {}))
            return

        # Launch browser
        dash.log(f"Launching browser (headless={headless}, stealth={stealth})…")
        browser = BrowserManager(
            headless=headless, stealth=stealth,
            locale=os.getenv("BROWSER_LOCALE", "en-US"),
            timezone=os.getenv("BROWSER_TIMEZONE", "America/New_York"),
        )
        await browser.start()
        dash.log("Browser ready", "SUCCESS")

        try:
            leads = []
            seen_urls = set()

            if resume:
                ckpt = load_checkpoint(slug)
                if ckpt and "leads" in ckpt:
                    leads = ckpt["leads"]
                    dash.log(f"Resumed from checkpoint: {len(leads)} leads loaded.", "SUCCESS")
                    for l in leads:
                        seen_urls.add(l.get("URL", ""))
                    dash.set_total_leads(len(leads))
                else:
                    dash.log("No checkpoint found. Starting fresh.", "WARNING")
                    resume = False

            if not resume:
                # Sub-queries logic
                sub_queries = [query]
                if auto_expand:
                    dash.set_phase("EXPANDING QUERY")
                    dash.log("Asking LLM to expand query into sub-regions...")
                    try:
                        from openai import AsyncOpenAI
                        import json
                        prompt = f"The user wants to search Google Maps for '{query}'. To bypass the 120-result limit, provide a JSON list of up to 10 highly specific local search queries for this target area (by neighborhood, zip code, or sub-region). Return strictly a JSON list of strings. Do not use markdown blocks."
                        
                        raw = ""
                        for attempt in range(100):
                            try:
                                c = AsyncOpenAI(base_url=pool.current_base_url(), api_key=pool.current_key())
                                resp = await c.chat.completions.create(
                                    model=pool.current_model(),
                                    messages=[{"role": "user", "content": prompt}],
                                    temperature=0.3,
                                )
                                raw = resp.choices[0].message.content.strip()
                                pool.record_success()
                                pool.reset_backoff()
                                break
                            except Exception as e:
                                if pool.is_auth_error(e):
                                    pool.mark_permanently_bad(str(e)[:100])
                                    pool.record_failure(str(e)[:200])
                                    if not pool.rotate(str(e)[:100]):
                                        dash.log("🚨 ALL API KEYS ARE BROKEN OR INVALID! Please close the program, fix your .env file, and restart.", "ERROR")
                                        break
                                elif pool.is_rotatable_error(e):
                                    pool.mark_rate_limited(str(e)[:100])
                                    pool.record_failure(str(e)[:200])
                                    if not pool.rotate(str(e)[:100]):
                                        delay = pool.get_backoff_delay()
                                        if delay:
                                            dash.log(f"⏳ API limits reached! Pausing for {int(delay)} seconds... (Do not close program)", "WARNING")
                                            await asyncio.sleep(delay)
                                            pool.reset_all()
                                        else:
                                            dash.log("❌ All API keys maxed out. Max retries reached. Please close and try again later.", "ERROR")
                                            raise Exception("Total key exhaustion")
                                else:
                                    raise e
                        if raw.startswith("```json"): raw = raw[7:]
                        if raw.endswith("```"): raw = raw[:-3]
                        sub_queries = json.loads(raw)
                        dash.log(f"LLM generated {len(sub_queries)} sub-queries!", "SUCCESS")
                        for sq in sub_queries:
                            dash.log(f"  - {sq}", "INFO")
                    except Exception as e:
                        dash.log(f"Query expansion failed: {e}. Proceeding with original.", "WARNING")

                dash.set_phase("MAPS SEARCH")
                
                for i, sq in enumerate(sub_queries):
                    if self._cancel.is_set():
                        break
                    dash.log(f"Searching [{i+1}/{len(sub_queries)}]: {sq}")
                    
                    remain = max_res - len(leads) if max_res > 0 else 0
                    if max_res > 0 and remain <= 0:
                        break
                        
                    sq_leads = await extract_leads(
                        browser, sq, dashboard=dash,
                        max_results=remain, min_rating=min_rat,
                        exclude_chains=excl_chain,
                    )
                    
                    for l in sq_leads:
                        url = l["URL"]
                        if url not in seen_urls:
                            seen_urls.add(url)
                            leads.append(l)
                            
                    dash.set_total_leads(len(leads))
                    if max_res > 0 and len(leads) >= max_res:
                        dash.log(f"Hit max target of {max_res} leads.", "SUCCESS")
                        break

                dash.log(f"Found {len(leads)} total unique leads", "SUCCESS")

                if not leads:
                    dash.log("No leads found", "WARNING")
                    self._q.put(("done", {}))
                    return

                save_checkpoint(slug, query, leads)

            # Output writer logic
            if custom_out:
                if fmt == "csv":
                    csv_path = custom_out
                    json_path = None
                elif fmt == "json":
                    csv_path = None
                    json_path = custom_out
                else:
                    base = custom_out.rsplit('.', 1)[0]
                    csv_path = f"{base}.csv"
                    json_path = f"{base}.json"
                dash.log(f"Saving output to: {custom_out}", "INFO")
            else:
                csv_path = f"{slug}.csv" if fmt in ("csv", "both") else None
                json_path = f"{slug}.json" if fmt in ("json", "both") else None
                dash.log(f"Saving output to: {slug}.*", "INFO")
                
            writer = OutputWriter(csv_path=csv_path, json_path=json_path, dedupe=dedupe, append=resume)
            writer.open()

            # Phase 2 & 3: Details + Emails Process function
            async def process_lead(index: int, lead: dict, worker_browser: BrowserManager):
                if self._cancel.is_set():
                    return

                if lead.get("done"):
                    dash.increment_processed()
                    if lead.get("Email"):
                        dash.increment_emails()
                    return

                name = lead.get("Name", "?")
                dash.set_phase("FETCHING DETAILS")
                dash.log(f"[{index+1}/{len(leads)}] {name}")
                dash.update_lead(name=name, category=lead.get("Category",""),
                                 rating=lead.get("Rating",""), status="SCRAPING",
                                 phone="", website="", email="")

                try:
                    phone, website, address = await fetch_place_details(worker_browser, lead["URL"])
                except Exception as e:
                    dash.log(f"Detail fetch failed: {e}", "ERROR")
                    phone = website = address = ""

                lead["Phone"] = phone
                lead["Website"] = website
                lead["Address"] = address
                dash.update_lead(phone=phone, website=website)

                email = ""
                if website:
                    dash.set_phase("EXTRACTING EMAILS")
                    dash.update_lead(status="NAVIGATING")
                    try:
                        email = await extract_from_site(
                            worker_browser, name, website, key_pool=pool, dashboard=dash)
                    except Exception as e:
                        dash.log(f"Email extraction failed: {e}", "ERROR")

                    if email:
                        if validate_email(email):
                            if verify:
                                dash.log(f"Verifying {email}...", "INFO")
                                from email_verifier import verify_email as verify_email_addr
                                is_valid = await verify_email_addr(email)
                                if is_valid:
                                    dash.log(f"Verified & Found: {email}", "SUCCESS")
                                    dash.increment_emails()
                                    dash.update_lead(email=email, status="FOUND")
                                else:
                                    dash.log(f"Email failed verification: {email}", "WARNING")
                                    email = ""
                                    dash.update_lead(status="INVALID")
                            else:
                                dash.log(f"Found email: {email}", "SUCCESS")
                                dash.increment_emails()
                                dash.update_lead(email=email, status="FOUND")
                        else:
                            dash.log(f"Rejected invalid: {email}", "SKIP")
                            email = ""
                            dash.update_lead(status="SKIPPED")
                    else:
                        dash.update_lead(status="SKIPPED")
                else:
                    dash.log(f"No website for {name}", "SKIP")
                    dash.update_lead(status="SKIPPED")

                lead["Email"] = email or ""

                skip = False
                if req_email and not email: skip = True
                if req_phone and not phone: skip = True
                if not skip:
                    writer.write_row(lead)

                update_checkpoint(slug, index, lead)
                dash.increment_processed()
                dash.set_key_info(pool.current_index + 1, pool.total_keys, pool.current_model())

            # Worker Parallelization Logic
            if workers == 1:
                for i, lead in enumerate(leads):
                    if self._cancel.is_set():
                        dash.log("Cancelled by user", "WARNING")
                        break
                    await process_lead(i, lead, browser)
            else:
                dash.log(f"Starting {workers} parallel workers...", "INFO")
                # Setup extra browsers
                worker_browsers = [browser]
                for _ in range(workers - 1):
                    wb = BrowserManager(
                        headless=headless, stealth=stealth,
                        locale=os.getenv("BROWSER_LOCALE", "en-US"),
                        timezone=os.getenv("BROWSER_TIMEZONE", "America/New_York"),
                    )
                    await wb.start()
                    worker_browsers.append(wb)

                # Initialize Queue
                queue = asyncio.Queue()
                for i, lead in enumerate(leads):
                    queue.put_nowait((i, lead))

                async def worker_task(wb: BrowserManager):
                    while not queue.empty() and not self._cancel.is_set():
                        try:
                            i, lead = queue.get_nowait()
                            await process_lead(i, lead, wb)
                            queue.task_done()
                        except asyncio.QueueEmpty:
                            break
                        except Exception as e:
                            dash.log(f"Worker exception: {e}", "ERROR")
                            try: queue.task_done()
                            except: pass

                # Execute tasks concurrently
                tasks = [asyncio.create_task(worker_task(wb)) for wb in worker_browsers]
                await asyncio.gather(*tasks)

                if self._cancel.is_set():
                    dash.log("Cancelled by user", "WARNING")

                # Tear down extra browsers
                for wb in worker_browsers[1:]:
                    await wb.close()

            # Done
            writer.close()
            end_time = time.time()
            report = generate_report(
                query=query, results=writer.get_results(),
                key_pool_status=pool.status(),
                start_time=self._start_ts, end_time=end_time, query_slug=slug,
            )
            dash.set_phase("DONE")
            dash.log("Scraping completed!", "SUCCESS")
            output_name = csv_path or json_path or ""
            if output_name:
                dash.log(f"Output saved: {output_name}", "SUCCESS")
            self._q.put(("done", {"report": report}))

        except Exception as e:
            dash.log(f"Error: {e}", "ERROR")
            self._q.put(("done", {}))
        finally:
            await browser.close()
            dash.log("Browser closed")
