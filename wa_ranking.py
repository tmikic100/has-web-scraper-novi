"""
Attaches world / European / national list-ranking context to every
individual has.hr result in athletics.db, sourced from worldathletics.org's
toplists.

Run after build_db.py: python wa_ranking.py [--season YEAR]

Scope:
- Only the given season (default: current year) is processed per run. A full
  historical backfill across every season is a deliberate, separate
  follow-up once this is validated against real data.
- Only "standard" (WA-scoreable) individual disciplines -- see HAS_TO_WA.
  Both indoor and outdoor has.hr results are in scope: WA doesn't have a
  generic indoor/outdoor toggle (the toplist URL's environment path segment
  was confirmed live to have no effect on the returned data) -- instead, for
  200m and up, indoor competition on an oversized 200m track gets its own WA
  discipline entirely ("Short Track", e.g. "200 Metres Short Track" /
  200-metres-short-track), separate from the plain outdoor slug. Sub-200m
  sprints (50m/55m/60m + hurdles) and field events don't have that split at
  all -- WA uses one slug regardless of surface (60m is indoor-only in real
  competition, but has.hr has a handful of outdoor 60m entries too; those
  just won't be found on WA's radar, which is fine -- see HAS_TO_WA_INDOOR
  and resolve_wa_discipline for the exact mapping.
- Only each athlete's CURRENT season-best race is ranked on any given run
  (see process_ranking's athlete_info build step). A superseded race keeps
  whatever rank/rank_snapshot rows it already earned while it WAS the SB,
  but stops receiving new ones the moment a better mark replaces it -- so a
  discipline scraped weekly across a season ends up with exactly one new
  rank_snapshot row per week in total, attributed to whichever race was
  actually current that week, rather than one row per week PER historical
  race (which would double/triple-count once a mark's been superseded).
  Season-long rank progression is still visible in athleteSingleYear.html,
  just spread across however many race rows the discipline accumulated.

Ranking uses two kinds of brackets, per discipline/gender/scope (national =
Croatia-filtered, world = unrestricted, europe = Europe-filtered):
- "Native" brackets -- senior, u20, u18 -- have a real WA ageCategory list,
  queried directly. "senior" here is WA's actual unrestricted/open list (no
  age exclusion at all -- literally everyone gets a senior rank, including
  competitive teenagers); u20/u18 are WA's own age-filtered lists, only
  queried for athletes who are actually age-eligible (no point comparing an
  adult's mark against a junior-only field).
- "Derived" brackets -- u23 always, plus u16/u14 for national only -- have
  no real table anywhere (WA doesn't have U23 at all; has.hr doesn't even
  publish a separate U23/"Mladji seniori" file for indoor competitions), so
  they're derived by classifying each row of the *senior* scan by its own
  DOB against the standard EA "age <= N in the competition year" formula
  (birth_year >= season_year - N -- see eligible_brackets/BRACKET_MAX_AGE),
  instead of trusting a WA ageCategory param that doesn't exist for them.
A single result can pick up several brackets from the same senior-list scan
at once (e.g. a 17-year-old is senior-, U18-, U20- and U23-eligible
simultaneously) -- see scan_bracket_list/scan_scope.

Rows with no DOB on file at WA (common for some countries/leagues -- e.g.
~90% of a US-collegiate-heavy indoor list) are treated as senior-only: we
can't confirm their age, but excluding them from every bracket entirely
would silently drop most of a list's real field from the senior count
specifically, making senior ranks look far better than they are.

National ranking does NOT use WA at all -- it's computed purely from
has.hr's own data (see compute_national_ranks). This was tested against
WA's Croatia-filtered list first, but that list turned out to only cover
~30% of has.hr's results even at its most permissive (WA's country lists
only contain results actually submitted to WA -- mostly national/
international-standard marks; the vast majority of ordinary has.hr club
meets never reach WA at all, confirmed live: WA's Croatia women's 100m
senior list tops out at 45 entries with a 12.99s floor, and disabling
bestResultsOnly barely moves that floor to 13.00s since the extra rows are
just repeat races by the *same* already-covered athletes, not new ones).
Ranking has.hr's own results against each other instead guarantees every
result gets a real national rank.

Cost control: an athlete whose season-best mark never shows up on WA's
Croatia-filtered radar at all is skipped entirely for the (much more
expensive) world/Europe scans, rather than attempting an unbounded scan of
those much larger lists for marks that were never going to be found there.
Once an athlete clears that bar, ALL of their races that season (not just
the qualifying one) get ranked via pure mark-value comparison against the
shared lists -- no name-matching needed for that part, since we already
know our own mark values from the DB (see scan_bracket_list). The
Croatia-list scan is still fetched (cheaply -- these lists are short) purely
to build this presence gate now, since it no longer produces national ranks
itself.
"""

# argparse: parses command-line flags like --season 2026.
# json: reads/writes the on-disk scrape cache.
# re: regular expressions.
# sqlite3: talks to athletics.db.
# sys / time: stdout encoding fix (see below) and time.sleep() for rate limiting.
# defaultdict: a dict that auto-creates a default value for any missing key
#              the first time it's accessed (see usages below for exactly
#              how that's used here).
# date: to compute "this year" as the default --season value.
# Path: filesystem paths.
import argparse
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

# requests: makes the actual HTTP calls to worldathletics.org.
# BeautifulSoup: parses the HTML responses.
import requests
from bs4 import BeautifulSoup

# Reusing the exact same name-normalization function build_db.py uses for
# has.hr athlete names, so a WA competitor name and a has.hr athlete name
# collapse to the same key when they refer to the same real person.
from build_db import normalize_name_key

if sys.stdout.encoding.lower() != 'utf-8':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / 'athletics.db'
CACHE_PATH = BASE_DIR / 'wa_ranking_cache.json'
# --test (see main()) points these at athletics_test.db instead -- the same
# database build_db.py --test builds from test_data/. A separate cache file
# keeps the fake test athletes' (mostly empty) WA lookups from mixing into
# the real cache, and vice versa.
TEST_DB_PATH = BASE_DIR / 'athletics_test.db'
TEST_CACHE_PATH = BASE_DIR / 'wa_ranking_test_cache.json'
HEADERS = {"User-Agent": "Mozilla/5.0"}
TOPLIST_URL = "https://worldathletics.org/records/toplists"

CURRENT_YEAR = date.today().year

# Standard EA "age <= N in the competition year" cutoffs, i.e.
# birth_year >= season_year - N -- used only for the DERIVED brackets (no
# real WA table exists for these). senior/u20/u18 have a real WA
# ageCategory list instead and don't need a cutoff formula at all.
BRACKET_MAX_AGE = {'u23': 22, 'u20': 19, 'u18': 17, 'u16': 15, 'u14': 13}
INTERNATIONAL_DERIVED_BRACKETS = {'u23'}


def eligible_brackets(birth_year, season_year):
    """'senior' is WA's real, unrestricted list -- every athlete is checked
    against it regardless of age, since that's what "no age restriction"
    means (a competitive teenager's mark is just as much on the senior list
    as an adult's). u20/u18 are WA's real, natively age-filtered lists --
    only worth checking for athletes who are actually age-eligible, to avoid
    a meaningless comparison against a junior-only field. u23/u16/u14 have
    no real table anywhere and get derived from the senior scan instead (see
    scan_bracket_list) -- also gated by real eligibility here, so e.g. a
    40-year-old is never even attempted against a U23 derivation.

    Rows with no DOB on file at WA (common for some countries/leagues --
    confirmed live, e.g. ~90% of a US-collegiate-heavy indoor 55m world
    list) fall back to senior-only: we can't confirm their age, but treating
    them as ineligible for senior too (the previous behaviour) silently
    dropped most of a list's real field from the senior count specifically,
    making senior ranks look far better than they are."""
    # A set literal -- starts containing just 'senior'; more brackets get
    # added below with .add() as they're determined to apply. Sets (not
    # lists) are used for bracket collections throughout this file since
    # "is bracket X in this collection?" is the only operation we ever need,
    # and duplicates couldn't happen/wouldn't mean anything anyway.
    brackets = {'senior'}
    if birth_year is None:
        return brackets
    for bracket, max_age in BRACKET_MAX_AGE.items():
        if birth_year >= season_year - max_age:
            brackets.add(bracket)
    return brackets


# has.hr discipline.name (normalize_discipline_name's output; standard,
# outdoor disciplines only) -> WA toplist (group slug, discipline slug).
# Cross-referenced against the live event catalogue embedded in
# https://worldathletics.org/records/toplists (toplists.init's eventId
# filter cases) during development -- same event universe already
# established by score_agr_2025.EVENT_MAP for WA-points scoring.
HAS_TO_WA = {
    '50m': ('sprints', '50-metres'),
    '55m': ('sprints', '55-metres'),
    '60m': ('sprints', '60-metres'),
    '100m': ('sprints', '100-metres'),
    '200m': ('sprints', '200-metres'),
    '300m': ('sprints', '300-metres'),
    '400m': ('sprints', '400-metres'),
    '500m': ('sprints', '500-metres'),
    '600m': ('middlelong', '600-metres'),
    '800m': ('middlelong', '800-metres'),
    '1000m': ('middlelong', '1000-metres'),
    '1500m': ('middlelong', '1500-metres'),
    'Milja': ('middlelong', 'one-mile'),
    '2000m': ('middlelong', '2000-metres'),
    '2000 m zapreke': ('middlelong', '2000-metres-steeplechase'),
    '3000m': ('middlelong', '3000-metres'),
    '3000 m zapreke': ('middlelong', '3000-metres-steeplechase'),
    '5000m': ('middlelong', '5000-metres'),
    '10000m': ('middlelong', '10000-metres'),
    '50 m prepone': ('hurdles', '50-metres-hurdles'),
    '55 m prepone': ('hurdles', '55-metres-hurdles'),
    '60 m prepone': ('hurdles', '60-metres-hurdles'),
    '100 m prepone': ('hurdles', '100-metres-hurdles'),
    '110 m prepone': ('hurdles', '110-metres-hurdles'),
    '400 m prepone': ('hurdles', '400-metres-hurdles'),
    'Dalj': ('jumps', 'long-jump'),
    'Vis': ('jumps', 'high-jump'),
    'Motka': ('jumps', 'pole-vault'),
    'Troskok': ('jumps', 'triple-jump'),
    'Kugla': ('throws', 'shot-put'),
    'Disk': ('throws', 'discus-throw'),
    'Kladivo': ('throws', 'hammer-throw'),
    'Koplje': ('throws', 'javelin-throw'),
    '5 km (cesta)': ('road-running', '5-kilometres'),
    '10 km (cesta)': ('road-running', '10-kilometres'),
    'Polumaraton': ('road-running', 'half-marathon'),
    'Maraton': ('road-running', 'marathon'),
    '5 km hodanje (cesta)': ('race-walks', '5-kilometres-race-walk'),
    '10 km hodanje (cesta)': ('race-walks', '10-kilometres-race-walk'),
    '20 km hodanje (cesta)': ('race-walks', '20-kilometres-race-walk'),
    '35 km hodanje (cesta)': ('race-walks', '35-kilometres-race-walk'),
    '3000 m hodanje': ('race-walks', '3000-metres-race-walk'),
    '5000 m hodanje': ('race-walks', '5000-metres-race-walk'),
    '10000 hodanje': ('race-walks', '10000-metres-race-walk'),
}

# has.hr discipline.name -> WA (group, slug) to use specifically when
# discipline.indoor = 1, for the events where indoor competition (on an
# oversized 200m "short track") is a genuinely distinct WA discipline from
# its outdoor counterpart -- confirmed against the live event catalogue
# embedded in https://worldathletics.org/records/toplists (every event with
# a "* Short Track" label). Only 200m-and-up track/walk events have this
# split; anything not listed here uses the same HAS_TO_WA slug regardless of
# indoor/outdoor (sub-200m sprints and their hurdles are indoor-only in real
# competition with no separate WA outdoor discipline at all, and field
# events aren't split by surface on WA's side either).
HAS_TO_WA_INDOOR = {
    '200m': ('sprints', '200-metres-short-track'),
    '300m': ('sprints', '300-metres-short-track'),
    '400m': ('sprints', '400-metres-short-track'),
    '500m': ('sprints', '500-metres-short-track'),
    '600m': ('middlelong', '600-metres-short-track'),
    '800m': ('middlelong', '800-metres-short-track'),
    '1000m': ('middlelong', '1000-metres-short-track'),
    '1500m': ('middlelong', '1500-metres-short-track'),
    'Milja': ('middlelong', 'one-mile-short-track'),
    '2000m': ('middlelong', '2000-metres-short-track'),
    '3000m': ('middlelong', '3000-metres-short-track'),
    '2 milje': ('middlelong', 'two-miles-short-track'),
    '5000m': ('middlelong', '5000-metres-short-track'),
    '3000 m hodanje': ('race-walks', '3000-metres-race-walk-short-track'),
}


def resolve_wa_discipline(discipline_name, indoor):
    """Given a has.hr discipline name and whether the race was indoor,
    returns the (WA group slug, WA discipline slug) tuple to query, or None
    if this discipline has no WA equivalent at all (e.g. combined events)."""
    if indoor and discipline_name in HAS_TO_WA_INDOOR:
        return HAS_TO_WA_INDOOR[discipline_name]
    return HAS_TO_WA.get(discipline_name)


# A dict comprehension building {'JAN': 1, 'FEB': 2, ..., 'DEC': 12} from a
# plain list of month abbreviations, using enumerate to pair each name with
# its 1-based position.
_MONTHS = {m: i + 1 for i, m in enumerate(
    ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'])}


def parse_dob(text):
    """Parses WA's "DD MON YYYY" date-of-birth text (e.g. "05 MAY 1990")
    into ISO "YYYY-MM-DD" format, or None if the text doesn't match."""
    m = re.match(r'(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})', text.strip())
    if not m:
        return None
    day, mon, year = m.groups()
    month = _MONTHS.get(mon.upper())
    if not month:
        return None
    return f"{year}-{month:02d}-{int(day):02d}"


def parse_mark_value(mark):
    """WA's own 'Mark' column format -- period-decimal, optional 'M:SS.ss'
    for track events, plain 'X.XX' for field events. Distinct from
    parse_tables.normalize_mark, which handles has.hr's Croatian
    comma-decimal source format instead."""
    mark = mark.strip()
    if not mark or mark == '-':
        return None
    if ':' in mark:
        parts = mark.split(':')
        try:
            total = float(parts[-1])
            for i, part in enumerate(reversed(parts[:-1])):
                total += int(part) * (60 ** (i + 1))
            return total
        except ValueError:
            return None
    try:
        return float(mark)
    except ValueError:
        return None


def parse_toplist_rows(html):
    """Parses a records-table page into rank-ordered rows. Matched on the
    td[data-th=...] attributes rather than column position, since the WIND
    column (and others) aren't present for every discipline."""
    soup = BeautifulSoup(html, 'html.parser')
    table = soup.find('table', class_='records-table')
    if table is None or table.find('tbody') is None:
        return []
    rows = []
    for tr in table.find('tbody').find_all('tr'):
        cells = {}
        for td in tr.find_all('td'):
            # data-th is a custom HTML attribute WA's page uses to label
            # each cell with its column name (useful for responsive/mobile
            # layouts) -- we piggyback on it here as a reliable way to find
            # "the Mark cell" etc. regardless of column order, since not
            # every discipline's table has the same set of columns (e.g.
            # WIND is absent for field events).
            key = (td.get('data-th') or '').strip()
            cells[key] = td.get_text(' ', strip=True)
        name = cells.get('Competitor', '').strip()
        if not name:
            continue
        rows.append({
            'name_key': normalize_name_key(name),
            'mark_value': parse_mark_value(cells.get('Mark', '')),
            'dob': parse_dob(cells.get('DOB', '')),
        })
    return rows


class Cache:
    """Persistent scrape cache, committed to the repo -- must survive both
    build_db.py's full DB wipe-and-rebuild and fresh GH Actions checkouts,
    or every weekly run would re-scrape everything from scratch."""

    def __init__(self, path):
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding='utf-8'))
        else:
            self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.data[key] = value

    def save(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding='utf-8')


def fetch_page(group, slug, gender, age_category, season, region_type, region, page, best_only):
    """Fetches and parses one page of one WA toplist. Retries transient
    network failures with increasing backoff (2s, 4s, 6s, 8s) instead of
    giving up on the first hiccup, since these scrapes run for a long time
    and a single flaky request shouldn't abort the whole run."""
    url = f"{TOPLIST_URL}/{group}/{slug}/outdoor/{gender}/{age_category}/{season}"
    params = {
        'regionType': region_type,
        'timing': 'electronic',
        'windReading': 'all',
        'page': page,
        # WA's API takes the boolean as a lowercase text string in the URL,
        # not a native JSON/Python bool -- str(True).lower() -> "true".
        'bestResultsOnly': str(best_only).lower(),
        'maxResultsByCountry': 'all',
        'ageCategory': age_category,
    }
    if region:
        params['region'] = region
    last_error = None
    for attempt in range(4):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=60)
            resp.raise_for_status()
            return parse_toplist_rows(resp.text)
        except requests.RequestException as e:
            last_error = e
            print(f"    WARN fetch failed (attempt {attempt + 1}/4): {e}", flush=True)
            time.sleep(2 * (attempt + 1))
    # All 4 attempts failed -- re-raise the last error we saw instead of
    # silently returning something like an empty list, since a genuine
    # network failure here should stop the run rather than be
    # misinterpreted as "this list has no more results".
    raise last_error


def scan_bracket_list(cache, force_refresh, group, slug, gender, age_category, season,
                       region_type, region, pending):
    """Pages ONE WA ageCategory list (senior/u20/u18) top-to-bottom, resolving
    pending races' ranks via pure mark-value comparison: rank = 1 + count of
    bracket-eligible entries strictly better than this mark (ties naturally
    share a rank). No name matching is involved in resolution -- we already
    know our own mark values from the DB, so a non-best race doesn't need to
    be "found" as a named row at all. No artificial depth cap: the scan only
    stops once every pending entry is resolved or the list is exhausted (a
    page comes back with 0 rows -- WA returns 200/empty past the last page
    rather than erroring, confirmed live).

    Each pending entry can request two kinds of brackets from this one scan:
      - the NATIVE bracket == age_category itself: every row in this list
        counts unconditionally, since WA already filtered it correctly (for
        age_category='senior' that means literally everyone -- no age
        restriction at all).
      - DERIVED brackets (u23/u16/u14), only meaningful when
        age_category == 'senior': each row is additionally classified by its
        own DOB against BRACKET_MAX_AGE, since WA has no real table for
        these at all.

    pending: dict raceathlete_id -> {mark_value, higher_is_better, brackets}
      where `brackets` is the subset of {age_category} | {'u23','u16','u14'}
      this particular race needs resolved from this list.

    Returns (results, seen_name_keys):
      results: raceathlete_id -> {bracket: rank}
      seen_name_keys: every competitor name_key encountered while scanning
        -- a free byproduct usable as a name-based presence check without a
        second fetch.
    """
    # by_bracket groups every pending (raceathlete, mark) entry by which
    # bracket it needs resolved -- so bracket 'u23' has its own list of
    # entries to compare against, separate from bracket 'senior', even
    # though they're scanning the exact same underlying WA list rows.
    # defaultdict(list) means "the first time a new key is accessed, create
    # an empty list for it automatically" -- so `by_bracket[b].append(...)`
    # works immediately even the first time bracket `b` is ever seen,
    # without a manual "if b not in by_bracket: by_bracket[b] = []" check.
    by_bracket = defaultdict(list)
    for raceathlete_id, info in pending.items():
        for b in info['brackets']:
            by_bracket[b].append({
                'raceathlete_id': raceathlete_id,
                'mark_value': info['mark_value'],
                'higher_is_better': info['higher_is_better'],
                'resolved': False,
            })

    results = defaultdict(dict)
    seen_name_keys = set()
    # A dict comprehension: one running counter per bracket, all starting
    # at 0, counting how many list rows we've scanned so far that count
    # toward that bracket.
    counts = {b: 0 for b in by_bracket}
    unresolved_total = sum(len(v) for v in by_bracket.values())
    total_pending = unresolved_total
    page = 1
    # Keep paging through the WA list until either every pending entry has
    # been resolved (unresolved_total hits 0) or we run out of pages
    # (handled by the `if not rows: break` below).
    while unresolved_total > 0:
        # The cache key uniquely identifies this exact page of this exact
        # list -- json.dumps(..., sort_keys=True) turns the parameter list
        # into a single consistent string usable as a dict key (a plain
        # Python list can't be a dict key directly, since lists are
        # mutable/unhashable; a string can).
        cache_key = json.dumps(
            [group, slug, gender, age_category, season, region_type, region, page], sort_keys=True)
        cached = cache.get(cache_key)
        rows = None if force_refresh else cached
        if rows is None:
            rows = fetch_page(group, slug, gender, age_category, season, region_type, region, page, True)
            cache.set(cache_key, rows)
            time.sleep(0.3)  # be polite to worldathletics.org's servers
        # Progress logging: print on the first page, every 5th page, and
        # the final (empty) page -- frequent enough to show real progress
        # on a long scan, without flooding the console with a line for
        # every single page of a list that might be hundreds of pages deep.
        if page == 1 or page % 5 == 0 or not rows:
            src = 'cache' if (rows is cached and cached is not None) else 'fetch'
            print(f"      {region or 'world'}/{age_category} page {page} ({src}): "
                  f"{unresolved_total}/{total_pending} unresolved", flush=True)
        if not rows:
            # Ran off the end of the list -- WA just returns an empty page
            # rather than an error, so this is our real stopping signal.
            break
        for row in rows:
            seen_name_keys.add(row['name_key'])
            row_brackets = set()
            if age_category in by_bracket:
                row_brackets.add(age_category)
            if age_category == 'senior':
                row_birth_year = int(row['dob'][:4]) if row['dob'] else None
                if row_birth_year is not None:
                    for bracket, max_age in BRACKET_MAX_AGE.items():
                        if bracket in by_bracket and row_birth_year >= season - max_age:
                            row_brackets.add(bracket)
            row_mark = row['mark_value']
            for b in row_brackets:
                if row_mark is not None:
                    for entry in by_bracket[b]:
                        if entry['resolved']:
                            continue
                        target = entry['mark_value']
                        # Which direction counts as "better" depends on the
                        # event: for a time, a SMALLER mark is better; for
                        # a throw/jump, a BIGGER mark is better.
                        strictly_better = (row_mark > target) if entry['higher_is_better'] else (row_mark < target)
                        if not strictly_better:
                            # This WA row is not better than our target mark
                            # -- meaning everyone counted so far (`counts[b]`
                            # rows strictly better) ranks above it, so our
                            # mark's rank is exactly that count, plus 1.
                            entry['resolved'] = True
                            results[entry['raceathlete_id']][b] = counts[b] + 1
                            unresolved_total -= 1
                counts[b] += 1
        page += 1
    # dict(results) converts the defaultdict back into a plain dict before
    # returning -- defaultdicts are convenient to build but can silently
    # create new empty entries if a caller later does `results[missing_key]`
    # by accident, so returning a plain dict is a small safety measure.
    return dict(results), seen_name_keys


def scan_scope(cache, force_refresh, group_slug, disc_slug, gender, season_year,
                region_type, region, athlete_info, derived_brackets):
    """Runs the senior scan (native 'senior' + derived brackets) plus the
    u20/u18 native scans (only for athletes actually eligible for them) for
    one WA scope, merging results across all of them. Used for world/europe
    ranking (real results kept), and for the Croatia-region call used purely
    as a presence gate (results discarded, only seen_name_keys kept -- see
    process_ranking; national ranking itself no longer comes from here, see
    compute_national_ranks).

    athlete_info: athlete_id -> {name_key, brackets (from eligible_brackets),
      higher_is_better, races: [race rows]}.
    derived_brackets: extra brackets (beyond 'senior') to resolve from the
      senior-list scan -- INTERNATIONAL_DERIVED_BRACKETS for world/europe,
      or an empty set for the presence-gate-only Croatia call.

    Returns (results, seen_name_keys) -- same shape as scan_bracket_list.
    """
    combined_results = defaultdict(dict)
    seen_name_keys = set()

    # --- Pass 1: the 'senior' list, which also yields the derived brackets ---
    senior_pending = {}
    for info in athlete_info.values():
        # `&` between two sets is set INTERSECTION -- "only the brackets
        # this athlete is actually eligible for AND that we were asked to
        # derive here" (e.g. don't bother deriving U23 for a 40-year-old,
        # even though U23 might be in derived_brackets generally).
        brackets = {'senior'} | (info['brackets'] & derived_brackets)
        for r in info['races']:
            senior_pending[r['raceathlete_id']] = {
                'mark_value': r['mark_value'],
                'higher_is_better': info['higher_is_better'],
                'brackets': brackets,
            }
    senior_results, senior_seen = scan_bracket_list(
        cache, force_refresh, group_slug, disc_slug, gender, 'senior', season_year,
        region_type, region, senior_pending)
    for rid, d in senior_results.items():
        # dict.update(other) merges keys from `d` into the existing
        # per-raceathlete bracket->rank dict, rather than overwriting it --
        # important since a later scan (u20/u18 below) needs to ADD its own
        # bracket's rank alongside whatever the senior scan already found.
        combined_results[rid].update(d)
    # `|=` between two sets is set UNION (in place) -- adds every element
    # of senior_seen into seen_name_keys.
    seen_name_keys |= senior_seen

    # --- Pass 2 & 3: the native u20 and u18 lists, only for eligible athletes ---
    for native_bracket in ('u20', 'u18'):
        pending = {}
        for info in athlete_info.values():
            if native_bracket not in info['brackets']:
                continue
            for r in info['races']:
                pending[r['raceathlete_id']] = {
                    'mark_value': r['mark_value'],
                    'higher_is_better': info['higher_is_better'],
                    'brackets': {native_bracket},
                }
        if not pending:
            # No athlete in this group is age-eligible for u20 (or u18) --
            # skip the fetch entirely rather than scanning a whole WA list
            # for a bracket nobody here actually needs.
            continue
        res, seen = scan_bracket_list(
            cache, force_refresh, group_slug, disc_slug, gender, native_bracket, season_year,
            region_type, region, pending)
        for rid, d in res.items():
            combined_results[rid].update(d)
        seen_name_keys |= seen

    return dict(combined_results), seen_name_keys


def compute_national_ranks(athlete_info):
    """Ranks every has.hr result directly against other has.hr results in
    the same discipline/gender/season/bracket -- no WA dependency at all, so
    (unlike world/europe) every race gets a real national rank regardless of
    whether WA ever recorded that athlete. Standard competition ranking:
    rank = 1 + count of strictly-better marks in the same bracket (ties
    share a rank). All 6 brackets (senior + the 5 age-derived ones) are
    purely birth-year-derived here -- has.hr has no separate native
    per-bracket list to query the way WA does for world/europe's
    senior/u20/u18.

    athlete_info: athlete_id -> {brackets (from eligible_brackets),
      higher_is_better, races: [race rows]}. Every race in a call shares the
      same higher_is_better (one discipline/gender group per call).

    Returns raceathlete_id -> {bracket: rank}.
    """
    if not athlete_info:
        return {}
    # iter(dict.values()) gives an iterator over the dict's values; next()
    # pulls just the first one. Used here purely to peek at ONE athlete's
    # higher_is_better flag, since every athlete passed into a single call
    # shares the same discipline (and therefore the same higher_is_better).
    higher_is_better = next(iter(athlete_info.values()))['higher_is_better']
    by_bracket = defaultdict(list)
    for info in athlete_info.values():
        for r in info['races']:
            for b in info['brackets']:
                by_bracket[b].append((r['raceathlete_id'], r['mark_value']))

    results = defaultdict(dict)
    for bracket, entries in by_bracket.items():
        # Sort every (raceathlete_id, mark) pair in this bracket from best
        # to worst. `key=lambda e: e[1]` sorts by the mark value (the
        # second element of each tuple); reverse=higher_is_better flips the
        # order for throws/jumps (where bigger is better) vs times (where
        # Python's default ascending sort already puts the smallest/best
        # time first).
        entries.sort(key=lambda e: e[1], reverse=higher_is_better)
        rank = 0
        prev_mark = None
        for i, (raceathlete_id, mark) in enumerate(entries):
            # Same "competition ranking" logic used elsewhere in this
            # project (see api.py's _individual_best_scores): ties share a
            # rank, and the next distinct mark resumes at its true
            # position in the sorted list (i is 0-based, so +1).
            if mark != prev_mark:
                rank = i + 1
                prev_mark = mark
            results[raceathlete_id][bracket] = rank
    return dict(results)


def get_target_rows(conn, season_year, discipline_name=None):
    """Every individual (non-relay), standard, mark-valued race for the
    season -- one row per raceathlete, no season-best dedup yet (that
    happens per-athlete in process_ranking's athlete_info build step, once
    rows are already grouped by discipline/gender/indoor -- see module
    docstring). Both indoor and outdoor are in scope --
    see resolve_wa_discipline for how the right WA discipline slug is picked
    per row. d.female (not a.female) determines gender: it's tied to which
    race category the result was actually scored under, so it's immune to
    athlete-level gender-data bugs discussed in conversation (a.female was
    wrong/unresolved for some athletes; d.female is authoritative here since
    this query is individual-only, r.info IS NULL, and the analogous
    relay-side discipline-gender bug doesn't apply to individual events).
    r.exclude_from_ranking excludes wind-assisted, irregular-conditions,
    no-wind-data, and out-of-stadium marks (see
    parse_tables.CONDITION_FLAG_PHRASES) -- still real, still stored, just
    not eligible to count towards national/world/europe rank."""
    # `AND (? IS NULL OR d.name = ?)` is a common SQL trick for "this filter
    # is optional": the two `?`s are both bound to the same Python value
    # (discipline_name) below, so passing None makes the whole clause always
    # true (no filtering), while passing an actual name filters normally.
    return conn.execute("""
        SELECT ra.id AS raceathlete_id, a.id AS athlete_id, a.name_key, a.birth_year,
               r.mark_value, d.higher_is_better, d.female, d.indoor,
               d.id AS discipline_id, d.name AS discipline_name
        FROM race r
        JOIN raceathlete ra ON ra.race_id = r.id
        JOIN athlete a ON a.id = ra.athlete_id
        JOIN discipline d ON d.id = r.discipline_id
        JOIN season s ON s.id = r.season_id
        WHERE s.year = ? AND d.standard = 1
          AND r.info IS NULL AND r.mark_value IS NOT NULL AND r.exclude_from_ranking = 0
          AND (? IS NULL OR d.name = ?)
    """, (season_year, discipline_name, discipline_name)).fetchall()


def process_ranking(conn, cache, season_year, force_refresh, group_limit=None, discipline_name=None,
                     scrape_date=None, scrape_week=None, scope='all'):
    """The main driver: fetches every eligible race for the season, groups
    them by (discipline, gender, indoor/outdoor), and for each group
    computes national/world/europe ranks and writes them back to the DB.

    scrape_date/scrape_week tag this run's results into rank_snapshot (in
    addition to updating raceathlete's own "current" rank columns as
    before) -- exactly one of the two should be given: scrape_date for a
    real run (today's date), scrape_week for a --test run (the --test-week
    number). This is what lets a race's rank change over successive scrapes
    while the race's own mark/date (its "SB") never does -- see main().

    scope: 'national', 'international', or 'all' (default). National ranking
    is pure has.hr-vs-has.hr comparison (cheap, no network); international
    (world/europe) needs many worldathletics.org requests per discipline
    (slow, the whole reason update-international-ranking.yml runs on its own
    schedule instead of every week alongside build_db.py). Restricting scope
    means only that scope's columns get computed AND written this run --
    both the raceathlete UPDATE and the rank_snapshot write are scope-aware
    (see below), so a national-only run can never wipe out international
    ranks a separate run already wrote for the same raceathlete (or the same
    scrape_date/week), and vice versa."""
    do_national = scope in ('national', 'all')
    do_international = scope in ('international', 'all')
    target_rows = get_target_rows(conn, season_year, discipline_name)
    print(f"Season {season_year}: {len(target_rows)} individual results to check.", flush=True)

    # A nested defaultdict: disc_groups[key] auto-creates a defaultdict(list)
    # the first time a new `key` is seen, and THAT inner defaultdict then
    # auto-creates an empty list for each new athlete_id -- so this whole
    # two-level structure can be built with a single `.append()` call per
    # row below, no manual "if not present, create it" checks needed at
    # either level.
    disc_groups = defaultdict(lambda: defaultdict(list))
    for row in target_rows:
        key = (row['discipline_id'], row['discipline_name'], row['female'], row['indoor'])
        disc_groups[key][row['athlete_id']].append(row)

    # .items() on a dict gives (key, value) pairs; list(...) materializes
    # them into an actual list so we can slice it below ([:group_limit]) --
    # dict_items views don't support slicing directly.
    group_items = list(disc_groups.items())
    if group_limit is not None:
        group_items = group_items[:group_limit]

    total_written = 0
    # enumerate(..., 1) numbers groups starting at 1 for the "[i/N]"
    # progress messages; the tuple-unpacking in the for-target
    # (`(discipline_id, discipline_name, female, indoor), athletes`)
    # destructures each (key, value) pair from group_items in one step,
    # and further destructures the 4-tuple key itself into named variables.
    for i, ((discipline_id, discipline_name, female, indoor), athletes) in enumerate(group_items, 1):
        wa_disc = resolve_wa_discipline(discipline_name, indoor)
        if wa_disc is None:
            # No WA equivalent for this discipline at all (e.g. combined
            # events) -- national ranking still doesn't need WA, but this
            # whole function currently only handles groups WA can also
            # rank, so these are skipped entirely for now.
            continue
        group_slug, disc_slug = wa_disc
        gender = 'women' if female else 'men'

        athlete_info = {}
        for athlete_id, races in athletes.items():
            higher_is_better = races[0]['higher_is_better']
            # Only the athlete's CURRENT season-best race gets ranked this
            # run -- an athlete can have several race rows in the same
            # discipline/season (build_db.py keeps every old SB around
            # instead of overwriting it), but a superseded one should stop
            # accumulating new rank_snapshot rows once it's no longer the
            # SB (see module docstring). max() with a direction-aware key
            # picks the best mark regardless of whether smaller (times) or
            # bigger (throws/jumps) counts as better.
            best_race = max(
                races,
                key=lambda r: r['mark_value'] if higher_is_better else -r['mark_value'],
            )
            athlete_info[athlete_id] = {
                'name_key': races[0]['name_key'],
                'brackets': eligible_brackets(races[0]['birth_year'], season_year),
                'higher_is_better': higher_is_better,
                'races': [best_race],
            }

        race_count = sum(len(a['races']) for a in athlete_info.values())
        env = 'indoor' if indoor else 'outdoor'
        print(f"  [{i}/{len(group_items)}] {discipline_name} {gender} ({env}, {disc_slug}): "
              f"{len(athlete_info)} athletes / {race_count} races", flush=True)

        # National ranking: pure has.hr-vs-has.hr comparison, no network call.
        national_results = {}
        if do_national:
            national_results = compute_national_ranks(athlete_info)
            print(f"    national (has.hr-derived): {len(national_results)}/{race_count} races ranked", flush=True)

        world_results, europe_results = {}, {}
        if do_international:
            # A cheap WA Croatia-list scan, used ONLY to determine which
            # athletes WA has ever heard of at all (the underscore `_`
            # throws away the first return value -- the actual bracket
            # ranks -- since we don't want WA's Croatia list to produce
            # national ranks anymore, just the presence signal).
            _, seen_names = scan_scope(
                cache, force_refresh, group_slug, disc_slug, gender, season_year,
                'countries', 'cro', athlete_info, set(),
            )
            # A set comprehension: every athlete_id whose name_key showed up
            # anywhere in the Croatia-list scan.
            gated_athlete_ids = {aid for aid, info in athlete_info.items() if info['name_key'] in seen_names}
            print(f"    presence gate (WA cro list): {len(gated_athlete_ids)}/{len(athlete_info)} "
                  f"athletes recognized", flush=True)

            if gated_athlete_ids:
                # A dict comprehension filtering athlete_info down to only
                # the WA-recognized athletes, before running the much more
                # expensive world/europe scans -- this is the actual cost
                # savings the presence gate buys us.
                gated_info = {aid: athlete_info[aid] for aid in gated_athlete_ids}
                gated_race_count = sum(len(a['races']) for a in gated_info.values())
                world_results, _ = scan_scope(
                    cache, force_refresh, group_slug, disc_slug, gender, season_year,
                    'world', None, gated_info, INTERNATIONAL_DERIVED_BRACKETS,
                )
                print(f"    world: resolved {len(world_results)}/{gated_race_count} races", flush=True)
                europe_results, _ = scan_scope(
                    cache, force_refresh, group_slug, disc_slug, gender, season_year,
                    'area', 'europe', gated_info, INTERNATIONAL_DERIVED_BRACKETS,
                )
                print(f"    europe: resolved {len(europe_results)}/{gated_race_count} races", flush=True)

        # Each entry keeps the raw per-scope dicts (rather than a flattened
        # tuple) so the UPDATE/INSERT statements below can each pick out
        # just their own scope's columns.
        updates = []
        for info in athlete_info.values():
            for r in info['races']:
                raceathlete_id = r['raceathlete_id']
                # .get(raceathlete_id, {}) returns an empty dict if this
                # race has no entry at all (e.g. never resolved in that
                # scope, or that scope wasn't even computed this run), so
                # the .get('senior') etc. calls just below are always safe
                # (an empty dict's .get() just returns None).
                nat = national_results.get(raceathlete_id, {})
                w = world_results.get(raceathlete_id, {})
                eu = europe_results.get(raceathlete_id, {})
                if not nat and not w and not eu:
                    # Nothing at all to write for this race -- skip it
                    # rather than issue a no-op write full of NULLs.
                    continue
                updates.append((raceathlete_id, nat, w, eu))

        written = 0
        national_updates, international_updates = [], []
        if do_national:
            # Only touches the national_rank_* columns -- an international-
            # only run never reaches this branch at all, so it can't stomp
            # national ranks written by a separate national-only run.
            national_updates = [(raceathlete_id, nat) for raceathlete_id, nat, w, eu in updates if nat]
            conn.executemany("""
                UPDATE raceathlete SET
                    national_rank_senior = ?, national_rank_u23 = ?, national_rank_u20 = ?,
                    national_rank_u18 = ?, national_rank_u16 = ?, national_rank_u14 = ?
                WHERE id = ?
            """, [
                (nat.get('senior'), nat.get('u23'), nat.get('u20'), nat.get('u18'),
                 nat.get('u16'), nat.get('u14'), raceathlete_id)
                for raceathlete_id, nat in national_updates
            ])
            written += len(national_updates)
        if do_international:
            # Symmetric: only touches world_rank_*/europe_rank_*, so a
            # national-only run (where w/eu are always {}) never reaches
            # this branch and can't null out international ranks.
            international_updates = [(raceathlete_id, w, eu) for raceathlete_id, nat, w, eu in updates if (w or eu)]
            conn.executemany("""
                UPDATE raceathlete SET
                    world_rank_senior = ?, world_rank_u23 = ?, world_rank_u20 = ?, world_rank_u18 = ?,
                    europe_rank_senior = ?, europe_rank_u23 = ?, europe_rank_u20 = ?, europe_rank_u18 = ?
                WHERE id = ?
            """, [
                (w.get('senior'), w.get('u23'), w.get('u20'), w.get('u18'),
                 eu.get('senior'), eu.get('u23'), eu.get('u20'), eu.get('u18'), raceathlete_id)
                for raceathlete_id, w, eu in international_updates
            ])
            written += len(international_updates)

        # rank_snapshot rows use UPSERT (not the previous blind INSERT OR
        # REPLACE) for the same reason as the UPDATEs above: a national-only
        # and an international-only run can legitimately target the SAME
        # (raceathlete_id, scrape_date/week) snapshot (e.g. --test runs
        # commonly rank the same --test-week for both scopes separately) --
        # ON CONFLICT DO UPDATE SET only the scope's own columns leaves
        # whatever the other scope already wrote for that snapshot alone,
        # instead of blowing it away with NULLs. Exactly one of
        # scrape_date/scrape_week is ever set (see the docstring above), so
        # the conflict target matches whichever partial UNIQUE index (see
        # build_db.py's SCHEMA_SQL) applies to this run.
        if scrape_date is not None:
            conflict_cols, conflict_where = "raceathlete_id, scrape_date", "scrape_date IS NOT NULL"
        else:
            conflict_cols, conflict_where = "raceathlete_id, scrape_week", "scrape_week IS NOT NULL"

        if do_national:
            conn.executemany(f"""
                INSERT INTO rank_snapshot(
                    raceathlete_id, scrape_date, scrape_week,
                    national_rank_senior, national_rank_u23, national_rank_u20,
                    national_rank_u18, national_rank_u16, national_rank_u14
                ) VALUES (?,?,?, ?,?,?,?,?,?)
                ON CONFLICT({conflict_cols}) WHERE {conflict_where} DO UPDATE SET
                    national_rank_senior = excluded.national_rank_senior,
                    national_rank_u23 = excluded.national_rank_u23,
                    national_rank_u20 = excluded.national_rank_u20,
                    national_rank_u18 = excluded.national_rank_u18,
                    national_rank_u16 = excluded.national_rank_u16,
                    national_rank_u14 = excluded.national_rank_u14
            """, [
                (raceathlete_id, scrape_date, scrape_week,
                 nat.get('senior'), nat.get('u23'), nat.get('u20'), nat.get('u18'),
                 nat.get('u16'), nat.get('u14'))
                for raceathlete_id, nat in national_updates
            ])
        if do_international:
            conn.executemany(f"""
                INSERT INTO rank_snapshot(
                    raceathlete_id, scrape_date, scrape_week,
                    world_rank_senior, world_rank_u23, world_rank_u20, world_rank_u18,
                    europe_rank_senior, europe_rank_u23, europe_rank_u20, europe_rank_u18
                ) VALUES (?,?,?, ?,?,?,?, ?,?,?,?)
                ON CONFLICT({conflict_cols}) WHERE {conflict_where} DO UPDATE SET
                    world_rank_senior = excluded.world_rank_senior,
                    world_rank_u23 = excluded.world_rank_u23,
                    world_rank_u20 = excluded.world_rank_u20,
                    world_rank_u18 = excluded.world_rank_u18,
                    europe_rank_senior = excluded.europe_rank_senior,
                    europe_rank_u23 = excluded.europe_rank_u23,
                    europe_rank_u20 = excluded.europe_rank_u20,
                    europe_rank_u18 = excluded.europe_rank_u18
            """, [
                (raceathlete_id, scrape_date, scrape_week,
                 w.get('senior'), w.get('u23'), w.get('u20'), w.get('u18'),
                 eu.get('senior'), eu.get('u23'), eu.get('u20'), eu.get('u18'))
                for raceathlete_id, w, eu in international_updates
            ])

        # Commit (and save the scrape cache) after EVERY group, not just once
        # at the very end -- this scan can run for a long time (many
        # network requests), so if it crashes or gets interrupted partway
        # through, all groups processed so far are already safely saved
        # instead of being lost.
        conn.commit()
        cache.save()
        total_written += written

    print(f"Season {season_year}: wrote ranks for {total_written} results.", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--season', type=int, default=CURRENT_YEAR,
                         help='season year to process (default: current year)')
    parser.add_argument('--group-limit', type=int, default=None,
                         help='debug: only process the first N discipline/gender groups')
    parser.add_argument('--discipline', type=str, default=None,
                         help='debug: only process this discipline.name (e.g. "100m"), for a fast, '
                              'easy-to-hand-verify test slice instead of the whole season')
    parser.add_argument('--test', action='store_true',
                         help='rank athletics_test.db (see build_db.py --test) instead of the '
                              'real athletics.db, using a separate wa_ranking_test_cache.json')
    parser.add_argument('--test-week', type=int,
                         help='required with --test: which --test-week this ranking run '
                              'corresponds to (tags every rank_snapshot row written this run '
                              'with scrape_week=N, instead of a real scrape_date)')
    parser.add_argument('--scope', choices=('national', 'international', 'all'), default='all',
                         help='which ranks to compute/write this run (default: all). "national" is '
                              'cheap has.hr-vs-has.hr comparison, no network calls; "international" '
                              '(world/europe) needs many worldathletics.org requests per discipline '
                              '-- separate GitHub workflows use --scope national (weekly, alongside '
                              'build_db.py) and --scope international (its own, less frequent '
                              'schedule) instead of paying that cost every week. Restricting scope '
                              'only touches that scope\'s columns, both on raceathlete and in '
                              'rank_snapshot, so the two scopes can run independently without either '
                              'one wiping out what the other already wrote.')
    args = parser.parse_args()

    if args.test and args.test_week is None:
        parser.error('--test requires --test-week (tag this run\'s rank_snapshot rows with '
                      'which test week they belong to)')
    if args.test_week is not None and not args.test:
        parser.error('--test-week requires --test')

    db_path = TEST_DB_PATH if args.test else DB_PATH
    cache_path = TEST_CACHE_PATH if args.test else CACHE_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cache = Cache(cache_path)
    try:
        # Only bypass the cache (force_refresh=True) for the CURRENT season
        # -- past seasons are finished/final, so their WA toplist results
        # will never change, making the cache always safe to trust for
        # them. The current season is still in progress, so its lists keep
        # changing week to week and must always be re-fetched fresh.
        process_ranking(conn, cache, args.season, force_refresh=(args.season == CURRENT_YEAR),
                         group_limit=args.group_limit, discipline_name=args.discipline,
                         scrape_date=None if args.test else date.today().isoformat(),
                         scrape_week=args.test_week if args.test else None, scope=args.scope)
    finally:
        # try/finally guarantees the cache gets saved and the DB connection
        # closed even if process_ranking raises partway through (e.g. a
        # network error that exhausted all retries) -- so a crash mid-run
        # still preserves whatever scraping progress was made.
        cache.save()
        conn.close()


if __name__ == '__main__':
    main()
