import asyncio
from scrapling.fetchers import AsyncStealthySession

async def main():
    async def process_page(page):
        print("Initial page URL:", page.url)
        # Check if it's a Bitly preview page
        target_url = await page.evaluate('''() => {
            const btn = document.getElementById("action:continue") || 
                        document.getElementById("action:continue_sm") || 
                        document.querySelector("a.preview__continue") ||
                        document.querySelector("a[id*='action:']");
            return btn ? btn.href : null;
        }''')
        if target_url:
            print("Detected Bitly preview! Navigating to:", target_url)
            await page.goto(target_url, timeout=20000, wait_until="domcontentloaded")
            print("After goto, page URL:", page.url)

    async with AsyncStealthySession(headless=True) as session:
        resp = await session.fetch("https://bit.ly/google_acepointconsulting", page_action=process_page)
        print("Final resp.url:", resp.url)
        print("Final HTML title:", resp.css("title::text").get())

asyncio.run(main())
