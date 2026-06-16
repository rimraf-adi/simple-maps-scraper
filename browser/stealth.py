"""
Stealth browser utilities — User-Agent rotation, JS stealth injection,
and anti-detection fingerprint evasion.
"""

from __future__ import annotations

import random

# ── 12 Real Chrome/Firefox User-Agent strings ──────────────────────────────

USER_AGENTS: list[str] = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome on Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
    # Firefox on Linux
    "Mozilla/5.0 (X11; Linux x86_64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ── Common viewport resolutions ────────────────────────────────────────────

VIEWPORTS: list[dict[str, int]] = [
    {"width": 1280, "height": 800},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]


def random_user_agent() -> str:
    """Return a random realistic user-agent string."""
    return random.choice(USER_AGENTS)


def random_viewport() -> dict[str, int]:
    """Return a random common viewport size."""
    return random.choice(VIEWPORTS)


# ── JavaScript stealth injection script ────────────────────────────────────

STEALTH_JS: str = """
// ── Hide webdriver flag ──────────────────────────────────────────────────
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true,
});

// ── Spoof navigator.plugins to look like a real browser ──────────────────
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ];
        plugins.length = 3;
        return plugins;
    },
    configurable: true,
});

// ── Spoof navigator.languages ────────────────────────────────────────────
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
    configurable: true,
});

// ── Spoof window.chrome ──────────────────────────────────────────────────
if (!window.chrome) {
    window.chrome = {
        runtime: {
            onMessage: { addListener: () => {}, removeListener: () => {} },
            sendMessage: () => {},
            connect: () => ({ onMessage: { addListener: () => {} }, postMessage: () => {} }),
        },
        loadTimes: () => ({}),
        csi: () => ({}),
    };
}

// ── Spoof navigator.permissions ──────────────────────────────────────────
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
    p.name === 'notifications'
        ? Promise.resolve({ state: 'denied', onchange: null })
        : originalQuery.call(window.navigator.permissions, p)
);

// ── Spoof device properties ──────────────────────────────────────────────
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8, configurable: true });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8, configurable: true });
Object.defineProperty(navigator, 'maxTouchPoints', { get: () => 0, configurable: true });

// ── Prevent detection via iframe contentWindow ───────────────────────────
const origHTMLIFrameElement = HTMLIFrameElement.prototype.__lookupGetter__('contentWindow');
if (origHTMLIFrameElement) {
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function() {
            const win = origHTMLIFrameElement.call(this);
            if (win) {
                try {
                    Object.defineProperty(win.navigator, 'webdriver', { get: () => undefined });
                } catch(e) {}
            }
            return win;
        },
    });
}

// ── Spoof WebGL renderer ─────────────────────────────────────────────────
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
    if (param === 37445) return 'Intel Inc.';
    if (param === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, param);
};
"""


# ── Block signals that indicate CAPTCHA or IP block ────────────────────────

BLOCK_SIGNALS: list[str] = [
    "google.com/sorry",
    "unusual traffic",
    "captcha",
    "blocked",
    "access denied",
    "please verify you are a human",
    "automated queries",
    "rate limit",
    "too many requests",
]


def check_for_block(url: str, page_text: str) -> str | None:
    """
    Check if the page URL or visible text contains known block signals.
    Returns the matched signal string, or None if no block detected.
    """
    url_lower = url.lower()
    text_lower = page_text[:2000].lower()

    for signal in BLOCK_SIGNALS:
        if signal in url_lower or signal in text_lower:
            return signal

    return None
