"""
Downloads all season tables (tablice sezone) from the Croatian Athletics
Federation website (has.hr) for every year and every age group, both
outdoor and indoor.

Rather than guessing filenames (naming conventions changed over the years,
e.g. old years use .txt/.htm with different prefixes), this script parses
the actual links from the index page and downloads exactly what it finds.
"""

import os
import re
import time
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.has.hr/index.php/statistika/tablice-sezone"
SITE_ROOT = "https://www.has.hr"
OUTPUT_DIR = "tablice_sezone"
HEADERS = {"User-Agent": "Mozilla/5.0"}

CLUBS_URL = "https://www.has.hr/index.php/klubovi"
CLUBS_OUTPUT_DIR = os.path.join(OUTPUT_DIR, "klubovi")
CLUBS_MAX_PAGES = 30  # safety cap; real site has ~8 pages as of 2026

# Current season files keep the same filename all season while has.hr keeps
# appending results to them, so they must be re-fetched every run rather
# than skipped just because a local copy already exists.
CURRENT_YEAR = date.today().year
FORCE_REFRESH_YEARS = {CURRENT_YEAR}


def get_table_links():
    resp = requests.get(BASE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/tabsez/" in href:
            links.append(urljoin(SITE_ROOT, href))

    # de-duplicate while preserving order
    seen = set()
    unique_links = []
    for link in links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
    return unique_links


def download_all():
    links = get_table_links()
    print(f"Found {len(links)} season table files.")

    for i, url in enumerate(links, 1):
        match = re.search(r"/tabsez/(\d{4})/([^/]+)$", url)
        if not match:
            print(f"  skip (unexpected url): {url}")
            continue
        year, filename = match.groups()

        year_dir = os.path.join(OUTPUT_DIR, year)
        os.makedirs(year_dir, exist_ok=True)
        dest_path = os.path.join(year_dir, filename)

        if os.path.exists(dest_path) and int(year) not in FORCE_REFRESH_YEARS:
            print(f"[{i}/{len(links)}] already have {year}/{filename}")
            continue

        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[{i}/{len(links)}] FAILED {url}: {e}")
            continue

        with open(dest_path, "wb") as f:
            f.write(resp.content)
        print(f"[{i}/{len(links)}] saved {year}/{filename}")

        time.sleep(0.2)  # be polite to the server


def download_clubs():
    """Downloads every page of the has.hr club directory (name/city/code per club).
    Small and cheap (a handful of pages), so always re-fetched in full rather than
    cached with skip-if-exists like the season tables."""
    os.makedirs(CLUBS_OUTPUT_DIR, exist_ok=True)
    page = 1
    saved = 0
    while page <= CLUBS_MAX_PAGES:
        url = CLUBS_URL if page == 1 else f"{CLUBS_URL}/{page}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"clubs page {page} FAILED {url}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        if not soup.find("h2", class_="uk-h3"):
            print(f"clubs page {page}: no club entries, stopping.")
            break

        dest_path = os.path.join(CLUBS_OUTPUT_DIR, f"page_{page}.html")
        with open(dest_path, "wb") as f:
            f.write(resp.content)
        saved += 1
        print(f"saved klubovi/page_{page}.html")
        page += 1
        time.sleep(0.2)  # be polite to the server

    print(f"Saved {saved} club directory pages.")


if __name__ == "__main__":
    download_all()
    download_clubs()
