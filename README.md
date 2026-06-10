# Maps Scraper

Scrapes Google Maps search results for businesses and extracts their emails
using an LLM-powered browser agent that navigates each website.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python package manager)
- Playwright browsers (installed below)
- An OpenAI-compatible LLM API key (Groq, OpenAI, etc.)

## Setup

```sh
# Install dependencies
uv sync

# Install Playwright browsers
uv run playwright install chromium

# Configure API key
cp .env.example .env
# Edit .env with your LLM_API_KEY
```

## Usage

```sh
uv run main.py "Business coaches in New York"
uv run main.py "Plumbers in Chicago" --headless
```

Output is saved to a CSV file named after the query, e.g.
`business_coaches_in_new_york.csv`.

### Options

- `--headless` — run browser in headless mode (no visible window)

## Environment Variables

| Variable       | Default                                  | Description                  |
|---------------|------------------------------------------|------------------------------|
| `LLM_BASE_URL` | `https://api.groq.com/openai/v1`        | OpenAI-compatible API URL    |
| `LLM_API_KEY`  | —                                        | API key                      |
| `LLM_MODEL`    | `openai/gpt-oss-120b`                   | Model name                   |

## How It Works

1. **Google Maps Search** — opens Maps, scrolls the results feed, and extracts
   business names, ratings, categories, and place-page URLs.

2. **Place Details** — visits each place page to collect phone, website, and
   address.

3. **Email Extraction** — for each business with a website, a LangGraph agent
   navigates the site using an LLM (text-based, with optional vision). The
   agent pre-scrolls for dynamic content, auto-detects emails via regex, and
   clicks navigation links to find contact pages.

4. **CSV Output** — all data is written to a single CSV with columns: Name,
   Phone, Email, Website, Address, Rating, Category.

## Running Tests

```sh
uv run python test/test.py
uv run python test/test_maps.py "Business coaches in New York"
```

## Project Structure

```
├── agent/            LangGraph agent — perceive/plan/act loop
├── browser/          Playwright browser manager
├── llm/              OpenAI-compatible LLM client
├── test/             Tests and sample data
├── main.py           Production entry point
├── maps_scraper.py   Google Maps scraping logic
├── extract_emails.py Email extraction pipeline
└── .env.example      Environment variable template
```
