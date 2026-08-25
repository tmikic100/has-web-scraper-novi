"""
Builds athletics.db from the parsed tablice_sezone/<year>/<file> season tables plus the
has.hr club directory (tablice_sezone/klubovi/page_N.html), using the normalized schema
(season/club/athlete/athlete_alias/clubathlete/discipline/wapoint/race/raceathlete).

Processes seasons newest -> oldest and stops as soon as a season contains no result for
club "AGR" -- those are the seasons that matter, and this is the boundary where AGR (as a
club code) stops existing in the historical data.

Incremental by default (see Loader.load_existing_state): an existing athletics.db is kept
and only added to, rather than wiped and rebuilt from that run's scrape alone. This matters
because has.hr's own season table pages only ever show each athlete's current season-best
per discipline -- a mark superseded by a new PB simply disappears from has.hr's page (and
downloader.py re-fetches the current year's file fresh every run, per its FORCE_REFRESH_YEARS).
Keeping the DB across runs is what lets earlier-in-the-season marks survive being superseded,
which is what makes rank progression across a season (see wa_ranking.py) meaningful instead
of only ever reflecting whatever's currently on top. Pass --fresh to wipe and rebuild anyway
(needed after a schema change, since only the fresh path runs SCHEMA_SQL).
"""

# io: lets us wrap stdout with a different text encoding (see below).
# re: regular expressions.
# sqlite3: Python's built-in SQLite database driver.
# sys: access to the running interpreter's stdout, argv, etc.
# unicodedata: Unicode character database -- used here to strip accents off
#              letters (č -> c) for building a case/accent-insensitive
#              search key.
import io
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

# On some setups (notably Windows consoles), Python's default stdout
# encoding isn't UTF-8, which would crash any `print()` of Croatian
# diacritics (č, ć, ž, š, đ). This detects that case and swaps stdout for a
# TextIOWrapper that always encodes as UTF-8, so printing accented names
# works regardless of the terminal's default codepage.
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Importing specific names from our own other modules in this project.
# Python runs the imported module's top-level code once (defining these
# functions/constants), then binds the names we asked for into this file's
# namespace -- after this, `parse_file(...)` here calls the exact same
# function object defined in parse_tables.py.
from parse_tables import parse_file, parse_clubs_page, normalize_mark, normalize_discipline_name
from wa_scoring import Scorer, HIGHER_IS_BETTER_COLUMNS, norm_col
from score_agr_2025 import resolve_column

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / 'tablice_sezone'
CLUBS_DIR = DATA_DIR / 'klubovi'
DB_PATH = BASE_DIR / 'athletics.db'

# --- Test mode (--test) ---------------------------------------------------
# A small, fully synthetic dataset used to try out/demonstrate the
# incremental-loading behavior above without touching the real
# tablice_sezone/athletics.db at all. Laid out to mirror the real thing:
#   test_data/tablice_sezone/klubovi/page_1.html  -- 3 fake clubs (AGR/TST/DEM)
#   test_data/tablice_sezone/2025/ssm25.html      -- one fixed snapshot, never
#                                                     changes (simulates a
#                                                     closed past season)
#   test_data/tablice_sezone/2026/ssm26.html      -- the "live" file that gets
#                                                     overwritten by --test-week
#   test_data/snapshots/week1_ssm26.html          -- source for week 1
#   test_data/snapshots/week2_ssm26.html          -- source for week 2 (some
#                                                     marks improved, one new
#                                                     athlete, one wind-aided
#                                                     mark, some unchanged --
#                                                     see the file itself)
# `--test-week N` copies the matching snapshot over 2026/ssm26.html before
# build() runs, then build() proceeds exactly as it would for real data --
# same incremental cache-based dedup, same AGR-boundary logic, nothing here
# is a special case in the loading code itself.
TEST_DATA_DIR = BASE_DIR / 'test_data' / 'tablice_sezone'
TEST_DB_PATH = BASE_DIR / 'athletics_test.db'
TEST_SNAPSHOTS_DIR = BASE_DIR / 'test_data' / 'snapshots'

# IND (unattached athletes) isn't a real club, so it doesn't appear in the has.hr club
# directory -- seed it explicitly.
PSEUDO_CLUBS = {
    'IND': 'Individualno',
}

# Genuinely age/equipment-adjusted variants already get distinct discipline *names* in the
# source (confirmed while normalizing discipline names -- see parse_tables.py), so a keyword
# match on the name is enough to classify scoring direction even for non-"standard" (youth)
# disciplines that have no official WA table entry and thus aren't covered by
# wa_scoring.HIGHER_IS_BETTER_COLUMNS.
# Matched against the *normalized* discipline name (Croatian side only, see
# normalize_discipline_name), not the raw bilingual source string.
# A tuple (parentheses) rather than a list is used here since this is a
# fixed, never-modified collection -- tuples signal "this won't change" the
# same way ALL_CAPS naming signals "this is a constant".
HIGHER_IS_BETTER_KEYWORDS = (
    'Dalj', 'Disk', 'Kladivo', 'Koplje', 'Kugla', 'Loptica', 'Motka', 'Troskok',
    'Vis', 'Vortex',
    'Desetoboj', 'Osmoboj', 'Petoboj', 'Sedmoboj', 'Četveroboj', 'Šestoboj', 'Troboj',
    '24 sata',
)

# Representative/national or regional teams show up with these prefixes in relay team names
# (confirmed against the current DB's distinct relay_team_name values, e.g. "REPREZENTACIJA
# HRVATSKE", "REPR. SJ.ZAP. HRVATSKE"); anything else (e.g. "AGRAM, ZAGREB") is a club team.
REPRESENTATIVE_TEAM_PREFIXES = ('REPREZENTACIJA', 'REPR.', 'REPREZ.')

# A triple-quoted string holding the entire database schema as raw SQL text.
# conn.executescript() (used down in build()) runs every statement in this
# string in order, in one call, which is how all 9 tables + 6 indexes below
# get created for a brand-new database.
SCHEMA_SQL = """
CREATE TABLE season (
    id   INTEGER PRIMARY KEY,
    year INTEGER UNIQUE NOT NULL
);

CREATE TABLE club (
    id         INTEGER PRIMARY KEY,
    name       TEXT,
    short_name TEXT UNIQUE NOT NULL,
    city       TEXT
);

CREATE TABLE athlete (
    id         INTEGER PRIMARY KEY,
    name_key   TEXT NOT NULL,
    name       TEXT NOT NULL,
    female     TINYINT(1),
    birth_year INTEGER,
    birthday   DATE,
    UNIQUE(name_key, birth_year)
);

CREATE TABLE athlete_alias (
    id           INTEGER PRIMARY KEY,
    athlete_id   INTEGER NOT NULL REFERENCES athlete(id),
    name_variant TEXT NOT NULL,
    source_year  INTEGER,
    UNIQUE(athlete_id, name_variant)
);

CREATE TABLE clubathlete (
    id         INTEGER PRIMARY KEY,
    athlete_id INTEGER NOT NULL REFERENCES athlete(id),
    club_id    INTEGER NOT NULL REFERENCES club(id),
    season_id  INTEGER NOT NULL REFERENCES season(id),
    UNIQUE(athlete_id, season_id)
);

CREATE TABLE discipline (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    short_name TEXT,
    info       TEXT,
    indoor     TINYINT(1) NOT NULL,
    female     TINYINT(1) NOT NULL,
    standard   TINYINT(1) NOT NULL DEFAULT 0,
    higher_is_better TINYINT(1) NOT NULL,
    UNIQUE(name, female, indoor)
);

CREATE TABLE wapoint (
    id            INTEGER PRIMARY KEY,
    discipline_id INTEGER NOT NULL REFERENCES discipline(id),
    mark          TEXT NOT NULL,
    mark_value    REAL NOT NULL,
    points        INTEGER NOT NULL,
    UNIQUE(discipline_id, mark_value)
);

CREATE TABLE race (
    id            INTEGER PRIMARY KEY,
    season_id     INTEGER NOT NULL REFERENCES season(id),
    discipline_id INTEGER NOT NULL REFERENCES discipline(id),
    wind          TEXT,
    wind_assisted TINYINT(1) NOT NULL DEFAULT 0,
    exclude_from_ranking TINYINT(1) NOT NULL DEFAULT 0,
    info          TEXT,
    mark          TEXT NOT NULL,
    mark_value    REAL,
    city          TEXT,
    date          DATE,
    wa_points     INTEGER,
    is_club_team  TINYINT(1) NOT NULL DEFAULT 1
);

CREATE TABLE raceathlete (
    id          INTEGER PRIMARY KEY,
    race_id     INTEGER NOT NULL REFERENCES race(id),
    athlete_id  INTEGER NOT NULL REFERENCES athlete(id),
    world_rank_senior  INTEGER,
    world_rank_u23     INTEGER,
    world_rank_u20     INTEGER,
    world_rank_u18     INTEGER,
    europe_rank_senior INTEGER,
    europe_rank_u23    INTEGER,
    europe_rank_u20    INTEGER,
    europe_rank_u18    INTEGER,
    national_rank_senior INTEGER,
    national_rank_u23    INTEGER,
    national_rank_u20    INTEGER,
    national_rank_u18    INTEGER,
    national_rank_u16    INTEGER,
    national_rank_u14    INTEGER,
    UNIQUE(race_id, athlete_id)
);

CREATE TABLE rank_snapshot (
    id             INTEGER PRIMARY KEY,
    raceathlete_id INTEGER NOT NULL REFERENCES raceathlete(id),
    scrape_date    DATE,       -- real wa_ranking.py runs: the calendar date it ran on
    scrape_week    INTEGER,    -- wa_ranking.py --test runs: the --test-week number
    world_rank_senior  INTEGER,
    world_rank_u23     INTEGER,
    world_rank_u20     INTEGER,
    world_rank_u18     INTEGER,
    europe_rank_senior INTEGER,
    europe_rank_u23    INTEGER,
    europe_rank_u20    INTEGER,
    europe_rank_u18    INTEGER,
    national_rank_senior INTEGER,
    national_rank_u23    INTEGER,
    national_rank_u20    INTEGER,
    national_rank_u18    INTEGER,
    national_rank_u16    INTEGER,
    national_rank_u14    INTEGER
);

CREATE INDEX idx_raceathlete_athlete ON raceathlete(athlete_id);
CREATE INDEX idx_race_season ON race(season_id);
CREATE INDEX idx_race_discipline ON race(discipline_id);
CREATE INDEX idx_clubathlete_athlete ON clubathlete(athlete_id);
CREATE INDEX idx_clubathlete_club ON clubathlete(club_id);
CREATE INDEX idx_wapoint_lookup ON wapoint(discipline_id, mark_value);
CREATE INDEX idx_rank_snapshot_raceathlete ON rank_snapshot(raceathlete_id);
-- Exactly one row per (raceathlete, real scrape date) -- a re-run on the same
-- calendar day overwrites (see wa_ranking.py's INSERT OR REPLACE) rather than
-- accumulating duplicates. Partial (WHERE ...) so this doesn't apply to the
-- --test rows, which are NULL here.
CREATE UNIQUE INDEX idx_rank_snapshot_real
    ON rank_snapshot(raceathlete_id, scrape_date) WHERE scrape_date IS NOT NULL;
-- Same idea for --test runs: one row per (raceathlete, test week).
CREATE UNIQUE INDEX idx_rank_snapshot_test
    ON rank_snapshot(raceathlete_id, scrape_week) WHERE scrape_week IS NOT NULL;
"""

# Run against EVERY build() call, fresh or incremental (unlike SCHEMA_SQL,
# which only runs once for a brand-new database) -- IF NOT EXISTS makes this
# safe to repeat forever: a no-op once the table/indexes are already there,
# but it's what lets a pre-existing athletics.db (built before rank_snapshot
# existed) pick up the new table without needing --fresh, which would
# otherwise defeat the whole point of incremental builds by wiping the race
# history --fresh is meant to be an escape hatch for, not a requirement for
# every future schema addition.
ENSURE_SQL = """
CREATE TABLE IF NOT EXISTS rank_snapshot (
    id             INTEGER PRIMARY KEY,
    raceathlete_id INTEGER NOT NULL REFERENCES raceathlete(id),
    scrape_date    DATE,
    scrape_week    INTEGER,
    world_rank_senior  INTEGER,
    world_rank_u23     INTEGER,
    world_rank_u20     INTEGER,
    world_rank_u18     INTEGER,
    europe_rank_senior INTEGER,
    europe_rank_u23    INTEGER,
    europe_rank_u20    INTEGER,
    europe_rank_u18    INTEGER,
    national_rank_senior INTEGER,
    national_rank_u23    INTEGER,
    national_rank_u20    INTEGER,
    national_rank_u18    INTEGER,
    national_rank_u16    INTEGER,
    national_rank_u14    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_rank_snapshot_raceathlete ON rank_snapshot(raceathlete_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rank_snapshot_real
    ON rank_snapshot(raceathlete_id, scrape_date) WHERE scrape_date IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_rank_snapshot_test
    ON rank_snapshot(raceathlete_id, scrape_week) WHERE scrape_week IS NOT NULL;
"""


def normalize_name_key(name):
    """Turns a name into an accent-insensitive, case-insensitive, whitespace-
    normalized "key" used to recognize the same athlete across different
    spellings/typos of their name, e.g. "Ivan Šimić" and "IVAN SIMIC" should
    both map to the same key."""
    # NFKD ("decomposed") Unicode normalization splits an accented letter
    # like "č" into two separate Unicode characters: the base letter "c" plus
    # a separate "combining caron" mark. unicodedata.combining(c) tells us
    # whether a character IS one of those combining marks, so the join()
    # below keeps only the base letters and drops every accent mark.
    decomposed = unicodedata.normalize('NFKD', name)
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', stripped).strip().upper()


def is_higher_is_better(discipline_name, wa_column):
    """True if a bigger mark means a better performance (throws, jumps,
    combined events) rather than a smaller one (times)."""
    if wa_column and norm_col(wa_column) in HIGHER_IS_BETTER_COLUMNS:
        return True
    # any(...) returns True as soon as one item in the generator expression
    # is truthy, without checking the rest -- here, "does any of these
    # keywords appear somewhere in the discipline name?"
    return any(kw in discipline_name for kw in HIGHER_IS_BETTER_KEYWORDS)


def is_club_team(relay_team_name):
    """False for national/regional representative teams (see
    REPRESENTATIVE_TEAM_PREFIXES); True for an actual club's own relay
    team, or when there's no team name to check at all."""
    if not relay_team_name:
        return True
    # str.startswith() can take a TUPLE of prefixes (not just one string)
    # and returns True if the string starts with ANY of them.
    return not relay_team_name.strip().upper().startswith(REPRESENTATIVE_TEAM_PREFIXES)


def format_mark(value, higher_is_better):
    """Reconstructs a display string for a wapoint mark from its parsed numeric value.
    Not used for scoring (mark_value is authoritative there) -- WA book's own text isn't
    retained by wa_scoring.py, so this is a display-only reconstruction."""
    if higher_is_better:
        # f'{value:.2f}' is an f-string with a "format spec": .2f means
        # "format this as a fixed-point decimal with exactly 2 digits after
        # the decimal point", e.g. 7.1 -> "7.10".
        return f'{value:.2f}'
    if value >= 60:
        # divmod(a, b) returns (a // b, a % b) in one call -- here, how many
        # whole minutes fit into `value` seconds, and the leftover seconds.
        minutes, seconds = divmod(value, 60)
        # `05.2f` means "pad with leading zeros to at least 5 characters
        # total (including the decimal point and 2 decimal digits)", so
        # 3.5 seconds prints as "03.50", not "3.5" or " 3.50".
        return f'{int(minutes)}:{seconds:05.2f}'
    return f'{value:.2f}'


class Loader:
    """Bundles the whole load process's mutable state (in-memory caches,
    the open DB connection, the WA-points Scorer) into one object, so the
    many `get_*_id`/`load_row` methods below can share that state via
    `self.` without a long chain of function arguments being threaded
    through every call."""

    def __init__(self, conn, clubs_dir=CLUBS_DIR):
        self.conn = conn
        self.clubs_dir = clubs_dir
        # Each of these caches maps some "natural key" (e.g. an athlete's
        # normalized name + birth year) to the numeric primary-key id
        # SQLite assigned that row -- so we only ever look a row up by its
        # real-world identity once per run, and after that just reuse the
        # cached id instead of re-querying the database every time.
        self.athlete_cache = {}
        self.club_cache = {}
        self.season_cache = {}
        self.discipline_cache = {}  # (name, female, indoor) -> (id, higher_is_better)
        self.clubathlete_seen = {}  # (athlete_id, season_id) -> club_id
        self.standard_populated = set()  # discipline_ids already given wapoint rows
        self.race_cache = {}  # physical-race identity -> race_id (see _race_key)
        self.new_race_count = 0  # races actually inserted this run (vs already-known)
        self.scorer = Scorer()

    def load_existing_state(self):
        """Populates every cache from a pre-existing DB instead of starting blank, so a
        re-run against an already-populated athletics.db is a true incremental update: rows
        that already exist (same natural key) are recognized and left alone, and only
        genuinely new ones (e.g. a new race has.hr appended to the current season's file
        since the last run) get inserted. Without this, get_athlete_id/get_discipline_id/
        get_season_id would blindly re-INSERT rows already present and crash on their UNIQUE
        constraints -- they were only ever safe because build() used to unlink() and recreate
        athletics.db from scratch on every run, discarding whatever it produced last time,
        including any results has.hr had since replaced with a newer season-best on its own
        site (has.hr's season table pages carry only the current best per athlete/discipline,
        confirmed empirically: >1 race per athlete/discipline/season is the ~1-2% exception
        even across full completed seasons -- see conversation). Keeping the DB and adding
        only what's new is what actually preserves that earlier history across weekly runs."""
        # Looping directly over `self.conn.execute(sql)` (instead of first
        # calling .fetchall()) iterates the query results one row at a time
        # -- each row unpacks straight into the loop variables here since
        # every SELECT below returns exactly as many columns as there are
        # loop variables. This is equivalent to fetchall() for these small
        # tables, just a slightly more memory-frugal habit for larger ones.
        for year, season_id in self.conn.execute('SELECT year, id FROM season'):
            self.season_cache[year] = season_id
        for name_key, birth_year, athlete_id in self.conn.execute(
                'SELECT name_key, birth_year, id FROM athlete'):
            self.athlete_cache[(name_key, birth_year)] = athlete_id
        for name, female, indoor, higher, discipline_id in self.conn.execute(
                'SELECT name, female, indoor, higher_is_better, id FROM discipline'):
            self.discipline_cache[(name, bool(female), bool(indoor))] = (discipline_id, bool(higher))
        for discipline_id, in self.conn.execute('SELECT id FROM discipline WHERE standard = 1'):
            self.standard_populated.add(discipline_id)
        for athlete_id, club_id, season_id in self.conn.execute(
                'SELECT athlete_id, club_id, season_id FROM clubathlete'):
            self.clubathlete_seen[(athlete_id, season_id)] = club_id
        # race_cache mirrors the two race_key shapes built in load_row: individual races are
        # keyed by (discipline_id, season_id, athlete_id, mark, date, city) via their single
        # raceathlete row (r.info IS NULL); relay races are keyed by (discipline_id, season_id,
        # relay_team_name, mark, date, city), with the team name stored in race.info.
        for discipline_id, season_id, athlete_id, mark, perf_date, city, race_id in self.conn.execute("""
                SELECT r.discipline_id, r.season_id, ra.athlete_id, r.mark, r.date, r.city, r.id
                FROM race r JOIN raceathlete ra ON ra.race_id = r.id
                WHERE r.info IS NULL"""):
            self.race_cache[(discipline_id, season_id, athlete_id, mark, perf_date, city)] = race_id
        for discipline_id, season_id, info, mark, perf_date, city, race_id in self.conn.execute("""
                SELECT discipline_id, season_id, info, mark, date, city, id
                FROM race WHERE info IS NOT NULL"""):
            self.race_cache[(discipline_id, season_id, info, mark, perf_date, city)] = race_id

    def get_season_id(self, year):
        """"Get or create" pattern used throughout this class: return the
        cached id if we've already seen this year, otherwise INSERT a new
        row and cache the id SQLite just assigned it (`cur.lastrowid`) for
        next time."""
        if year in self.season_cache:
            return self.season_cache[year]
        cur = self.conn.execute('INSERT INTO season(year) VALUES (?)', (year,))
        self.season_cache[year] = cur.lastrowid
        return cur.lastrowid

    def seed_clubs(self):
        """Populates the `club` table from the scraped has.hr club directory
        pages, plus the hardcoded PSEUDO_CLUBS. Runs every time (not just for
        a fresh DB) -- `INSERT OR IGNORE` makes re-running this against
        clubs already in the DB a harmless no-op."""
        self.known_club_codes = set()
        # Path.glob('page_*.html') finds every file matching that wildcard
        # pattern in CLUBS_DIR. sorted(..., key=...) with a lambda sorts
        # them numerically by the page number embedded in the filename
        # (p.stem is the filename without its extension, e.g. "page_3";
        # .split('_')[1] pulls out "3"), rather than alphabetically (which
        # would wrongly put "page_10" before "page_2").
        for page in sorted(self.clubs_dir.glob('page_*.html'), key=lambda p: int(p.stem.split('_')[1])):
            for code, name, city in parse_clubs_page(page):
                if code in self.known_club_codes:
                    continue
                self.known_club_codes.add(code)
                self.conn.execute(
                    'INSERT OR IGNORE INTO club(short_name, name, city) VALUES (?, ?, ?)',
                    (code, name, city),
                )
        for code, name in PSEUDO_CLUBS.items():
            self.known_club_codes.add(code)
            self.conn.execute(
                'INSERT OR IGNORE INTO club(short_name, name, city) VALUES (?, ?, NULL)',
                (code, name),
            )

    def resolve_club_code(self, code):
        # any code not in the current has.hr club directory (defunct/renamed clubs, or
        # parser artifacts like a stray city name) is treated as unattached, per instruction:
        # only clubs actually on the scraped list count as "standard" clubs.
        if not code or code not in self.known_club_codes:
            return 'IND'
        return code

    def get_club_id(self, code):
        if code in self.club_cache:
            return self.club_cache[code]
        cur = self.conn.execute('SELECT id FROM club WHERE short_name = ?', (code,))
        club_id = cur.fetchone()[0]
        self.club_cache[code] = club_id
        return club_id

    def get_athlete_id(self, name, birth_year, birth_date, gender, year):
        """`gender` is None for mixed-relay legs (row.gender == 'X' at the race
        level doesn't tell us any individual leg runner's own gender) --
        unlike individual races and single-gender relays, where it's always
        reliably 'M'/'F'. Coercing that unknown case to int(None == 'F') == 0
        used to silently record every such athlete as male; instead `female`
        is left NULL until a reliable (non-mixed-relay) sighting resolves it,
        here or in a later/earlier season -- see conversation for a concrete
        case (a female athlete first seen as a mixed-relay leg)."""
        name_key = normalize_name_key(name)
        cache_key = (name_key, birth_year)
        athlete_id = self.athlete_cache.get(cache_key)
        # `int(gender == 'F')` converts the boolean comparison into 1 or 0
        # (True -> 1, False -> 0) for storage as a TINYINT column -- but only
        # when gender is known at all; otherwise female stays Python's None,
        # which sqlite3 stores as SQL NULL.
        female = None if gender is None else int(gender == 'F')
        if athlete_id is None:
            # First time we've ever seen this (name_key, birth_year) pair
            # (in this run OR, thanks to load_existing_state, in any prior
            # run) -- insert a brand new athlete row.
            cur = self.conn.execute(
                'INSERT INTO athlete(name_key, name, female, birth_year, birthday) '
                'VALUES (?, ?, ?, ?, ?)',
                (name_key, name, female, birth_year, birth_date),
            )
            athlete_id = cur.lastrowid
            self.athlete_cache[cache_key] = athlete_id
        else:
            # Already know this athlete -- opportunistically fill in details
            # we might not have had before. Both UPDATEs are deliberately
            # scoped with "AND ... IS NULL" so they only ever fill in a gap,
            # never overwrite a previously-confirmed value with a worse one.
            if birth_date:
                self.conn.execute(
                    'UPDATE athlete SET birthday = ?, name = ? WHERE id = ? AND birthday IS NULL',
                    (birth_date, name, athlete_id),
                )
            if female is not None:
                self.conn.execute(
                    'UPDATE athlete SET female = ? WHERE id = ? AND female IS NULL',
                    (female, athlete_id),
                )
        # Record this exact spelling of the athlete's name as a known alias
        # (e.g. a maiden name, or a typo has.hr later fixed), so search can
        # still find them by an old spelling too. OR IGNORE means "if this
        # exact (athlete_id, name_variant) pair is already recorded, just
        # silently do nothing instead of erroring on the UNIQUE constraint."
        self.conn.execute(
            'INSERT OR IGNORE INTO athlete_alias(athlete_id, name_variant, source_year) '
            'VALUES (?, ?, ?)',
            (athlete_id, name, year),
        )
        return athlete_id

    def note_club_membership(self, athlete_id, club_id, season_id, year, name):
        """Records that an athlete competed for a given club in a given
        season -- but an athlete should only belong to ONE club per season,
        so this also detects (and warns about, rather than silently
        overwriting) the rare case where the source data disagrees with
        itself about which club someone was on."""
        key = (athlete_id, season_id)
        prev = self.clubathlete_seen.get(key)
        if prev is None:
            self.clubathlete_seen[key] = club_id
            self.conn.execute(
                'INSERT INTO clubathlete(athlete_id, club_id, season_id) VALUES (?, ?, ?)',
                (athlete_id, club_id, season_id),
            )
        elif prev != club_id:
            print(f"  WARN {name} ({year}): conflicting club codes in same season, "
                  f"keeping first seen")

    def get_discipline_id(self, raw_name, age_group_code, gender, indoor):
        name = normalize_discipline_name(raw_name, age_group_code)
        female = gender == 'F'
        key = (name, female, indoor)
        entry = self.discipline_cache.get(key)
        if entry is None:
            higher = is_higher_is_better(name, None)
            cur = self.conn.execute(
                'INSERT INTO discipline(name, indoor, female, standard, higher_is_better) '
                'VALUES (?, ?, ?, 0, ?)',
                (name, int(indoor), int(female), int(higher)),
            )
            discipline_id = cur.lastrowid
            entry = (discipline_id, higher)
            self.discipline_cache[key] = entry
        # Only the id and the (possibly re-derived) canonical name are
        # actually useful to callers -- entry[1] (higher_is_better) is kept
        # in the cache tuple for completeness but isn't returned here, since
        # every caller so far only ever needed the id/name pair.
        return entry[0], name

    def maybe_populate_wapoint(self, discipline_id, wa_column, gender):
        """Lazily fills in the wapoint (mark -> WA points) lookup table for
        one discipline, the first time that discipline is seen with a known
        WA scoring column -- there's no need to do this per-row, since the
        table is the same for every row of the same discipline/gender."""
        if discipline_id in self.standard_populated:
            return
        column = norm_col(wa_column)
        table = self.scorer.tables.get(gender, {}).get(column)
        if not table:
            return
        self.standard_populated.add(discipline_id)
        self.conn.execute('UPDATE discipline SET standard = 1 WHERE id = ?', (discipline_id,))
        higher = column in HIGHER_IS_BETTER_COLUMNS
        # A list comprehension building one tuple per (mark_value, points)
        # entry in the WA table, ready to hand to executemany() below.
        # dict.items() iterates (key, value) pairs together.
        rows = [
            (discipline_id, format_mark(value, higher), value, points)
            for value, points in table.items()
        ]
        # executemany runs the same SQL statement once per tuple in `rows`,
        # much faster than looping in Python and calling .execute() one row
        # at a time (fewer round-trips between Python and SQLite).
        self.conn.executemany(
            'INSERT OR IGNORE INTO wapoint(discipline_id, mark, mark_value, points) '
            'VALUES (?, ?, ?, ?)',
            rows,
        )

    def load_row(self, row):
        """Same physical result often gets re-published once per age-group file the
        participant(s) qualify for (confirmed against the raw data: identical mark/date/
        location repeated verbatim across e.g. dd/jj/mj/ss files for one performance) -- race
        is a physical event, not an age-group listing, so a re-listing must reuse the existing
        race row. raceathlete no longer stores anything that varies per age-group listing
        (agegroup_id/rank were dropped -- ranking is now derived from birth year, not has.hr's
        own category), so `INSERT OR IGNORE INTO raceathlete(race_id, athlete_id)` naturally
        collapses re-listings into the single existing row via UNIQUE(race_id, athlete_id)."""
        season_id = self.get_season_id(row.year)
        discipline_id, disc_name = self.get_discipline_id(
            row.discipline, row.age_group_code, row.gender, row.indoor,
        )
        mark_value = normalize_mark(row.mark_raw)

        wa_column = resolve_column(row.discipline, row.gender, row.age_group_code, row.indoor)
        wa_points = None
        if wa_column:
            self.maybe_populate_wapoint(discipline_id, wa_column, row.gender)
            if mark_value is not None:
                wa_points = self.scorer.score(row.gender, wa_column, mark_value)

        if not row.is_relay:
            # --- Individual result branch ---
            club_code = self.resolve_club_code(row.club_code)
            club_id = self.get_club_id(club_code)
            athlete_id = self.get_athlete_id(
                row.name, row.birth_year, row.birth_date, row.gender, row.year,
            )
            self.note_club_membership(athlete_id, club_id, season_id, row.year, row.name)

            # This tuple is the "natural key" identifying one physical
            # individual race -- see load_existing_state's matching cache
            # population and the module docstring's note on why this is
            # what makes incremental updates (and re-listing dedup) work.
            race_key = (discipline_id, season_id, athlete_id, row.mark_raw, row.perf_date,
                        row.location)
            race_id = self.race_cache.get(race_key)
            if race_id is None:
                cur = self.conn.execute(
                    'INSERT INTO race(season_id, discipline_id, wind, wind_assisted, '
                    'exclude_from_ranking, mark, mark_value, city, date, wa_points) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?)',
                    (season_id, discipline_id, row.wind, int(row.wind_assisted),
                     int(row.exclude_from_ranking), row.mark_raw, mark_value, row.location,
                     row.perf_date, wa_points),
                )
                race_id = cur.lastrowid
                self.race_cache[race_key] = race_id
                self.new_race_count += 1
            self.conn.execute(
                'INSERT OR IGNORE INTO raceathlete(race_id, athlete_id) VALUES (?, ?)',
                (race_id, athlete_id),
            )
        else:
            # --- Relay result branch ---
            race_key = (discipline_id, season_id, row.relay_team_name, row.mark_raw,
                        row.perf_date, row.location)
            race_id = self.race_cache.get(race_key)
            if race_id is None:
                club_team = is_club_team(row.relay_team_name)
                cur = self.conn.execute(
                    'INSERT INTO race(season_id, discipline_id, wind, wind_assisted, '
                    'exclude_from_ranking, info, mark, mark_value, city, date, wa_points, '
                    'is_club_team) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (season_id, discipline_id, row.wind, int(row.wind_assisted),
                     int(row.exclude_from_ranking), row.relay_team_name, row.mark_raw,
                     mark_value, row.location, row.perf_date, wa_points, int(club_team)),
                )
                race_id = cur.lastrowid
                self.race_cache[race_key] = race_id
                self.new_race_count += 1
            # Link every leg runner of this relay to the same race row --
            # this is *why* a relay race can have multiple raceathlete rows
            # (one per leg) where an individual race only ever has one.
            for leg_name, leg_birth_year in row.relay_legs:
                leg_gender = row.gender if row.gender != 'X' else None
                leg_athlete_id = self.get_athlete_id(
                    leg_name, leg_birth_year, None, leg_gender, row.year,
                )
                self.conn.execute(
                    'INSERT OR IGNORE INTO raceathlete(race_id, athlete_id) VALUES (?, ?)',
                    (race_id, leg_athlete_id),
                )


def season_files(year_dir):
    """Returns every season-table file inside one year's folder, sorted by
    filename. A generator expression `(p for p in ... if ...)` inside
    sorted() filters year_dir's contents down to just .html/.htm/.txt files
    before sorting them."""
    return sorted(
        p for p in year_dir.iterdir()
        if p.suffix.lower() in ('.html', '.htm', '.txt')
    )


def build(fresh=False, data_dir=DATA_DIR, db_path=DB_PATH):
    """Incremental by default: an existing athletics.db is kept and only added to, so a
    result has.hr later drops from its own season table (superseded by a new season-best)
    stays in ours -- see Loader.load_existing_state. Pass fresh=True (or --fresh on the CLI)
    to wipe and rebuild from scratch instead, e.g. after a schema change: an incremental run
    against a DB from an older schema version will fail (missing/renamed columns), since only
    the fresh path runs SCHEMA_SQL.

    data_dir/db_path default to the real tablice_sezone/athletics.db, but --test (see main())
    points these at the small synthetic fixture set under test_data/ and athletics_test.db
    instead -- everything below runs identically either way, since it never references the
    module-level DATA_DIR/DB_PATH/CLUBS_DIR constants directly once inside this function."""
    if fresh and db_path.exists():
        db_path.unlink()  # delete the file
    is_new_db = not db_path.exists()
    # sqlite3.connect() creates the file automatically if it doesn't exist
    # yet, so this is safe to call whether or not we just deleted it above.
    conn = sqlite3.connect(db_path)
    if is_new_db:
        conn.executescript(SCHEMA_SQL)
    conn.executescript(ENSURE_SQL)
    loader = Loader(conn, clubs_dir=data_dir / 'klubovi')
    if not is_new_db:
        loader.load_existing_state()
    loader.seed_clubs()

    # data_dir.iterdir() lists every entry (files and folders) directly
    # inside tablice_sezone/; the generator expression filters that down to
    # just the year subfolders (excluding the "klubovi" folder), and
    # sorted(..., reverse=True) orders them newest-year-first.
    year_dirs = sorted(
        (p for p in data_dir.iterdir() if p.is_dir() and p.name != 'klubovi'),
        key=lambda p: int(p.name),
        reverse=True,
    )

    total_rows = 0
    for year_dir in year_dirs:
        year = int(year_dir.name)
        year_parsed = []
        for path in season_files(year_dir):
            try:
                # Append a (path, list_of_parsed_rows) pair for this file --
                # kept together as a list of tuples so the AGR-boundary
                # check below can inspect all of this year's rows before we
                # commit to actually loading any of them into the DB.
                year_parsed.append((path, parse_file(path)))
            except ValueError as e:
                print(f"  WARN skip {path.name}: {e}")

        # any(...) with a nested generator expression: walk every parsed
        # row of every file for this year, and stop as soon as we find one
        # individual (non-relay) result whose club code is "AGR". This is a
        # compact way of writing what would otherwise be a small nested
        # for-loop with a flag variable.
        has_agr = any(
            row.club_code == 'AGR'
            for _path, rows in year_parsed
            for row in rows
            if not row.is_relay
        )
        if not has_agr:
            print(f"Stopping at {year}: no AGR results found this season.")
            break

        year_row_count = 0
        for path, rows in year_parsed:
            for row in rows:
                loader.load_row(row)
                year_row_count += 1
        total_rows += year_row_count
        print(f"{year}: loaded {year_row_count} rows from {len(year_parsed)} files")

    # Nothing is actually saved to disk until commit() is called -- SQLite
    # (like most databases) batches changes into a transaction, and this is
    # what makes them durable. Committing once at the very end (rather than
    # after every single insert) is both much faster and means a mid-run
    # crash leaves the previous state untouched instead of a half-loaded DB.
    conn.commit()

    print("\n--- Summary ---")
    for table in ('season', 'club', 'athlete', 'athlete_alias', 'clubathlete',
                  'discipline', 'wapoint', 'race', 'raceathlete', 'rank_snapshot'):
        count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        print(f"{table}: {count}")
    print(f"total rows scanned: {total_rows}")
    print(f"new races inserted this run: {loader.new_race_count}")

    conn.close()


if __name__ == '__main__':
    # argparse: standard library module for parsing command-line arguments
    # (e.g. `python build_db.py --fresh`) into a simple namespace object.
    import argparse
    import shutil
    parser = argparse.ArgumentParser()
    # action='store_true' means "this is a flag, not a value-taking option"
    # -- args.fresh is False unless --fresh was actually passed on the
    # command line, in which case it becomes True.
    parser.add_argument('--fresh', action='store_true',
                         help='wipe athletics.db and rebuild from scratch instead of the '
                              'default incremental update (needed after a schema change)')
    parser.add_argument('--test', action='store_true',
                         help='build athletics_test.db from the small synthetic fixture set '
                              'under test_data/ instead of the real tablice_sezone/athletics.db '
                              '-- see the TEST_* constants above for the fixture layout')
    parser.add_argument('--test-week', type=int,
                         help='only valid with --test: copies test_data/snapshots/week{N}_ssm26.html '
                              'over test_data/tablice_sezone/2026/ssm26.html before building, to '
                              'simulate that week\'s has.hr scrape -- any N with a matching snapshot '
                              'file works (not just 1/2), so you can add week3_ssm26.html, '
                              'week4_ssm26.html, etc. yourself and just keep incrementing N. Run '
                              '--test-list-weeks to see what snapshots currently exist')
    parser.add_argument('--test-list-weeks', action='store_true',
                         help='list available test_data/snapshots/week{N}_ssm26.html files and exit')
    args = parser.parse_args()

    if args.test_list_weeks:
        # Path.glob('week*_ssm26.html') finds every snapshot file regardless
        # of N; re.findall pulls the number back out of each filename so we
        # can list them in numeric (not alphabetical -- "week10" would
        # otherwise sort before "week2") order.
        weeks = sorted(
            int(re.findall(r'\d+', p.stem)[0])
            for p in TEST_SNAPSHOTS_DIR.glob('week*_ssm26.html')
        )
        print(f"Available test weeks: {weeks}" if weeks else "No test-week snapshots found.")
        raise SystemExit

    if args.test_week is not None and not args.test:
        parser.error('--test-week requires --test')

    if args.test:
        if args.test_week is not None:
            src = TEST_SNAPSHOTS_DIR / f'week{args.test_week}_ssm26.html'
            if not src.exists():
                parser.error(
                    f"no snapshot file at {src} -- run --test-list-weeks to see what's available, "
                    f"or add this file yourself (same format as the other week*_ssm26.html files) "
                    f"to define a new week"
                )
            (TEST_DATA_DIR / '2026').mkdir(parents=True, exist_ok=True)
            dst = TEST_DATA_DIR / '2026' / 'ssm26.html'
            shutil.copyfile(src, dst)
            print(f"[test] copied {src.name} -> {dst}")
        build(fresh=args.fresh, data_dir=TEST_DATA_DIR, db_path=TEST_DB_PATH)
    else:
        build(fresh=args.fresh)
