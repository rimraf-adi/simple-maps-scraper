import asyncio
from browser.manager import BrowserManager
import re

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

async def test():
    b = BrowserManager(headless=True)
    await b.start()
    await b.navigate("https://spinoffstudio.com") # Using user's site or example
    snap = await b.take_snapshot()
    emails = []
    for m in EMAIL_RE.finditer(snap.visible_text):
        emails.append(m.group(0))
    print("Emails in text:", emails)
    
    mailtos = []
    for el in snap.interactive_elements:
        href = el.get("href", "")
        if href.startswith("mailto:"):
            mailtos.append(href)
    print("Mailto links:", mailtos)
    await b.close()

asyncio.run(test())
