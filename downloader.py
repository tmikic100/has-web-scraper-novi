"""
Downloads all season tables (tablice sezone) from the Croatian Athletics
Federation website (has.hr) for every year and every age group, both
outdoor and indoor.

Rather than guessing filenames (naming conventions changed over the years,
e.g. old years use .txt/.htm with different prefixes), this script parses
the actual links from the index page and downloads exactly what it finds.
"""

# --- Imports -----------------------------------------------------------
# Standard library modules (these ship with Python, no "pip install" needed).
import os               # filesystem helpers: making directories, joining paths
import re                # "regular expressions" -- pattern matching on text
import time              # time.sleep() to pause between requests
from datetime import date            # gives us today's date, used to find the current year
from urllib.parse import urljoin     # turns a relative link ("/foo/bar") into a full URL

# Third-party libraries (installed via requirements.txt / pip).
import requests           # makes HTTP requests (like a browser, but from code)
from bs4 import BeautifulSoup   # parses HTML so we can search it like a tree of tags

# --- Constants -----------------------------------------------------------
# ALL_CAPS names are a Python convention meaning "this is a constant, don't
# reassign it". Python doesn't actually enforce that -- it's just a convention
# everyone agrees to follow so readers know these values aren't meant to change.
BASE_URL = "https://www.has.hr/index.php/statistika/tablice-sezone"
SITE_ROOT = "https://www.has.hr"
OUTPUT_DIR = "tablice_sezone"
HEADERS = {"User-Agent": "Mozilla/5.0"}  # pretend to be a normal browser, not a bot

CLUBS_URL = "https://www.has.hr/index.php/klubovi"
CLUBS_OUTPUT_DIR = os.path.join(OUTPUT_DIR, "klubovi")
CLUBS_MAX_PAGES = 30  # safety cap; real site has ~8 pages as of 2026

# Current season files keep the same filename all season while has.hr keeps
# appending results to them, so they must be re-fetched every run rather
# than skipped just because a local copy already exists.
CURRENT_YEAR = date.today().year
# A `set` (curly braces, like `{1, 2, 3}`) is used here instead of a list because
# membership checks ("is this year in the set?") are what we need, and sets do
# that lookup in roughly constant time regardless of how many years are in it.
FORCE_REFRESH_YEARS = {CURRENT_YEAR}


def get_table_links():
    """Fetches the has.hr index page and pulls out every link that points to
    a season table file. Returns a plain Python list of full URLs."""
    # requests.get() sends an HTTP GET request, just like typing the URL into
    # a browser's address bar. `resp` holds the server's response.
    resp = requests.get(BASE_URL, headers=HEADERS, timeout=30)
    # If the server responded with an error status (404, 500, ...), this
    # raises an exception immediately instead of silently continuing with
    # broken/empty data.
    resp.raise_for_status()
    # BeautifulSoup parses the raw HTML text into a navigable tree of tags,
    # similar to how a browser turns HTML into the DOM.
    soup = BeautifulSoup(resp.text, "html.parser")

    links = []
    # soup.find_all(...) returns every <a> tag that actually has an href
    # attribute. This is a for-loop iterating over that list of tag objects.
    for a in soup.find_all("a", href=True):
        href = a["href"]  # tags support dict-style access for their attributes
        if "/tabsez/" in href:
            # urljoin turns a relative link like "/tabsez/2026/foo.htm" into
            # "https://www.has.hr/tabsez/2026/foo.htm" using SITE_ROOT as the
            # base. If href is already a full URL, urljoin leaves it as-is.
            links.append(urljoin(SITE_ROOT, href)) # type: ignore

    # de-duplicate while preserving order
    # A plain `set()` would de-duplicate too, but sets don't remember insertion
    # order reliably for this purpose, so we track "have we seen this?" with a
    # set (fast lookups) while building a separate list (keeps the order).
    seen = set()
    unique_links = []
    for link in links:
        if link not in seen:
            seen.add(link)
            unique_links.append(link)
    return unique_links


def download_all():
    """Downloads every season table file found by get_table_links(), saving
    each one under tablice_sezone/<year>/<filename>. Skips files we already
    have locally, except for the current year (see FORCE_REFRESH_YEARS)."""
    links = get_table_links()
    print(f"Found {len(links)} season table files.")

    # enumerate(links, 1) walks the list while also giving us a running
    # counter `i` starting at 1 (instead of the default 0) -- handy for
    # "[3/50] doing thing" style progress messages.
    for i, url in enumerate(links, 1):
        # re.search looks for the pattern anywhere in the string. The
        # parentheses `(...)` are "capture groups" -- if the pattern matches,
        # we can pull out just those parts. Here: a 4-digit year, then a
        # slash, then everything up to the end as the filename.
        match = re.search(r"/tabsez/(\d{4})/([^/]+)$", url)
        if not match:
            print(f"  skip (unexpected url): {url}")
            continue  # skip straight to the next loop iteration
        # .groups() returns the two captured pieces as a tuple, which we
        # immediately "unpack" into two separate variables.
        year, filename = match.groups()

        year_dir = os.path.join(OUTPUT_DIR, year)
        # exist_ok=True means "don't error if this directory already exists".
        os.makedirs(year_dir, exist_ok=True)
        dest_path = os.path.join(year_dir, filename)

        # Skip re-downloading a file we already have, UNLESS its year is in
        # FORCE_REFRESH_YEARS (the current year, whose page has.hr keeps
        # updating in place all season).
        if os.path.exists(dest_path) and int(year) not in FORCE_REFRESH_YEARS:
            print(f"[{i}/{len(links)}] already have {year}/{filename}")
            continue

        # try/except catches errors instead of letting them crash the whole
        # script -- if one file's download fails (network hiccup, has.hr
        # briefly down, etc.), we log it and move on to the next file.
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[{i}/{len(links)}] FAILED {url}: {e}")
            continue

        # "wb" = write, binary mode -- we're saving the raw bytes of the
        # response exactly as received (HTML can have odd encodings, so we
        # don't want Python trying to decode/re-encode it as text here).
        # The `with` block ("context manager") guarantees the file gets
        # closed automatically once we leave the block, even if an error
        # happens partway through the write.
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
    # A `while` loop, unlike `for`, keeps going until its condition is false
    # -- used here because we don't know in advance how many pages exist;
    # we just keep asking for the next page until the site stops giving us
    # club listings (or we hit the safety cap).
    while page <= CLUBS_MAX_PAGES:
        # Page 1 has a different URL shape (no page number suffix) than every
        # page after it -- this ternary expression ("A if condition else B")
        # picks the right one in a single line.
        url = CLUBS_URL if page == 1 else f"{CLUBS_URL}/{page}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"clubs page {page} FAILED {url}: {e}")
            break  # stop the while loop entirely, not just this iteration

        soup = BeautifulSoup(resp.text, "html.parser")
        # If this page has no club-heading elements at all, we've walked
        # past the last real page (has.hr just serves an empty page instead
        # of a 404), so stop here.
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


# This `if` block only runs when the file is executed directly (e.g.
# `python downloader.py`), NOT when it's imported by another module (like
# `import downloader`). It's the standard Python entry-point pattern.
if __name__ == "__main__":
    download_all()
    download_clubs()
