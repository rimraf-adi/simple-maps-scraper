import asyncio
import csv
import re
import sys
from urllib.parse import urlparse
from scrapling.fetchers import Fetcher, AsyncStealthySession

async def fetch_place_details(session, url):
    resp = await session.fetch(url, network_idle=True, timeout=30000)

    phone_btn = resp.css('button[data-item-id*="phone"]').first
    phone = phone_btn.attrib.get('aria-label', '').replace('Phone: ', '').strip() if phone_btn else ''

    website_link = resp.css('a[data-item-id*="authority"]').first
    website = website_link.attrib.get('href', '').strip() if website_link else ''

    addr_btn = resp.css('button[data-item-id="address"]').first
    address = addr_btn.attrib.get('aria-label', '').replace('Address: ', '').strip() if addr_btn else ''

    return phone, website, address

def extract_emails(html):
    emails = set()
    for m in re.finditer(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', html):
        email = m.group()
        if not email.endswith(('.png', '.jpg', '.jpeg', '.gif', '.svg', '.css', '.js')):
            emails.add(email)
    return list(emails)

def _fetch_emails_sync(website_url):
    if not website_url:
        return []
    try:
        resp = Fetcher.get(website_url, timeout=15)
        emails = extract_emails(resp.text)
        if not emails:
            parsed = urlparse(website_url)
            contact_urls = [
                f"{parsed.scheme}://{parsed.netloc}/contact",
                f"{parsed.scheme}://{parsed.netloc}/contact-us",
                f"{parsed.scheme}://{parsed.netloc}/about",
                f"{parsed.scheme}://{parsed.netloc}/about-us",
            ]
            for cu in contact_urls:
                try:
                    cr = Fetcher.get(cu, timeout=10)
                    emails = extract_emails(cr.text)
                    if emails:
                        break
                except Exception:
                    continue
        return emails
    except Exception:
        return []

async def fetch_emails(website_url):
    return await asyncio.to_thread(_fetch_emails_sync, website_url)

async def main():
    if len(sys.argv) < 2:
        print("Usage: uv run main.py <search query>")
        print("Example: uv run main.py Dentists in Miami")
        return

    search_query = " ".join(sys.argv[1:])
    async with AsyncStealthySession(headless=True) as session:
        url = f"https://www.google.com/maps/search/{search_query.replace(' ', '+')}"

        feed_sel = 'div[role="feed"]'

        async def scroll_feed(page):
            await page.wait_for_selector(feed_sel, timeout=15000)
            for _ in range(5):
                await page.evaluate("document.querySelector('div[role=\"feed\"]').scrollBy(0, 1000)")
                await asyncio.sleep(2)

        resp = await session.fetch(
            url,
            page_action=scroll_feed,
            wait_selector=feed_sel,
            network_idle=True,
            timeout=60000,
        )

        feed = resp.css(feed_sel).first
        leads = []

        for article in feed.css('div[role="article"]'):
            link_elem = article.css('a.hfpxzc').first
            if not link_elem:
                continue
            href = link_elem.attrib.get('href', '')
            if '/maps/place/' not in href:
                continue

            name = article.css('.qBF1Pd::text').get(default="").strip()
            rating = article.css('.MW4etd::text').get(default="").strip()
            category = article.css('.W4Efsd span span:first-child::text').get(default="").strip()

            leads.append({
                "Name": name,
                "Rating": rating,
                "Category": category,
                "URL": href,
            })

        print(f"Found {len(leads)} leads. Fetching contact details...")

        for i, lead in enumerate(leads):
            print(f"  [{i+1}/{len(leads)}] {lead['Name']}...", end=" ", flush=True)
            phone, website, address = await fetch_place_details(session, lead["URL"])
            lead["Phone"] = phone
            lead["Website"] = website
            lead["Address"] = address

            if website:
                emails = await fetch_emails(website)
                lead["Email"] = "; ".join(emails) if emails else ""
            else:
                lead["Email"] = ""
            print("done")

        print(f"\n{'='*80}")
        print(f"{'Name':28s} {'Phone':16s} {'Email':30s} {'Rating':6s}")
        print(f"{'='*80}")
        for lead in leads:
            email = lead.get("Email", "")[:28]
            print(f"{lead['Name'][:26]:28s} {lead['Phone'][:14]:16s} {email:30s} {lead['Rating']:6s}")

        safe_name = search_query.replace(" ", "_").lower()
        csv_file = f"{safe_name}.csv"
        with open(csv_file, "w", newline="") as f:
            clean = [{k: str(v) for k, v in lead.items()} for lead in leads]
            writer = csv.DictWriter(f, fieldnames=["Name", "Phone", "Email", "Website", "Address", "Rating", "Category", "URL"])
            writer.writeheader()
            writer.writerows(clean)
        print(f"\nSaved {len(leads)} leads to {csv_file}")

if __name__ == "__main__":
    asyncio.run(main())
