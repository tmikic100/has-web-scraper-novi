"""
Scores AGR club's 2025 individual results (all age groups, indoor+outdoor combined) against
the World Athletics Scoring Tables, keeping each athlete's best-scoring mark per
(discipline, indoor/outdoor), and writes the result to a tab-separated club ranking list:

    R.B.  Disciplina  Atletičar/ka  Rezultat  Datum  Grad  Bodovi

Scope notes (see conversation for the reasoning):
- Individual events only -- relays are not included.
- Only events run/thrown/jumped at standard senior specs are scored, since the official WA
  tables only cover the ~80 open events per sex. Age-group variants with different hurdle
  heights or implement weights (noted in the source data as e.g. "(h=0,840m)" or
  "Kugla (4 kg)") have no corresponding table and are skipped -- there's no official points
  value for them. Running/jumping events that don't depend on equipment (sprints, middle/long
  distance, walks, long/high/triple jump, pole vault) DO apply across all age groups.
- "Milja / One Mille" is assumed to be the track mile (MD_II 'Mile'), not a road mile, since
  the source data doesn't distinguish.
- Foreign venue cities get a "(XXX)" country code appended, from a hand-built lookup of the
  cities that actually appear in AGR's 2025 results (see FOREIGN_CITY_COUNTRY below) -- not a
  geocoding service, so a city missing from that table prints a warning instead of a guess.

Note for readers: main() below queries a SQL VIEW called v_athlete_results that no longer
exists in the current athletics.db schema (build_db.py doesn't create it) -- so running this
file's main() today would raise a sqlite3 error. resolve_column(), however, is still very
much alive: build_db.py imports it directly to decide, per has.hr row, which WA scoring
column (if any) applies. So this module is half "one-off report generator" (main(), stale)
and half "still-used lookup logic" (resolve_column(), current) -- worth knowing before you
try to just run this file standalone.
"""

# csv: reads/writes comma- (or here, tab-) separated files without you having to
#      hand-roll the escaping/quoting rules yourself.
# re: regular expressions, for pattern-based string search/replace.
# sqlite3: Python's built-in SQLite database driver (no separate install needed).
import csv
import re
import sqlite3
# Path (from pathlib) represents a filesystem path as an object with useful
# methods/operators, instead of doing raw string concatenation for file paths.
from pathlib import Path

from wa_scoring import Scorer

# __file__ is the path to this very script. .parent walks up one directory
# level, then `/` (overloaded by Path) joins on a filename -- much less
# error-prone than manually gluing strings with os.path.join everywhere.
DB_PATH = Path(__file__).parent / 'athletics.db'
OUT_PATH = Path(__file__).parent / 'AGR_2025_WA_points.csv'

# A dict literal mapping has.hr's bilingual discipline name (as stored in
# athletics.db) to a tuple of (WA scoring-table column name, does this event
# have a separate indoor points table?). Written out by hand once, since WA's
# column naming doesn't follow a predictable pattern we could derive
# programmatically.
EVENT_MAP = {
    '10 km (cesta) / 10 km (Road Race)': ('10 km', False),
    '100 m': ('100m', False),
    '100 m prepone / 100 m Hurdles': ('100mH', False),
    '1000 m': ('1000m', True),
    '10000 m': ('10000m', False),
    '110 m prepone / 110 m Hurdles': ('110mH', False),
    '1500 m': ('1500m', True),
    '2 milje / 2 Milles': ('2 Miles', True),
    '200 m': ('200m', True),
    '2000 m': ('2000m', True),
    '2000 m zapreke / 2000 m Steeplechase': ('2000m SC', False),
    '3000 m': ('3000m', True),
    '3000 m zapreke / 3000 m Steeplechase': ('3000m SC', False),
    '300 m': ('300m', True),
    '400 m': ('400m', True),
    '400 m prepone / 400 m Hurdles': ('400mH', False),
    '5 km (cesta)': ('5 km', False),
    '5 km (cesta) / 5 km (Road Race)': ('5 km', False),
    '50 m': ('50m', False),
    '500 m': ('500m', True),
    '5000 m': ('5000m', True),
    '55 m': ('55m', False),
    '55 m prepone / 55 m Hurdles': ('55mH', False),
    '60 m': ('60m', False),
    '60 m prepone / 60 m Hurdles': ('60mH', False),
    '600 m': ('600m', True),
    '800 m': ('800m', True),
    'Dalj / Long Jump': ('LJ', False),
    'Disk / Discus Throw': ('DT', False),
    'Kladivo / Hammer Throw': ('HT', False),
    'Koplje / Javelin Throw': ('JT', False),
    'Kugla / Shot Put': ('SP', False),
    'Milja / One Mille': ('Mile', True),
    'Motka / Pole Vault': ('PV', False),
    'Polumaraton / Half Marathon': ('HM', False),
    'Maraton / Marathon': ('Marathon', False),
    'Troskok / Triple Jump': ('TJ', False),
    'Vis / High Jump': ('HJ', False),
    '35 km hodanje (cesta) / 35 km Road Race Walking': ('35km W', False),
    '20 km hodanje (cesta) / 20 km Road Race Walking': ('20km W', False),
    '10 km hodanje (cesta) / 10 km Road Race Walking': ('10km W', False),
    '10000 hodanje / 10000 m Race Walking': ('10,000mW', False),
    '5000 m hodanje / 5000 Race Walking': ('5000mW', False),
    '3000 m hodanje / 3000 m Race Walking': ('3000mW', False),
    '5 km hodanje (cesta) / 5 km Road Race Walking': ('5km W', False),
    '3 km hodanje (cesta) / 3 km Road Race Walking': ('3km W', False),
    '50 m prepone / 50 m Hurdles': ('50mH', False),
}

# cities appearing in AGR's 2025 results that are outside Croatia -> country code to append.
# Croatian cities (Zagreb, Split, Osijek, ...) get no annotation.
FOREIGN_CITY_COUNTRY = {
    'Beograd': 'SRB',
    'Borsky Mikulaš': 'SVK',
    'Bratislava': 'SVK',
    'Bönnigheim': 'GER',
    'College Station, TX': 'USA',
    'Craiova': 'ROU',
    'Dudince': 'SVK',
    'Eppingen': 'GER',
    'Gainesville, FL': 'USA',
    'Jacksonville, FL': 'USA',
    'Kaposvar': 'HUN',
    'Maribor': 'SLO',
    'Minneapolis, MN': 'USA',
    'New York': 'USA',
    'Novo Mesto': 'SLO',
    'Skopje': 'MKD',
    'Sofia': 'BUL',
    'Tampere': 'FIN',
}
# A `set` literal (curly braces with no `:`, unlike a dict) -- used here
# purely for fast "is this city in the set?" membership checks in
# format_location(), where we don't need any value attached to each city.
CROATIAN_CITIES = {'Zagreb', 'Split', 'Osijek', 'Rijeka', 'Karlovac', 'Varaždin', 'Zadar',
                    'Zaprešić', 'Čakovec', 'Pula', 'Šibenik', 'Slavonski Brod'}


def resolve_column(discipline_name, gender, age_group_code, indoor):
    """Given one has.hr result row's details, returns the column name to look
    up in WA's scoring tables (see wa_scoring.Scorer), or None if this event
    has no official WA points table entry at all (e.g. an age-adjusted hurdle
    height with no senior equivalent). This is the function build_db.py still
    imports and calls for every row it loads."""
    # Pentathlon is a special case: WA only publishes a combined-events scoring
    # table for certain age groups/genders, so it can't just be a plain
    # EVENT_MAP lookup like everything else.
    if discipline_name == 'Petoboj / Pentathlon':
        if gender == 'F' and age_group_code in ('ss', 'ms'):
            return 'Pent.sh'
        return None
    # dict.get(key) returns None if the key isn't present, instead of raising
    # a KeyError the way `EVENT_MAP[discipline_name]` would -- convenient here
    # since "not in the map" is an expected, normal outcome (youth-only
    # variant events), not a bug.
    mapping = EVENT_MAP.get(discipline_name)
    if mapping is None:
        return None
    # Unpacking a 2-tuple into two named variables in one line.
    column, has_indoor_variant = mapping
    if indoor and has_indoor_variant:
        return column + ' sh'  # WA suffixes indoor ("short track"/hall) columns with " sh"
    return column


def format_discipline(discipline_name, indoor):
    """Turns has.hr's bilingual "Croatian / English" discipline name into a
    short Croatian-only display label for the CSV, e.g.
    '100 m prepone / 100 m Hurdles' -> '100m prepone'."""
    # str.split('/') breaks the string wherever a "/" appears, returning a
    # list of pieces; [0] takes just the first (Croatian) piece, and
    # .strip() removes any leading/trailing whitespace left over.
    name = discipline_name.split('/')[0].strip()
    # re.sub(pattern, replacement, string) finds every match of `pattern` and
    # replaces it. `(?<=\d)` and `(?=k?m\b)` are "lookbehind"/"lookahead"
    # assertions -- they check that a digit comes right before, and "m" or
    # "km" comes right after, WITHOUT consuming those characters themselves.
    # Net effect: collapses "100 m" -> "100m" but leaves other spaces alone.
    name = re.sub(r'(?<=\d) (?=k?m\b)', '', name)
    # Same idea: "3000 hodanje" -> "3000m hodanje" (inserts a missing "m").
    name = re.sub(r'(?<=\d)(?= hodanje\b)', 'm', name)
    if indoor and 'hodanje' not in name.lower():
        name += ' dvorana'  # "dvorana" = "indoor hall" in Croatian
    return name


def format_location(location):
    """Appends a "(XXX)" country code to foreign venue names; leaves known
    Croatian cities untouched; warns (but doesn't crash) on anything
    unrecognized so a missing entry gets noticed instead of silently
    producing a wrong-looking but plausible city name."""
    if location in CROATIAN_CITIES:
        return location
    country = FOREIGN_CITY_COUNTRY.get(location)
    if country is None:
        print(f"  WARN unknown city '{location}' -- no country code, check FOREIGN_CITY_COUNTRY")
        return location
    return f'{location} ({country})'


def main():
    # sqlite3.connect opens (or creates) a database file and gives us a
    # connection object to run SQL through.
    conn = sqlite3.connect(DB_PATH)
    # By default sqlite3 returns each row as a plain tuple (positional access
    # only, e.g. row[0]). Setting row_factory to sqlite3.Row instead lets us
    # access columns by name too, e.g. row['athlete_id'] -- much more
    # readable than remembering column order.
    conn.row_factory = sqlite3.Row
    scorer = Scorer()

    # conn.execute(sql) runs the query; .fetchall() pulls every matching row
    # back as a list (loaded fully into memory -- fine here since this is a
    # one-season, one-club result set, not millions of rows).
    rows = conn.execute("""
        SELECT athlete_id, canonical_name, age_group_code, age_group_name, gender,
               discipline_name, indoor, mark_raw, mark_value, location, perf_date
        FROM v_athlete_results
        WHERE year = 2025 AND club_code = 'AGR'
    """).fetchall()

    # An empty dict we'll fill in as we scan rows: for each (athlete,
    # discipline, indoor/outdoor) combination, keep only the single
    # best-scoring result seen so far.
    best_per_athlete_discipline = {}
    # A set (not a list) because we only care about *which* discipline names
    # had no WA table match, not how many times each one occurred, and sets
    # automatically drop duplicates for free.
    unscored_disciplines = set()

    for r in rows:
        column = resolve_column(r['discipline_name'], r['gender'], r['age_group_code'], r['indoor'])
        if column is None:
            unscored_disciplines.add(r['discipline_name'])
            continue
        points = scorer.score(r['gender'], column, r['mark_value'])
        if points is None:
            continue
        # Tuples are hashable (as long as their contents are), so a tuple of
        # (athlete_id, discipline_name, indoor) can be used directly as a
        # dict key -- a common Python pattern for "composite key" lookups.
        key = (r['athlete_id'], r['discipline_name'], r['indoor'])
        existing = best_per_athlete_discipline.get(key)
        if existing is None or points > existing['points']:
            best_per_athlete_discipline[key] = {
                'points': points,
                'discipline_name': r['discipline_name'],
                'canonical_name': r['canonical_name'],
                'mark_raw': r['mark_raw'],
                'perf_date': r['perf_date'],
                'location': r['location'],
                'indoor': r['indoor'],
            }

    # .values() gives us just the dict's values (the little result dicts),
    # discarding the composite keys we no longer need. sorted(..., key=...)
    # sorts using whatever the `key` function returns for each item; negating
    # the points (`-x['points']`) sorts highest-scoring first instead of
    # Python's default ascending order.
    results = sorted(best_per_athlete_discipline.values(), key=lambda x: -x['points'])

    # newline='' is the standard recommendation when writing CSV/TSV files in
    # Python, so the csv module can control line-ending behavior itself
    # instead of it being mangled by the file object's own newline handling.
    # 'utf-8-sig' writes a small byte-order-mark prefix that makes Excel
    # correctly detect UTF-8 (and thus display "č"/"ž"/etc. properly) instead
    # of guessing a Western-European encoding and mangling them.
    with open(OUT_PATH, 'w', newline='', encoding='utf-8-sig') as f:
        # delimiter='\t' makes this a *tab*-separated file even though it's
        # the same `csv` module normally used for comma-separated files.
        writer = csv.writer(f, delimiter='\t')
        writer.writerow(['R.B.', 'Disciplina', 'Atletičar/ka', 'Rezultat', 'Datum', 'Grad', 'Bodovi'])
        prev_points = None
        # enumerate(results, 1) numbers rows starting at 1, matching the
        # "R.B." (redni broj / sequence number) column in the output.
        for i, r in enumerate(results, 1):
            # Rows tied on points share the same rank number; only print a
            # fresh number when the points value actually changes from the
            # previous row (results is already sorted by points, so ties are
            # always adjacent).
            rb = '' if r['points'] == prev_points else str(i)
            prev_points = r['points']
            datum = ''
            if r['perf_date']:
                # The date is stored as "YYYY-MM-DD"; split('-') breaks it
                # into three pieces which we immediately unpack, then
                # reassemble in Croatian "DD.MM.YYYY" display order.
                y, m, d = r['perf_date'].split('-')
                datum = f'{d}.{m}.{y}'
            writer.writerow([rb, format_discipline(r['discipline_name'], r['indoor']),
                              r['canonical_name'], r['mark_raw'], datum,
                              format_location(r['location']), r['points']])

    print(f"Wrote {len(results)} scored rows to {OUT_PATH}")
    print(f"{len(unscored_disciplines)} distinct disciplines had no matching WA table entry:")
    for d in sorted(unscored_disciplines):
        print(f"  - {d}")


if __name__ == '__main__':
    main()
