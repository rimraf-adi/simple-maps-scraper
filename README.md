# Simple Maps Scraper

Scrapes Google Maps search results for business contact details using [Scrapling](https://github.com/nicoverbruggen/scrapling) and [Playwright](https://playwright.dev/).

## Setup

```sh
uv sync
uv run python -m patchright install
```

## Usage

```sh
uv run main.py <search query>
```

### Examples

```sh
uv run main.py Dentists in Miami
uv run main.py Plumbers in Chicago
uv run main.py Coffee shops in Austin
```

Output is saved to a CSV file named after the query, e.g. `dentists_in_miami.csv`.

## How it works

1. Opens a Google Maps search for the query
2. Scrolls the results feed to load all listings
3. Extracts names, ratings, and map URLs from each listing
4. Visits each place page individually to collect phone, website, and address
5. Saves everything to a CSV
