"""
Builds athletics.db from the parsed tablice_sezone/<year>/<file> season tables plus the
has.hr club directory (tablice_sezone/klubovi/page_N.html), using the normalized schema
(season/agegroup/club/athlete/athlete_alias/clubathlete/discipline/wapoint/race/raceathlete).

Processes seasons newest -> oldest and stops as soon as a season contains no result for
club "AGR" -- those are the seasons that matter, and this is the boundary where AGR (as a
club code) stops existing in the historical data.
"""

import io
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from parse_tables import parse_file, parse_clubs_page, normalize_mark, normalize_discipline_name
from wa_scoring import Scorer, HIGHER_IS_BETTER_COLUMNS, norm_col
from score_agr_2025 import resolve_column

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / 'tablice_sezone'
CLUBS_DIR = DATA_DIR / 'klubovi'
DB_PATH = BASE_DIR / 'athletics.db'

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

SCHEMA_SQL = """
CREATE TABLE season (
    id   INTEGER PRIMARY KEY,
    year INTEGER UNIQUE NOT NULL
);

CREATE TABLE agegroup (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    short_name TEXT,
    tech_name  TEXT UNIQUE NOT NULL
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
    female     TINYINT(1) NOT NULL,
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
    agegroup_id INTEGER NOT NULL REFERENCES agegroup(id),
    rank        INTEGER,
    season_best TINYINT(1) NOT NULL DEFAULT 0,
    UNIQUE(race_id, athlete_id, agegroup_id)
);

CREATE INDEX idx_raceathlete_athlete ON raceathlete(athlete_id);
CREATE INDEX idx_raceathlete_agegroup ON raceathlete(agegroup_id);
CREATE INDEX idx_race_season ON race(season_id);
CREATE INDEX idx_race_discipline ON race(discipline_id);
CREATE INDEX idx_clubathlete_athlete ON clubathlete(athlete_id);
CREATE INDEX idx_clubathlete_club ON clubathlete(club_id);
CREATE INDEX idx_wapoint_lookup ON wapoint(discipline_id, mark_value);
"""


def normalize_name_key(name):
    decomposed = unicodedata.normalize('NFKD', name)
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', stripped).strip().upper()


def is_higher_is_better(discipline_name, wa_column):
    if wa_column and norm_col(wa_column) in HIGHER_IS_BETTER_COLUMNS:
        return True
    return any(kw in discipline_name for kw in HIGHER_IS_BETTER_KEYWORDS)


def is_club_team(relay_team_name):
    if not relay_team_name:
        return True
    return not relay_team_name.strip().upper().startswith(REPRESENTATIVE_TEAM_PREFIXES)


def format_mark(value, higher_is_better):
    """Reconstructs a display string for a wapoint mark from its parsed numeric value.
    Not used for scoring (mark_value is authoritative there) -- WA book's own text isn't
    retained by wa_scoring.py, so this is a display-only reconstruction."""
    if higher_is_better:
        return f'{value:.2f}'
    if value >= 60:
        minutes, seconds = divmod(value, 60)
        return f'{int(minutes)}:{seconds:05.2f}'
    return f'{value:.2f}'


class Loader:
    def __init__(self, conn):
        self.conn = conn
        self.athlete_cache = {}
        self.club_cache = {}
        self.season_cache = {}
        self.agegroup_cache = {}
        self.discipline_cache = {}  # (name, female, indoor) -> (id, higher_is_better)
        self.clubathlete_seen = {}  # (athlete_id, season_id) -> club_id
        self.standard_populated = set()  # discipline_ids already given wapoint rows
        self.race_cache = {}  # physical-race identity -> race_id (see _race_key)
        self.scorer = Scorer()

    def get_season_id(self, year):
        if year in self.season_cache:
            return self.season_cache[year]
        cur = self.conn.execute('INSERT INTO season(year) VALUES (?)', (year,))
        self.season_cache[year] = cur.lastrowid
        return cur.lastrowid

    def get_agegroup_id(self, code, name):
        if code in self.agegroup_cache:
            return self.agegroup_cache[code]
        cur = self.conn.execute(
            'INSERT INTO agegroup(name, tech_name) VALUES (?, ?)', (name, code),
        )
        self.agegroup_cache[code] = cur.lastrowid
        return cur.lastrowid

    def seed_clubs(self):
        self.known_club_codes = set()
        for page in sorted(CLUBS_DIR.glob('page_*.html'), key=lambda p: int(p.stem.split('_')[1])):
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
        name_key = normalize_name_key(name)
        cache_key = (name_key, birth_year)
        athlete_id = self.athlete_cache.get(cache_key)
        if athlete_id is None:
            cur = self.conn.execute(
                'INSERT INTO athlete(name_key, name, female, birth_year, birthday) '
                'VALUES (?, ?, ?, ?, ?)',
                (name_key, name, int(gender == 'F'), birth_year, birth_date),
            )
            athlete_id = cur.lastrowid
            self.athlete_cache[cache_key] = athlete_id
        elif birth_date:
            self.conn.execute(
                'UPDATE athlete SET birthday = ?, name = ? WHERE id = ? AND birthday IS NULL',
                (birth_date, name, athlete_id),
            )
        self.conn.execute(
            'INSERT OR IGNORE INTO athlete_alias(athlete_id, name_variant, source_year) '
            'VALUES (?, ?, ?)',
            (athlete_id, name, year),
        )
        return athlete_id

    def note_club_membership(self, athlete_id, club_id, season_id, year, name):
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
        return entry[0], name

    def maybe_populate_wapoint(self, discipline_id, wa_column, gender):
        if discipline_id in self.standard_populated:
            return
        column = norm_col(wa_column)
        table = self.scorer.tables.get(gender, {}).get(column)
        if not table:
            return
        self.standard_populated.add(discipline_id)
        self.conn.execute('UPDATE discipline SET standard = 1 WHERE id = ?', (discipline_id,))
        higher = column in HIGHER_IS_BETTER_COLUMNS
        rows = [
            (discipline_id, format_mark(value, higher), value, points)
            for value, points in table.items()
        ]
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
        race row and just add another raceathlete row (this file's own agegroup + rank),
        rather than create a duplicate race."""
        season_id = self.get_season_id(row.year)
        agegroup_id = self.get_agegroup_id(row.age_group_code, row.age_group_name)
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
            club_code = self.resolve_club_code(row.club_code)
            club_id = self.get_club_id(club_code)
            athlete_id = self.get_athlete_id(
                row.name, row.birth_year, row.birth_date, row.gender, row.year,
            )
            self.note_club_membership(athlete_id, club_id, season_id, row.year, row.name)

            race_key = (discipline_id, season_id, athlete_id, row.mark_raw, row.perf_date,
                        row.location)
            race_id = self.race_cache.get(race_key)
            if race_id is None:
                cur = self.conn.execute(
                    'INSERT INTO race(season_id, discipline_id, wind, wind_assisted, mark, '
                    'mark_value, city, date, wa_points) VALUES (?,?,?,?,?,?,?,?,?)',
                    (season_id, discipline_id, row.wind, int(row.wind_assisted), row.mark_raw,
                     mark_value, row.location, row.perf_date, wa_points),
                )
                race_id = cur.lastrowid
                self.race_cache[race_key] = race_id
            self.conn.execute(
                'INSERT OR IGNORE INTO raceathlete(race_id, athlete_id, agegroup_id, rank) '
                'VALUES (?, ?, ?, ?)',
                (race_id, athlete_id, agegroup_id, row.rank),
            )
        else:
            race_key = (discipline_id, season_id, row.relay_team_name, row.mark_raw,
                        row.perf_date, row.location)
            race_id = self.race_cache.get(race_key)
            if race_id is None:
                club_team = is_club_team(row.relay_team_name)
                cur = self.conn.execute(
                    'INSERT INTO race(season_id, discipline_id, wind, wind_assisted, info, '
                    'mark, mark_value, city, date, wa_points, is_club_team) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (season_id, discipline_id, row.wind, int(row.wind_assisted),
                     row.relay_team_name, row.mark_raw, mark_value, row.location,
                     row.perf_date, wa_points, int(club_team)),
                )
                race_id = cur.lastrowid
                self.race_cache[race_key] = race_id
            for leg_name, leg_birth_year in row.relay_legs:
                leg_gender = row.gender if row.gender != 'X' else None
                leg_athlete_id = self.get_athlete_id(
                    leg_name, leg_birth_year, None, leg_gender, row.year,
                )
                self.conn.execute(
                    'INSERT OR IGNORE INTO raceathlete(race_id, athlete_id, agegroup_id, rank) '
                    'VALUES (?, ?, ?, ?)',
                    (race_id, leg_athlete_id, agegroup_id, row.rank),
                )

    def compute_season_best(self):
        """season_best lives on raceathlete, not race: a relay race is shared by multiple
        athletes, and a given race can legitimately be one teammate's personal season-best
        while not being another's (confirmed against the real data -- a relay leg who only
        ran one race that season trivially has it as their best, while a teammate with faster
        splits elsewhere does not)."""
        rows = self.conn.execute("""
            SELECT ra.athlete_id, r.discipline_id, r.season_id, r.id, r.mark_value,
                   d.higher_is_better
            FROM raceathlete ra
            JOIN race r ON r.id = ra.race_id
            JOIN discipline d ON d.id = r.discipline_id
            WHERE r.mark_value IS NOT NULL
        """).fetchall()

        best = {}  # (athlete_id, discipline_id, season_id) -> (race_id, mark_value)
        for athlete_id, discipline_id, season_id, race_id, mark_value, higher in rows:
            key = (athlete_id, discipline_id, season_id)
            current = best.get(key)
            if current is None:
                best[key] = (race_id, mark_value)
                continue
            _, best_value = current
            better = mark_value > best_value if higher else mark_value < best_value
            if better:
                best[key] = (race_id, mark_value)

        self.conn.executemany(
            'UPDATE raceathlete SET season_best = 1 WHERE athlete_id = ? AND race_id = ?',
            [(athlete_id, race_id) for (athlete_id, _, _), (race_id, _) in best.items()],
        )


def season_files(year_dir):
    return sorted(
        p for p in year_dir.iterdir()
        if p.suffix.lower() in ('.html', '.htm', '.txt')
    )


def build():
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA_SQL)
    loader = Loader(conn)
    loader.seed_clubs()

    year_dirs = sorted(
        (p for p in DATA_DIR.iterdir() if p.is_dir() and p.name != 'klubovi'),
        key=lambda p: int(p.name),
        reverse=True,
    )

    total_rows = 0
    for year_dir in year_dirs:
        year = int(year_dir.name)
        year_parsed = []
        for path in season_files(year_dir):
            try:
                year_parsed.append((path, parse_file(path)))
            except ValueError as e:
                print(f"  WARN skip {path.name}: {e}")

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

    print("Computing season-best flags...")
    loader.compute_season_best()

    conn.commit()

    print("\n--- Summary ---")
    for table in ('season', 'agegroup', 'club', 'athlete', 'athlete_alias', 'clubathlete',
                  'discipline', 'wapoint', 'race', 'raceathlete'):
        count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        print(f"{table}: {count}")
    print(f"total rows loaded: {total_rows}")

    conn.close()


if __name__ == '__main__':
    build()
