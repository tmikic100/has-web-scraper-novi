"""Read-only export API over athletics.db (new normalized schema).

Mirrors the four export functions from the reference C# ExportController:
club statistics, club records, athlete career, athlete single-year.
Run with: uvicorn api:app --reload

To point this at the synthetic test database instead of the real
athletics.db (see build_db.py --test), set the ATHLETICS_DB_PATH
environment variable before starting uvicorn, e.g.:
  ATHLETICS_DB_PATH=athletics_test.db uvicorn api:app --reload --port 8000
"""
# os: used here just to read the ATHLETICS_DB_PATH environment variable.
import os
# sqlite3: Python's built-in database driver for SQLite files.
import sqlite3
# defaultdict: used to group rank_snapshot rows by raceathlete_id.
from collections import defaultdict
# Path: object-oriented filesystem paths.
from pathlib import Path
# Optional[int] is a *type hint* meaning "an int, or None". Type hints don't
# change how Python runs the code (Python doesn't enforce them at runtime by
# default) -- they're documentation for humans and tools (editors, FastAPI
# itself) about what a function expects/returns.
from typing import Optional

# FastAPI: the web framework that turns plain Python functions into HTTP API
# endpoints. HTTPException lets us return proper error responses (like a 404);
# Query lets us describe/validate URL query-string parameters.
from fastapi import FastAPI, HTTPException, Query
# CORS = Cross-Origin Resource Sharing -- browser security rules about which
# websites are allowed to call this API from JavaScript. The middleware below
# relaxes those rules so the docs/pages/*.html front-end (served from a
# different origin/port) can fetch data from this API.
from fastapi.middleware.cors import CORSMiddleware

# __file__ is this script's own path; .resolve() makes it an absolute path
# (no "..", no relative segments) and .parent is the folder containing it.
BASE_DIR = Path(__file__).resolve().parent
# os.environ.get(name, default) reads an environment variable, falling back
# to `default` if it isn't set -- this lets `ATHLETICS_DB_PATH=athletics_test.db
# uvicorn api:app` point the whole API at the test database (see build_db.py
# --test) without touching any code, while a plain `uvicorn api:app` with no
# environment variable set still serves the real athletics.db as before.
DB_PATH = Path(os.environ.get('ATHLETICS_DB_PATH', BASE_DIR / 'athletics.db'))

# `app` is the central FastAPI application object. Every `@app.get(...)`
# decorator below registers one more URL route on this same app. uvicorn
# (the web server) is told to run this specific `app` object via the
# "api:app" string in the run command at the top of this docstring.
app = FastAPI(title="Athletics Export API")
# add_middleware wraps every request/response through the given class.
# allow_origins=["*"] means "any website may call this API" -- fine for a
# small public read-only data API, but you'd normally lock this down to
# specific domains for anything that isn't purely public data.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# A "decorator" (the `@app.get("/health")` line) is Python syntax for
# wrapping a function with extra behavior -- here, FastAPI uses it to say
# "when an HTTP GET request comes in for the path /health, call this
# function and turn whatever it returns into a JSON response." You never
# call `health()` yourself; the web framework does, per incoming request.
@app.get("/health")
def health():
    return {"status": "ok", "db_exists": DB_PATH.exists(), "db_path": str(DB_PATH)}


def get_conn():
    """Opens a fresh, read-only connection to athletics.db. A new connection
    is opened per request (see the try/finally pattern in every endpoint
    below) rather than one shared connection, which keeps requests isolated
    and avoids any cross-request state or threading issues."""
    # "file:...?mode=ro" plus uri=True is sqlite3's way of opening a database
    # in strictly read-only mode -- this API only ever reads, never writes,
    # so this guards against an API bug ever accidentally mutating the data.
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    # Return rows as sqlite3.Row objects instead of plain tuples, so callers
    # can do row['column_name'] instead of remembering column positions.
    conn.row_factory = sqlite3.Row
    return conn


def get_club_id(conn, short_name: str) -> int:
    """Looks up a club's numeric id from its short code (e.g. "AGR"). Raises
    an HTTP 404 error (via FastAPI's exception handling) if the code doesn't
    exist, instead of returning None and making every caller check for it."""
    row = conn.execute('SELECT id FROM club WHERE short_name = ?', (short_name,)).fetchone()
    if row is None:
        raise HTTPException(404, f"unknown club '{short_name}'")
    return row['id']


def resolve_season_year(conn, year: Optional[int]) -> Optional[int]:
    """Turns a season YEAR (e.g. 2026, what URLs use) into the internal
    season.id (what foreign keys use). Passing None through unchanged lets
    callers use this for "no year filter was given" without a separate
    branch at every call site."""
    if year is None:
        return None
    row = conn.execute('SELECT id FROM season WHERE year = ?', (year,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no season for year {year}")
    return row['id']


@app.get("/seasons")
def list_seasons():
    conn = get_conn()
    # try/finally guarantees conn.close() runs even if something above it
    # raises an exception (e.g. a malformed query) -- without this, a failed
    # request could leak an open database connection.
    try:
        rows = conn.execute('SELECT year FROM season ORDER BY year DESC').fetchall()
        # A list comprehension: build a new list by evaluating `row['year']`
        # for every `row` in `rows`, equivalent to (but more concise than):
        #   result = []
        #   for row in rows:
        #       result.append(row['year'])
        return [row['year'] for row in rows]
    finally:
        conn.close()


@app.get("/athletes/list")
def list_athletes():
    """Full id/label list for client-side autocomplete (fetched once, filtered
    in the browser) rather than a live per-keystroke search endpoint."""
    conn = get_conn()
    try:
        rows = conn.execute('SELECT id, name, birth_year FROM athlete ORDER BY name').fetchall()
        # A list comprehension whose body is a dict literal -- for each row,
        # build a small {"id":..., "name":..., "label":...} dict. The
        # ternary expression (`A if condition else B`) picks between two
        # label formats depending on whether birth_year is known.
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "label": f"{row['name']} ({row['birth_year']})" if row["birth_year"] else row["name"],
            }
            for row in rows
        ]
    finally:
        conn.close()


@app.get("/clubs/list")
def list_clubs():
    """Full club list for client-side autocomplete, same rationale as /athletes/list."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT short_name, name, city FROM club WHERE short_name != 'IND' ORDER BY name"
        ).fetchall()
        return [
            {
                "short_name": row["short_name"],
                "name": row["name"] or row["short_name"],
                "label": f"{row['name']} ({row['short_name']})" if row["name"] else row["short_name"],
            }
            for row in rows
        ]
    finally:
        conn.close()


@app.get("/athletes/search")
def search_athletes(name: str, exact: bool = False):
    """Resolve a name to athlete id(s) — matches current name or any past alias."""
    # FastAPI reads a function's parameters (name: str, exact: bool = False)
    # to figure out what URL query parameters this endpoint accepts, e.g.
    # /athletes/search?name=Farkas&exact=true -- and automatically converts
    # the raw text from the URL into the right Python types.
    conn = get_conn()
    try:
        if exact:
            # LEFT JOIN keeps every athlete row even if they have zero
            # matching athlete_alias rows (an inner JOIN would drop them).
            # DISTINCT avoids returning the same athlete twice if both their
            # current name AND an alias happen to match.
            rows = conn.execute("""
                SELECT DISTINCT a.id, a.name, a.female, a.birth_year, a.birthday
                FROM athlete a
                LEFT JOIN athlete_alias al ON al.athlete_id = a.id
                WHERE a.name = ? OR al.name_variant = ?
            """, (name, name)).fetchall()
        else:
            # SQL's LIKE with '%' wildcards on both sides does a substring
            # match, e.g. '%far%' matches "Farkaš" anywhere in the name.
            like = f'%{name}%'
            rows = conn.execute("""
                SELECT DISTINCT a.id, a.name, a.female, a.birth_year, a.birthday
                FROM athlete a
                LEFT JOIN athlete_alias al ON al.athlete_id = a.id
                WHERE a.name LIKE ? OR al.name_variant LIKE ?
            """, (like, like)).fetchall()
        # dict(r) converts a sqlite3.Row into a plain dict -- FastAPI knows
        # how to turn plain dicts (and lists of them) into a JSON response
        # automatically, so this is the "hand it off to the framework" step.
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _rank_history(conn, raceathlete_ids):
    """Every rank_snapshot row (one per past wa_ranking.py / --test-week run)
    for the given raceathlete ids, grouped by raceathlete_id -- this is the
    "rank changed over time, even though this specific race's own mark never
    did" history described in wa_ranking.py's module docstring. Returns
    {raceathlete_id: [snapshot dict, ...]}, each list ordered oldest-first
    (real runs by scrape_date, test runs by scrape_week -- a given
    raceathlete only ever has one kind or the other, never both, since a DB
    is either being fed by real wa_ranking.py runs or --test ones)."""
    if not raceathlete_ids:
        return {}
    # "?" placeholders can't directly represent "one per item in a list" --
    # this builds "?,?,?" with one "?" per id, then passes the ids
    # themselves as the parameter list, so sqlite3 still does safe
    # parameter binding (not string-pasting the ids into the SQL).
    placeholders = ','.join('?' * len(raceathlete_ids))
    rows = conn.execute(f"""
        SELECT raceathlete_id, scrape_date, scrape_week,
               world_rank_senior, world_rank_u23, world_rank_u20, world_rank_u18,
               europe_rank_senior, europe_rank_u23, europe_rank_u20, europe_rank_u18,
               national_rank_senior, national_rank_u23, national_rank_u20,
               national_rank_u18, national_rank_u16, national_rank_u14
        FROM rank_snapshot
        WHERE raceathlete_id IN ({placeholders})
        ORDER BY raceathlete_id, scrape_date, scrape_week
    """, raceathlete_ids).fetchall()
    history = defaultdict(list)
    for row in rows:
        d = dict(row)
        raceathlete_id = d.pop('raceathlete_id')
        history[raceathlete_id].append(d)
    return dict(history)


def _career_rows(conn, athlete_id: int, season_id: Optional[int] = None):
    """Shared query behind both /career (every season) and /{year} (one
    season) -- season_id=None means "no season filter", used by the career
    endpoint below. A leading underscore marks this as an internal helper,
    not one of the API's own routes (notice it has no @app.get decorator)."""
    query = """
        SELECT ra.id AS raceathlete_id,
               s.year, d.name AS discipline, d.indoor, d.higher_is_better, r.mark, r.mark_value,
               r.wind, r.city, r.date, r.wa_points, r.info,
               ra.world_rank_senior, ra.world_rank_u23, ra.world_rank_u20, ra.world_rank_u18,
               ra.europe_rank_senior, ra.europe_rank_u23, ra.europe_rank_u20, ra.europe_rank_u18,
               ra.national_rank_senior, ra.national_rank_u23, ra.national_rank_u20,
               ra.national_rank_u18, ra.national_rank_u16, ra.national_rank_u14,
               c.short_name AS club,
               (SELECT COUNT(*) FROM raceathlete ra2 WHERE ra2.race_id = r.id) AS team_size
        FROM raceathlete ra
        JOIN race r ON r.id = ra.race_id
        JOIN discipline d ON d.id = r.discipline_id
        JOIN season s ON s.id = r.season_id
        LEFT JOIN clubathlete ca ON ca.athlete_id = ra.athlete_id AND ca.season_id = r.season_id
        LEFT JOIN club c ON c.id = ca.club_id
        WHERE ra.athlete_id = ?
    """
    # Building the SQL and its parameter list up incrementally: start with
    # the base query/params, then conditionally append more of each. Using
    # `?` placeholders (rather than pasting season_id straight into the
    # string) avoids SQL injection and lets sqlite3 handle quoting safely.
    params = [athlete_id]
    if season_id is not None:
        query += " AND r.season_id = ?"
        params.append(season_id)
    query += " ORDER BY r.date"
    results = [dict(row) for row in conn.execute(query, params).fetchall()]

    history_by_raceathlete = _rank_history(conn, [r['raceathlete_id'] for r in results])
    for r in results:
        # r.pop(...) both reads raceathlete_id (to look up its history) and
        # removes it from the row in one step -- it was only ever needed
        # internally here, not part of the public response shape.
        r['rank_history'] = history_by_raceathlete.get(r.pop('raceathlete_id'), [])
    return results


@app.get("/athletes/{athlete_id}/career")
def get_athlete_career(athlete_id: int):
    # `{athlete_id}` in the route path above is a "path parameter" -- FastAPI
    # extracts whatever's in that URL segment (e.g. /athletes/42/career) and
    # passes it in as the `athlete_id` argument, converted to int per the
    # type hint (a non-numeric value there would automatically produce a
    # 422 error response, without any code here needing to check for it).
    conn = get_conn()
    try:
        athlete = conn.execute('SELECT * FROM athlete WHERE id = ?', (athlete_id,)).fetchone()
        if athlete is None:
            raise HTTPException(404, f"unknown athlete id {athlete_id}")
        return {"athlete": dict(athlete), "results": _career_rows(conn, athlete_id)}
    finally:
        conn.close()


@app.get("/athletes/{athlete_id}/{year}")
def get_athlete_single_year(athlete_id: int, year: int):
    conn = get_conn()
    try:
        athlete = conn.execute('SELECT * FROM athlete WHERE id = ?', (athlete_id,)).fetchone()
        if athlete is None:
            raise HTTPException(404, f"unknown athlete id {athlete_id}")
        season_id = resolve_season_year(conn, year)
        return {"athlete": dict(athlete), "year": year, "results": _career_rows(conn, athlete_id, season_id)}
    finally:
        conn.close()


def _individual_best_scores(conn, club_id, season_id):
    """One row per (athlete, discipline) -- that athlete's best-scoring individual
    result for the club/season. Generalizes score_agr_2025.py's per-athlete-per-
    discipline dedup (previously hardcoded to club AGR / year 2025) to any
    club/year. discipline_id already encodes indoor vs outdoor (see the
    `UNIQUE(name, female, indoor)` schema constraint), so grouping on it alone
    reproduces that script's (discipline, indoor) split for free. Relays are
    excluded (`r.info IS NULL` -- only relay races have team-name info set, see
    build_db.py's load_row) since a relay score isn't one athlete's own
    performance. Relies on race.wa_points already being computed at build time
    (Loader.maybe_populate_wapoint / Scorer.score), so no WA-column mapping
    needs to be re-derived here -- unscored (non-"standard") disciplines are
    naturally excluded since their wa_points is NULL.
    """
    rows = conn.execute("""
        SELECT ra.athlete_id, a.name AS athlete_name, d.id AS discipline_id,
               d.name AS discipline, d.indoor, r.mark, r.mark_value, r.date,
               r.city, r.wa_points
        FROM clubathlete ca
        JOIN raceathlete ra ON ra.athlete_id = ca.athlete_id
        JOIN race r ON r.id = ra.race_id AND r.season_id = ca.season_id
        JOIN discipline d ON d.id = r.discipline_id
        JOIN athlete a ON a.id = ra.athlete_id
        WHERE ca.club_id = ? AND ca.season_id = ?
          AND r.info IS NULL AND r.wa_points IS NOT NULL
    """, (club_id, season_id)).fetchall()

    # Plain Python loop (rather than SQL's own GROUP BY/MAX) to keep the
    # *entire* winning row (mark, date, city, ...) per (athlete, discipline),
    # not just the winning points number -- SQL aggregate functions like
    # MAX() only give you the single aggregated value, not the whole row it
    # came from, without a more awkward query.
    best = {}
    for row in rows:
        key = (row['athlete_id'], row['discipline_id'])
        current = best.get(key)
        if current is None or row['wa_points'] > current['wa_points']:
            best[key] = row

    # A generator expression `(dict(r) for r in best.values())` is like a
    # list comprehension but produces items one at a time instead of
    # building the whole list upfront -- sorted() consumes it fully anyway
    # here, so the practical effect is the same as a list comprehension, but
    # it's a common enough style to be worth recognizing.
    ranked = sorted((dict(r) for r in best.values()), key=lambda r: -r['wa_points'])
    prev_points, rank = None, 0
    for i, r in enumerate(ranked, 1):
        # Standard "competition ranking": ties share the same rank number,
        # and the next distinct score resumes at its true position (1, 2, 2,
        # 4, ...) rather than a dense sequence (1, 2, 2, 3, ...).
        if r['wa_points'] != prev_points:
            rank = i
        r['rank'] = rank
        prev_points = r['wa_points']
    return ranked


@app.get("/clubs/{short_name}/statistics")
def get_club_statistics(short_name: str, year: int = Query(..., description="season year")):
    """Roster size, races entered, plus a scored individual ranking list for
    a club in one season -- see _individual_best_scores for why WA points
    aren't just summed/averaged over every raceathlete row."""
    # `Query(..., description=...)` marks `year` as a REQUIRED query-string
    # parameter (the `...` -- Python's "Ellipsis" object -- means "no
    # default, this must be supplied") and attaches a description that shows
    # up in FastAPI's auto-generated /docs page.
    conn = get_conn()
    try:
        club_id = get_club_id(conn, short_name)
        season_id = resolve_season_year(conn, year)

        # fetchone() on a `SELECT COUNT(*)` returns a single row containing
        # one column; [0] grabs that first (and only) value directly instead
        # of going through the row['colname'] name-based access used
        # elsewhere in this file.
        roster = conn.execute("""
            SELECT COUNT(*) FROM clubathlete WHERE club_id = ? AND season_id = ?
        """, (club_id, season_id)).fetchone()[0]

        rows = conn.execute("""
            SELECT ra.race_id
            FROM clubathlete ca
            JOIN raceathlete ra ON ra.athlete_id = ca.athlete_id
            JOIN race r ON r.id = ra.race_id AND r.season_id = ca.season_id
            WHERE ca.club_id = ? AND ca.season_id = ?
        """, (club_id, season_id)).fetchall()

        # A set comprehension `{... for ... in ...}` -- same idea as a list
        # comprehension, but the curly braces build a set, which
        # automatically collapses duplicate race_ids (a relay race can
        # produce multiple raceathlete rows, one per club member on that
        # relay leg, but it's still only one physical race).
        distinct_races = {row['race_id'] for row in rows}

        ranking = _individual_best_scores(conn, club_id, season_id)
        points = [r['wa_points'] for r in ranking]

        return {
            "club": short_name,
            "year": year,
            "athletes": roster,
            "races_entered": len(distinct_races),
            "wa_points_total": sum(points),
            # A ternary guarding against division by zero: only compute the
            # average if there's at least one scored result, else report
            # None (which FastAPI serializes as JSON `null`).
            "wa_points_avg": (sum(points) / len(points)) if points else None,
            "ranking": ranking,
        }
    finally:
        conn.close()


@app.get("/clubs/{short_name}/records")
def get_club_records(short_name: str, year: Optional[int] = None):
    """Best mark per discipline+gender for athletes on this club's roster.

    If `year` is omitted, considers every season the athlete was on the
    club's roster (all-time record); otherwise scoped to that one season.
    National/representative-team relay races (race.is_club_team = 0) are
    excluded — they belong to the country, not the club.
    """
    # `year: Optional[int] = None` makes this an OPTIONAL query parameter --
    # unlike /statistics above, calling this endpoint with no ?year=... at
    # all is valid and simply leaves `year` as None.
    conn = get_conn()
    try:
        club_id = get_club_id(conn, short_name)
        season_id = resolve_season_year(conn, year) if year is not None else None

        query = """
            SELECT d.id AS discipline_id, d.name AS discipline, d.indoor, d.female,
                   d.higher_is_better, r.mark, r.mark_value, r.date, r.city,
                   a.id AS athlete_id, a.name AS athlete_name, s.year
            FROM clubathlete ca
            JOIN raceathlete ra ON ra.athlete_id = ca.athlete_id
            JOIN race r ON r.id = ra.race_id AND r.season_id = ca.season_id
            JOIN discipline d ON d.id = r.discipline_id
            JOIN athlete a ON a.id = ra.athlete_id
            JOIN season s ON s.id = r.season_id
            WHERE ca.club_id = ? AND r.mark_value IS NOT NULL AND r.is_club_team = 1
        """
        params = [club_id]
        if season_id is not None:
            query += " AND ca.season_id = ?"
            params.append(season_id)

        rows = conn.execute(query, params).fetchall()

        best = {}
        for row in rows:
            key = (row['discipline_id'], row['female'])
            current = best.get(key)
            if current is None:
                best[key] = row
                continue
            # Which mark counts as "better" depends on the event: for
            # times, lower is better; for distance/height, higher is
            # better -- d.higher_is_better (computed once at build time)
            # tells us which comparison direction to use here.
            better = (row['mark_value'] > current['mark_value']) if row['higher_is_better'] \
                else (row['mark_value'] < current['mark_value'])
            if better:
                best[key] = row

        # Sort by a tuple key: primary sort on `indoor` (False/0 sorts
        # before True/1, so outdoor records list first), secondary sort
        # alphabetically by discipline name for ties on that first key.
        records = sorted(
            (dict(row) for row in best.values()),
            key=lambda r: (r['indoor'], r['discipline']),
        )
        return {"club": short_name, "year": year, "records": records}
    finally:
        conn.close()
