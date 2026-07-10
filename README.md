# Athletics DB — how this project works

A pipeline that scrapes Croatian Athletics Federation (has.hr) results into
a normalized SQLite database, serves it through a small read-only API, and
shows it on a static web page. Everything refreshes weekly with no manual
steps once it's set up.

```
has.hr  --downloader.py-->  tablice_sezone/ (raw HTML)
                                    |
                          parse_tables.py (regex parsing)
                                    |
                            build_db.py (ETL + scoring)
                                    |
                             athletics.db (SQLite)
                                    |
                              api.py (FastAPI)
                                    |
                          docs/ (static frontend)
```

Two things drive updates automatically:
- **`.github/workflows/update-db.yml`** — every Monday, GitHub Actions
  re-scrapes has.hr, rebuilds `athletics.db`, and pushes it if it changed.
- **Render** (hosting the API) and **GitHub Pages** (hosting `docs/`) both
  auto-deploy on every push to `master` — so the Monday commit above is the
  only trigger the whole chain needs.

---

## 1. Scraping — `downloader.py`

Two independent jobs:

- **`download_all()`** — has.hr publishes one HTML/txt "season table" file
  per year + age group + gender + indoor/outdoor combination (filenames
  like `ssm25.html` = seniors, men, 2025). Rather than guessing filenames,
  it scrapes the actual list of `/tabsez/` links off the index page and
  downloads whatever's there, into `tablice_sezone/<year>/<filename>`.
  Already-downloaded files are skipped **except** for `CURRENT_YEAR`, whose
  files has.hr keeps appending to all season — those are always re-fetched.
- **`download_clubs()`** — has.hr's club directory (`/index.php/klubovi`,
  paginated) lists every club's short code / full name / city. Saved to
  `tablice_sezone/klubovi/page_N.html`. Cheap (~8 pages), so always
  re-fetched in full rather than cached.

Run it standalone: `python downloader.py`.

## 2. Parsing — `parse_tables.py`

Turns the raw, inconsistently-formatted HTML/text files into a list of
`ParsedRow` objects. This is the messiest part of the codebase because the
source format drifted over ~24 years — most of the file is regexes and
hand-written edge-case handling, not architecture. The pieces worth
knowing about:

- **`ROW_RE` / `RELAY_RE`** — match one result line for an individual event
  or a relay (relay lines list all leg runners inline, parsed further by
  `RELAY_LEG_RE`).
- **`parse_file(path)`** — the main entry point; picks the right parser
  based on the filename (`parse_standard_file` for the normal format,
  `parse_legacy_indoor_2002` for one especially malformed old file) and
  returns `list[ParsedRow]`.
- **`normalize_mark(mark_raw)`** — converts a displayed mark (`"11,34"`,
  `"2:37,60"`, `"1,88"`) into a single comparable float (seconds, or
  metres) — this is what `build_db.py` sorts/compares on, not the display
  string.
- **`normalize_discipline_name(raw, age_group_code)`** — has.hr writes the
  same event many different ways (`"4x400"` vs `"4x400m"`, `"300m
  pr.(76,2)"` vs `"300m prepone (h=0,762m)"`). This collapses those down to
  one canonical Croatian name so they don't fragment into separate
  `discipline` rows. `DISCIPLINE_RAW_ALIASES` and
  `_AGE_GROUP_DISCIPLINE_OVERRIDES` are hand-verified one-off fixes found
  by cross-checking real data, not guesses — if you add a new one, check
  real co-occurrence in the data first, the same way these were found.
- **`_resolve_club(club, location)`** — some rows (mostly older
  international results) have no club field in the source at all; the row
  regex's club group isn't optional, so it greedily eats the first word of
  what's actually the location. This heuristic (real club codes are always
  uppercase) catches the common case and routes the athlete to `IND`
  instead. It doesn't catch every case (documented in a comment right
  there) — `build_db.py`'s club-code validation against the real scraped
  club list is the backstop for whatever slips through.
- **`parse_clubs_page(path)`** — parses the has.hr club directory HTML into
  `(code, name, city)` tuples.

## 3. Scoring — `wa_scoring.py`, `score_agr_2025.py`

World Athletics scores marks using **lookup tables**, not a formula (a PDF
of tables ships in the repo). `wa_scoring.py`:

- `build_tables()` / `load_tables()` — parse the WA scoring tables PDF into
  `{gender: {column_name: {mark_value: points}}}`, cached to
  `wa_scoring_cache.json` so the PDF isn't re-parsed every run.
- `Scorer` — wraps those tables; `Scorer.score(gender, column, mark_value)`
  finds the right bracket and returns points, using the WA rule "if the
  mark falls between two tabulated rows, use the lower score."
- `HIGHER_IS_BETTER_COLUMNS` — which WA columns are jumps/throws (bigger
  mark = better) vs. everything else (smaller = better, i.e. times).

`score_agr_2025.py`'s `resolve_column(discipline, gender, age_group,
indoor)` maps a has.hr discipline name to the right WA table column (e.g.
"100m" → the right column for that gender/age group), including the
Pentathlon age-group exception (only scored for `ss`/`ms`). `build_db.py`
imports this directly rather than duplicating the mapping.

## 4. The database — `build_db.py`

This is the core ETL. Run standalone: `python build_db.py` (rebuilds
`athletics.db` from scratch from whatever's currently in `tablice_sezone/`).

### Schema

```
season(id, year)
agegroup(id, name, short_name, tech_name)        -- ss/ms/jj/mj/dd/md
club(id, name, short_name, city)
athlete(id, name_key, name, female, birth_year, birthday)
athlete_alias(id, athlete_id, name_variant, source_year)
clubathlete(id, athlete_id, club_id, season_id)   -- one club per athlete per season
discipline(id, name, short_name, info, indoor, female, standard, higher_is_better)
wapoint(id, discipline_id, mark, mark_value, points)  -- WA scoring lookup rows
race(id, season_id, discipline_id, wind, wind_assisted, info, mark, mark_value,
     city, date, wa_points, is_club_team)
raceathlete(id, race_id, athlete_id, agegroup_id, rank, season_best)
```

The two design decisions most worth understanding if you extend this:

- **One physical result = one `race` row, however many age-group lists it
  appears in.** has.hr republishes the same result once per age group a
  participant qualifies for (e.g. a junior's result shows up in both the
  `jj` and `mj` files). `Loader.load_row`'s `race_cache` keys on
  `(discipline, season, athlete/team, raw mark, date, location)` and
  reuses the existing `race` row when it's seen again, only adding a new
  `raceathlete` row for the new age group + rank. That's why
  `raceathlete`'s uniqueness is `(race_id, athlete_id, agegroup_id)` rather
  than just `(race_id, athlete_id)`.
- **`clubathlete` is season-scoped, not per-race.** has.hr never shows
  mid-season club changes, so "which club was this athlete on in season
  X" is a single fact, derived once from whichever club code their results
  that season carry (`note_club_membership`, first-seen wins with a
  warning on conflict — real conflicts do happen, e.g. genuine indoor/
  outdoor club differences or mid-window transfers).

Other things worth knowing:

- **Club resolution** (`resolve_club_code`): any club code not present in
  the real scraped `tablice_sezone/klubovi/` directory is treated as
  unattached (`IND`) — covers defunct/renamed clubs and the parser
  artifact above uniformly, validated against real data rather than a
  heuristic guess.
- **`is_club_team`**: relay team names starting with `REPREZENTACIJA` /
  `REPR.` / `REPREZ.` are national/representative teams, not club teams —
  matters for club records (a relay run for Croatia shouldn't count as a
  club record).
- **`season_best`** lives on `raceathlete`, not `race` — a relay race is
  shared by multiple athletes, and it can be one teammate's season-best
  while not being another's. Computed once at the end (
  `compute_season_best`) by grouping `raceathlete` rows by
  `(athlete, discipline, season)` and flagging the best `mark_value` per
  group, direction-aware via `discipline.higher_is_better`.
- **`build()`** processes seasons newest → oldest and stops the first time
  a season has zero results for club `AGR` — that's the boundary where
  historical data stops being useful for this dataset.

## 5. The API — `api.py`

A thin **read-only** FastAPI layer directly over `athletics.db` — no ORM,
plain SQL, one `sqlite3` connection opened and closed per request
(`mode=ro` — the API never writes). Endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /health` | liveness check, used by Render |
| `GET /seasons` | list of years with data, powers the frontend's year dropdowns |
| `GET /athletes/search?name=&exact=` | resolve a name (or partial name) to athlete id(s), matching current name or any past alias |
| `GET /athletes/{id}/career` | full result history for one athlete |
| `GET /athletes/{id}/{year}` | one athlete, one season |
| `GET /clubs/{short_name}/statistics?year=` | roster size, races entered, podiums, WA points for a club/season |
| `GET /clubs/{short_name}/records?year=` | best mark per discipline+gender for a club's roster; omit `year` for all-time |

Run locally: `uvicorn api:app --reload` (defaults to `http://127.0.0.1:8000`).

CORS is wide open (`allow_origins=["*"]`) since this is public read-only
data with no auth — simplest option, revisit only if that stops being true.

## 6. The frontend — `docs/`

Plain HTML/CSS/JS, no build step, no framework — deliberately, since it's
four endpoints and two views. Served as-is by GitHub Pages from the
`docs/` folder.

- **`index.html`** — two panels: athlete search and club search.
- **`app.js`** — all the logic. Worth knowing:
  - `API_BASE_URL` at the top points at the deployed Render URL. Change
    this if you ever move hosts.
  - `api(path)` is the single fetch wrapper everything goes through. It
    also drives the "waking up the server" banner: Render's free tier
    sleeps after ~15 min idle, so a cold request can take up to a minute.
    The banner only appears if a request is still pending after 2 seconds
    (`wakeupTimer`), so normal warm requests show nothing.
  - Athlete flow: search → click a match → `loadAthleteCareer()` fetches
    results, populates the discipline dropdown, draws a Chart.js line
    chart of `mark_value` over time for the selected discipline, and fills
    the results table.
  - Club flow: type a code (+ optional year) → fetches stats (only if a
    year is picked — "statistics" is inherently season-scoped) and records
    (works with or without a year) in parallel.
- **`style.css`** — minimal, no framework.

## 7. Hosting

- **API → Render** (`Dockerfile`, `render.yaml`, free plan): the
  `Dockerfile` copies `api.py` + `athletics.db` into a slim Python image
  and runs `uvicorn` bound to Render's `$PORT`. `render.yaml` sets
  `autoDeploy: true`, so any push to the connected branch rebuilds and
  redeploys automatically — including the weekly bot commit. Free-tier
  tradeoff: it sleeps when idle (see the banner above).
- **Frontend → GitHub Pages**, serving `docs/` directly, also rebuilds
  automatically on push. No separate build step needed since there's no
  bundler.

## 8. Local dev loop

```
pip install -r requirements.txt

# refresh data (only needed if you want newer results than what's committed)
python downloader.py
python build_db.py

# run the API
uvicorn api:app --reload

# serve the frontend, pointing app.js's API_BASE_URL at localhost first
cd docs && python -m http.server 8080
```

## 9. Where to extend things

- **New discipline name variant showing up as a duplicate?** Check real
  co-occurrence in the data first (same approach used throughout
  `parse_tables.py`), then add to `DISCIPLINE_RAW_ALIASES` or
  `_AGE_GROUP_DISCIPLINE_OVERRIDES`.
- **New export/query?** Follow the existing `api.py` pattern: open a
  read-only connection, plain SQL joining from `raceathlete`/`race`
  outward, close in a `finally`. Add the route, then a matching fetch +
  render function in `app.js`.
- **New page/view?** `docs/` has no router — add a new section to
  `index.html` and wire it up the same way the existing two panels are
  wired at the bottom of `app.js`.
- **Known gap, not yet fixed**: `_resolve_club`'s uppercase-heuristic in
  `parse_tables.py` doesn't catch every "location leaked into club field"
  case (specifically bilingual locations like `"Rim / Rome"` where the
  location field isn't literally empty) — mitigated at build time by the
  club-code validation, but the `location` text itself isn't restored for
  that shape of row.
