"""
Builds athletics.db from the parsed tablice_sezone/<year>/<file> season tables.

Processes seasons newest -> oldest and stops as soon as a season contains no result for
club "AGR" -- those are the seasons that matter, and this is the boundary where AGR (as a
club code) stops existing in the historical data.
"""

import re
import sqlite3
import unicodedata
from pathlib import Path

from parse_tables import parse_file, normalize_mark

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / 'tablice_sezone'
DB_PATH = BASE_DIR / 'athletics.db'

SCHEMA_SQL = """
CREATE TABLE athletes (
    id INTEGER PRIMARY KEY,
    name_key TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    birth_year INTEGER,
    birth_date TEXT,
    gender TEXT,
    UNIQUE(name_key, birth_year)
);

CREATE TABLE athlete_aliases (
    id INTEGER PRIMARY KEY,
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    name_variant TEXT NOT NULL,
    source_year INTEGER,
    UNIQUE(athlete_id, name_variant)
);

CREATE TABLE clubs (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL
);

CREATE TABLE seasons (
    id INTEGER PRIMARY KEY,
    year INTEGER UNIQUE NOT NULL
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    age_group_code TEXT NOT NULL,
    age_group_name TEXT NOT NULL,
    gender TEXT NOT NULL,
    discipline_name TEXT NOT NULL,
    UNIQUE(age_group_code, gender, discipline_name)
);

CREATE TABLE results (
    id INTEGER PRIMARY KEY,
    athlete_id INTEGER REFERENCES athletes(id),
    event_id INTEGER NOT NULL REFERENCES events(id),
    season_id INTEGER NOT NULL REFERENCES seasons(id),
    indoor INTEGER NOT NULL,
    rank INTEGER,
    mark_raw TEXT NOT NULL,
    mark_value REAL,
    wind TEXT,
    wind_assisted INTEGER NOT NULL DEFAULT 0,
    club_id INTEGER REFERENCES clubs(id),
    location TEXT,
    perf_date TEXT,
    is_relay INTEGER NOT NULL DEFAULT 0,
    relay_team_name TEXT,
    source_file TEXT NOT NULL
);

CREATE TABLE relay_legs (
    id INTEGER PRIMARY KEY,
    result_id INTEGER NOT NULL REFERENCES results(id),
    athlete_id INTEGER NOT NULL REFERENCES athletes(id),
    leg_order INTEGER,
    birth_year INTEGER
);

CREATE INDEX idx_results_athlete ON results(athlete_id);
CREATE INDEX idx_results_event ON results(event_id);
CREATE INDEX idx_results_season ON results(season_id);
CREATE INDEX idx_relay_legs_athlete ON relay_legs(athlete_id);

CREATE VIEW v_athlete_results AS
SELECT
    r.id AS result_id, a.id AS athlete_id, a.canonical_name, a.birth_year, a.birth_date,
    s.year, r.indoor, ev.age_group_code, ev.age_group_name, ev.gender, ev.discipline_name,
    r.rank, r.mark_raw, r.mark_value, r.wind, r.wind_assisted,
    c.code AS club_code, r.location, r.perf_date, r.source_file
FROM results r
JOIN athletes a ON a.id = r.athlete_id
JOIN events ev ON ev.id = r.event_id
JOIN seasons s ON s.id = r.season_id
LEFT JOIN clubs c ON c.id = r.club_id
WHERE r.is_relay = 0;

CREATE VIEW v_athlete_relay_results AS
SELECT
    r.id AS result_id, a.id AS athlete_id, a.canonical_name, a.birth_year, a.birth_date,
    s.year, r.indoor, ev.age_group_code, ev.age_group_name, ev.gender, ev.discipline_name,
    NULL AS rank, r.mark_raw, r.mark_value, NULL AS wind, 0 AS wind_assisted,
    c.code AS club_code, r.location, r.perf_date, r.source_file,
    r.relay_team_name, rl.leg_order
FROM relay_legs rl
JOIN results r ON r.id = rl.result_id
JOIN athletes a ON a.id = rl.athlete_id
JOIN events ev ON ev.id = r.event_id
JOIN seasons s ON s.id = r.season_id
LEFT JOIN clubs c ON c.id = r.club_id;

CREATE VIEW v_athlete_career AS
SELECT athlete_id, canonical_name, year, indoor, age_group_code, age_group_name, gender,
       discipline_name, rank, mark_raw, mark_value, wind, wind_assisted, club_code, location,
       perf_date, 0 AS is_relay, NULL AS relay_team_name
FROM v_athlete_results
UNION ALL
SELECT athlete_id, canonical_name, year, indoor, age_group_code, age_group_name, gender,
       discipline_name, rank, mark_raw, mark_value, wind, wind_assisted, club_code, location,
       perf_date, 1 AS is_relay, relay_team_name
FROM v_athlete_relay_results;

CREATE VIEW v_athlete_club_history AS
SELECT DISTINCT athlete_id, canonical_name, year, club_code
FROM v_athlete_results
WHERE club_code IS NOT NULL
ORDER BY athlete_id, year;
"""


def normalize_name_key(name):
    decomposed = unicodedata.normalize('NFKD', name)
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', stripped).strip().upper()


class Loader:
    def __init__(self, conn):
        self.conn = conn
        self.athlete_cache = {}  # (name_key, birth_year) -> athlete_id
        self.club_cache = {}
        self.event_cache = {}
        self.season_cache = {}

    def get_season_id(self, year):
        if year in self.season_cache:
            return self.season_cache[year]
        cur = self.conn.execute('INSERT INTO seasons(year) VALUES (?)', (year,))
        self.season_cache[year] = cur.lastrowid
        return cur.lastrowid

    def get_club_id(self, code):
        if not code:
            return None
        if code in self.club_cache:
            return self.club_cache[code]
        cur = self.conn.execute('INSERT INTO clubs(code) VALUES (?)', (code,))
        self.club_cache[code] = cur.lastrowid
        return cur.lastrowid

    def get_event_id(self, age_group_code, age_group_name, gender, discipline_name):
        key = (age_group_code, gender, discipline_name)
        if key in self.event_cache:
            return self.event_cache[key]
        cur = self.conn.execute(
            'INSERT INTO events(age_group_code, age_group_name, gender, discipline_name) '
            'VALUES (?, ?, ?, ?)',
            (age_group_code, age_group_name, gender, discipline_name),
        )
        self.event_cache[key] = cur.lastrowid
        return cur.lastrowid

    def get_athlete_id(self, name, birth_year, birth_date, gender, year):
        name_key = normalize_name_key(name)
        cache_key = (name_key, birth_year)
        athlete_id = self.athlete_cache.get(cache_key)
        if athlete_id is None:
            cur = self.conn.execute(
                'INSERT INTO athletes(name_key, canonical_name, birth_year, birth_date, gender) '
                'VALUES (?, ?, ?, ?, ?)',
                (name_key, name, birth_year, birth_date, gender),
            )
            athlete_id = cur.lastrowid
            self.athlete_cache[cache_key] = athlete_id
        elif birth_date:
            self.conn.execute(
                'UPDATE athletes SET birth_date = ?, canonical_name = ? '
                'WHERE id = ? AND birth_date IS NULL',
                (birth_date, name, athlete_id),
            )
        self.conn.execute(
            'INSERT OR IGNORE INTO athlete_aliases(athlete_id, name_variant, source_year) '
            'VALUES (?, ?, ?)',
            (athlete_id, name, year),
        )
        return athlete_id

    def load_row(self, row):
        season_id = self.get_season_id(row.year)
        event_id = self.get_event_id(
            row.age_group_code, row.age_group_name, row.gender, row.discipline
        )
        club_id = self.get_club_id(row.club_code)
        mark_value = normalize_mark(row.mark_raw)

        athlete_id = None
        if not row.is_relay:
            athlete_id = self.get_athlete_id(
                row.name, row.birth_year, row.birth_date, row.gender, row.year
            )

        cur = self.conn.execute(
            'INSERT INTO results(athlete_id, event_id, season_id, indoor, rank, mark_raw, '
            'mark_value, wind, wind_assisted, club_id, location, perf_date, is_relay, '
            'relay_team_name, source_file) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (
                athlete_id, event_id, season_id, int(row.indoor), row.rank, row.mark_raw,
                mark_value, row.wind, int(row.wind_assisted), club_id, row.location,
                row.perf_date, int(row.is_relay), row.relay_team_name, row.source_file,
            ),
        )
        result_id = cur.lastrowid

        for leg_order, (leg_name, leg_birth_year) in enumerate(row.relay_legs, start=1):
            leg_athlete_id = self.get_athlete_id(
                leg_name, leg_birth_year, None, row.gender if row.gender != 'X' else None,
                row.year,
            )
            self.conn.execute(
                'INSERT INTO relay_legs(result_id, athlete_id, leg_order, birth_year) '
                'VALUES (?, ?, ?, ?)',
                (result_id, leg_athlete_id, leg_order, leg_birth_year),
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

    year_dirs = sorted(
        (p for p in DATA_DIR.iterdir() if p.is_dir()),
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

    conn.commit()

    print("\n--- Summary ---")
    for table in ('athletes', 'athlete_aliases', 'clubs', 'seasons', 'events', 'results',
                  'relay_legs'):
        count = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        print(f"{table}: {count}")
    print(f"total rows loaded: {total_rows}")

    conn.close()


if __name__ == '__main__':
    build()
