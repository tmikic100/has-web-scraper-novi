"""Read-only export API over athletics.db (new normalized schema).

Mirrors the four export functions from the reference C# ExportController:
club statistics, club records, athlete career, athlete single-year.
Run with: uvicorn api:app --reload
"""
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / 'athletics.db'

app = FastAPI(title="Athletics Export API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "db_exists": DB_PATH.exists()}


def get_conn():
    conn = sqlite3.connect(f'file:{DB_PATH}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def get_club_id(conn, short_name: str) -> int:
    row = conn.execute('SELECT id FROM club WHERE short_name = ?', (short_name,)).fetchone()
    if row is None:
        raise HTTPException(404, f"unknown club '{short_name}'")
    return row['id']


def resolve_season_year(conn, year: Optional[int]) -> Optional[int]:
    if year is None:
        return None
    row = conn.execute('SELECT id FROM season WHERE year = ?', (year,)).fetchone()
    if row is None:
        raise HTTPException(404, f"no season for year {year}")
    return row['id']


@app.get("/seasons")
def list_seasons():
    conn = get_conn()
    try:
        rows = conn.execute('SELECT year FROM season ORDER BY year DESC').fetchall()
        return [row['year'] for row in rows]
    finally:
        conn.close()


@app.get("/athletes/search")
def search_athletes(name: str, exact: bool = False):
    """Resolve a name to athlete id(s) — matches current name or any past alias."""
    conn = get_conn()
    try:
        if exact:
            rows = conn.execute("""
                SELECT DISTINCT a.id, a.name, a.female, a.birth_year, a.birthday
                FROM athlete a
                LEFT JOIN athlete_alias al ON al.athlete_id = a.id
                WHERE a.name = ? OR al.name_variant = ?
            """, (name, name)).fetchall()
        else:
            like = f'%{name}%'
            rows = conn.execute("""
                SELECT DISTINCT a.id, a.name, a.female, a.birth_year, a.birthday
                FROM athlete a
                LEFT JOIN athlete_alias al ON al.athlete_id = a.id
                WHERE a.name LIKE ? OR al.name_variant LIKE ?
            """, (like, like)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _career_rows(conn, athlete_id: int, season_id: Optional[int] = None):
    query = """
        SELECT s.year, d.name AS discipline, d.indoor, r.mark, r.mark_value,
               r.wind, r.city, r.date, r.wa_points, r.info,
               ag.tech_name AS agegroup, ra.rank,
               ra.season_best,
               c.short_name AS club,
               (SELECT COUNT(*) FROM raceathlete ra2 WHERE ra2.race_id = r.id) AS team_size
        FROM raceathlete ra
        JOIN race r ON r.id = ra.race_id
        JOIN discipline d ON d.id = r.discipline_id
        JOIN season s ON s.id = r.season_id
        JOIN agegroup ag ON ag.id = ra.agegroup_id
        LEFT JOIN clubathlete ca ON ca.athlete_id = ra.athlete_id AND ca.season_id = r.season_id
        LEFT JOIN club c ON c.id = ca.club_id
        WHERE ra.athlete_id = ?
    """
    params = [athlete_id]
    if season_id is not None:
        query += " AND r.season_id = ?"
        params.append(season_id)
    query += " ORDER BY r.date"
    return [dict(row) for row in conn.execute(query, params).fetchall()]


@app.get("/athletes/{athlete_id}/career")
def get_athlete_career(athlete_id: int):
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

    best = {}
    for row in rows:
        key = (row['athlete_id'], row['discipline_id'])
        current = best.get(key)
        if current is None or row['wa_points'] > current['wa_points']:
            best[key] = row

    ranked = sorted((dict(r) for r in best.values()), key=lambda r: -r['wa_points'])
    prev_points, rank = None, 0
    for i, r in enumerate(ranked, 1):
        if r['wa_points'] != prev_points:
            rank = i
        r['rank'] = rank
        prev_points = r['wa_points']
    return ranked


@app.get("/clubs/{short_name}/statistics")
def get_club_statistics(short_name: str, year: int = Query(..., description="season year")):
    """Roster size, races entered, podiums (raw participation counts) plus a
    scored individual ranking list for a club in one season -- see
    _individual_best_scores for why WA points aren't just summed/averaged
    over every raceathlete row."""
    conn = get_conn()
    try:
        club_id = get_club_id(conn, short_name)
        season_id = resolve_season_year(conn, year)

        roster = conn.execute("""
            SELECT COUNT(*) FROM clubathlete WHERE club_id = ? AND season_id = ?
        """, (club_id, season_id)).fetchone()[0]

        rows = conn.execute("""
            SELECT ra.race_id, ra.rank
            FROM clubathlete ca
            JOIN raceathlete ra ON ra.athlete_id = ca.athlete_id
            JOIN race r ON r.id = ra.race_id AND r.season_id = ca.season_id
            WHERE ca.club_id = ? AND ca.season_id = ?
        """, (club_id, season_id)).fetchall()

        distinct_races = {row['race_id'] for row in rows}
        podiums = sum(1 for row in rows if row['rank'] in (1, 2, 3))

        ranking = _individual_best_scores(conn, club_id, season_id)
        points = [r['wa_points'] for r in ranking]

        return {
            "club": short_name,
            "year": year,
            "athletes": roster,
            "races_entered": len(distinct_races),
            "podium_finishes": podiums,
            "wa_points_total": sum(points),
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
            better = (row['mark_value'] > current['mark_value']) if row['higher_is_better'] \
                else (row['mark_value'] < current['mark_value'])
            if better:
                best[key] = row

        records = sorted(
            (dict(row) for row in best.values()),
            key=lambda r: (r['indoor'], r['discipline']),
        )
        return {"club": short_name, "year": year, "records": records}
    finally:
        conn.close()
