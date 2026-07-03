import asyncio
from browser.manager import BrowserManager
from maps_scraper import fetch_place_details

async def main():
    browser = BrowserManager(headless=True, stealth=False)
    await browser.start()
    url = "https://www.google.com/maps/place/Lee+91+Spa/data=!4m7!3m6!1s0x89c2595dd7e00b23:0x8f4010bc09afe88a!8m2!3d40.7824072!4d-73.9532259!16s%2Fg%2F11j0xpbjs6!19sChIJIwvg111ZwokRiuivCbwQQI8?authuser=0&hl=en&rclk=1"
    print("Navigating...")
    p, w, a = await fetch_place_details(browser, url)
    print(f"Phone: {p}")
    print(f"Website: {w}")
    print(f"Address: {a}")
    await browser.close()

asyncio.run(main())
