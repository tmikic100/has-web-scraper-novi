"""
Parses has.hr "tablice sezone" season-table files (downloaded by downloader.py into
tablice_sezone/<year>/<file>) into structured rows ready for loading into a SQL database.

Three file generations are handled:
  - 2003-2005 .htm and 2002 .txt: <pre>-style text rows, birth field is a 4-digit year.
  - 2006-2026 .htm/.html: <pre>-style text rows, birth field is a full DD.MM.YYYY date.
  - 2002 TMIn02.htm / TWIn02.htm: legacy raw <table><tr><td> markup (seniors only, indoor),
    birth field is a 2-digit year.

Row classification is done on tag-stripped plain text lines using the fields present rather
than fixed column offsets, since padding/column widths drift across eras and long names or
locations can overflow into the next field:
  - a line whose first token is numeric and which ends in a birth field (year or date) is an
    individual result row.
  - a line whose first token is numeric but has no birth field is a relay/team result row.
  - a <font style='font-size:7.0pt'>...</font> sub-line containing "Name SURNAME (YYYY)" chunks
    lists the leg runners of the most recent relay result.
  - a non-numeric line containing " - " is a new event header ("Category / Gender - Discipline").
  - a line matching one of CONDITION_FLAG_PHRASES (each a "Croatian phrase / English phrase" pair,
    e.g. "vjetra / Wind assisted", "neregularni uvjeti / irregular conditions") flags subsequent
    rows as excluded from ranking until the next event header -- the mark itself is still real and
    worth keeping/displaying, but shouldn't count towards national/world/europe ranking since its
    legality can't be confirmed (or, for "izvan stadiona", it wasn't run under normal conditions).
    "vjetra"/"wind assisted" additionally sets wind_assisted specifically (kept as its own field
    since it's informational, not just a ranking-eligibility flag).
  - anything else (blank lines, "(10)" rank markers, decathlon per-discipline detail lines,
    banner text) is skipped.
"""

# re: regular expressions -- pattern matching used throughout this file to
#     pick apart loosely-structured text rows into named fields.
import re
# dataclass is a decorator that auto-generates the boring boilerplate (an
# __init__ that assigns every field, a readable __repr__, equality
# comparison, ...) for a class that's mainly just a bag of named fields --
# see the ParsedRow class below. `field` lets us customize one specific
# field's default value (used for the mutable relay_legs list).
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

# --- Regular expressions used across this file -------------------------
# A quick primer, since this file leans heavily on regex:
#   ^ / $        start / end of the string (or line, with re.MULTILINE)
#   \s+          one or more whitespace characters
#   \d           a single digit; \d{2} means "exactly two digits"
#   (?P<name>…)  a "named capture group" -- after a match, you can pull out
#                just that piece via match.group('name') or match.groupdict()
#   (?:…)        a "non-capturing group" -- groups characters together for
#                `?`/`+`/`*` without creating a named/numbered capture
#   ?            makes the preceding item optional (zero or one)
#   .+?          "one or more of any character, but as few as possible"
#                (a *non-greedy* match, so it stops at the first place the
#                rest of the pattern can also match, instead of grabbing as
#                much text as possible)

# FILENAME_RE picks apart a season-table filename like "ssm26.html" into its
# meaningful pieces: age group code, gender letter, 2-digit year, an
# optional "d" (indoor/"dvorana") flag, and the file extension.
FILENAME_RE = re.compile(r'^(ss|ms|jj|mj|dd|md)(m|w)(\d{2})(d)?\.(html?|txt)$', re.IGNORECASE)

# Each pair is matched via a lowercase substring check against a line (same mechanism the
# original wind-assisted-only detection used) -- confirmed against the raw source data (see
# conversation) as the full set of "Croatian phrase / English phrase" condition-flag lines that
# appear in the season tables. WIND is kept as its own name since it also sets wind_assisted.
CONDITION_FLAG_PHRASES = {
    'WIND': ('vjetra', 'wind assisted'),
    'IRREGULAR_CONDITIONS': ('neregularni uvjeti', 'irregular conditions'),
    'NO_WIND_INFO': ('nema podataka o vjetru', 'no wind information'),
    'OUT_OF_STADIUM': ('izvan stadiona', 'out of stadium'),
}

AGE_GROUP_NAMES = {
    'ss': 'Seniori',
    'ms': 'Mlađi seniori',
    'jj': 'Juniori',
    'mj': 'Mlađi juniori',
    'dd': 'Kadeti',
    'md': 'Mlađi kadeti',
}

# re.VERBOSE mode (below) lets us spread a regex across multiple lines with
# comments and insignificant whitespace, purely for readability -- without
# it, all of this would have to be one dense unbroken line. ROW_RE matches
# one individual-result data line and pulls out its fields by name.
ROW_RE = re.compile(r'''
    ^\s*
    (?P<mark>[\d,:]+)
    \s+
    (?:(?P<wind>[+-]?\d,\d)\s+)?
    (?P<name>.+?)
    \s+
    (?P<birth>\d{2}\.\d{2}\.\d{4}|\d{4})
    \s+
    (?P<club>\S+)
    \s+
    (?P<location>.*?)
    \s*
    (?P<date>\d{2}\.\d{2}\.\d{4})
    \s*$
''', re.VERBOSE)

# RELAY_RE is the equivalent pattern for a relay/team result line -- no
# individual name/birth/club fields, just a mark, the team name, and where
# and when it happened. The two-or-more-spaces (`\s{2,}`) between the team
# name and location is what lets this tell "Team Name" apart from a location
# that might itself contain single spaces.
RELAY_RE = re.compile(r'''
    ^\s*
    (?P<mark>[\d,:]+)
    \s+
    (?P<team>.+?)
    \s{2,}
    (?P<location>.*?)
    \s*
    (?P<date>\d{2}\.\d{2}\.\d{4})
    \s*$
''', re.VERBOSE)

# Matches one "Name SURNAME (YYYY)" chunk from a relay's leg-runner sub-line,
# e.g. "Ivan Horvat (1998)" -> captures "Ivan Horvat" and "1998" separately.
RELAY_LEG_RE = re.compile(r'([^,()]+?)\s*\((\d{4})\)')

# Matches a standalone "(10)" style rank marker line, which carries no real
# data and should just be skipped.
RANK_MARKER_RE = re.compile(r'^\s*\(\s*\d+\s*\)\s*$')

# Matches any HTML tag (e.g. "<b>", "</font>") so it can be stripped out,
# leaving just the tag's text content.
TAG_RE = re.compile(r'<[^>]+>')


def match_condition_flag(lower_line):
    """Returns the CONDITION_FLAG_PHRASES key matched by this (already-lowercased) line, or
    None. Checked against non-numeric lines the same way the original "vjetra"-only check was."""
    # .items() iterates a dict's (key, value) pairs together; here each value
    # is itself a 2-tuple, immediately unpacked into `cro` and `eng`.
    for name, (cro, eng) in CONDITION_FLAG_PHRASES.items():
        if cro in lower_line or eng in lower_line:
            return name
    return None


# @dataclass auto-generates __init__, __repr__, and __eq__ for this class
# based purely on the type-annotated fields listed below -- so you get a
# usable `ParsedRow(age_group_code=..., gender=..., ...)` constructor
# without writing `def __init__(self, ...): self.x = x` by hand for every
# single field. The `type | None` syntax (e.g. `int | None`) means "an int,
# or None" -- Python's modern shorthand for Optional[int].
@dataclass
class ParsedRow:
    age_group_code: str
    age_group_name: str
    gender: str
    indoor: bool
    year: int
    discipline: str
    rank: int | None
    mark_raw: str
    wind: str | None
    wind_assisted: bool
    exclude_from_ranking: bool
    is_relay: bool
    name: str | None
    birth_year: int | None
    birth_date: str | None
    club_code: str | None
    relay_team_name: str | None
    location: str
    perf_date: str | None
    source_file: str
    # Using a plain `relay_legs: list = []` default would be a classic Python
    # bug: that ONE list object would be shared and mutated across every
    # ParsedRow instance, since default values are only created once when
    # the class is defined, not per-instance. `field(default_factory=list)`
    # tells the dataclass to call `list()` fresh for every new ParsedRow
    # instead, giving each one its own independent empty list.
    relay_legs: list = field(default_factory=list)  # list of (name, birth_year)


def _to_iso(date_str):
    """Converts a "DD.MM.YYYY" date string (as printed by has.hr) into ISO
    "YYYY-MM-DD" format (what SQLite/most databases expect for date
    comparisons/sorting to work correctly)."""
    if not date_str:
        return None
    day, month, year = date_str.split('.')
    return f"{year}-{month}-{day}"

def _infer_century(two_digit_year, file_year):
    """Guesses the full year from a 2-digit year (only used by the 2002
    legacy indoor file, which prints birth years as e.g. "85" instead of
    "1985"). Anything up to the file's own 2-digit year is assumed to be a
    2000s birth year (a competitor can't be born after the file's own
    season); anything higher is assumed to be 1900s."""
    threshold = file_year % 100
    yy = int(two_digit_year)
    return 2000 + yy if yy <= threshold else 1900 + yy


def _parse_birth(birth_raw, file_year):
    """Handles both birth-field formats: a bare 4-digit year (older files)
    or a full DD.MM.YYYY date (newer files). Always returns a
    (birth_year, birth_date_or_None) pair, so callers don't need to know
    which format the source file actually used."""
    if len(birth_raw) == 4:
        return int(birth_raw), None
    day, month, year = birth_raw.split('.')
    return int(year), _to_iso(birth_raw)


def normalize_mark(mark_raw):
    """Converts has.hr's Croatian-formatted mark text (comma as decimal
    separator, "M:SS,ss" for timed events) into a plain float, or None if it
    can't be parsed. E.g. "10,49" -> 10.49, "1:02,15" -> 62.15."""
    if mark_raw is None:
        return None
    if ':' in mark_raw:
        # "M:SS,ss" or "H:MM:SS,ss" -- same minutes/hours-to-seconds
        # conversion pattern used in wa_scoring._mark_to_float and
        # wa_ranking.parse_mark_value, just with a comma decimal here
        # instead of a period.
        parts = mark_raw.split(':')
        try:
            seconds = float(parts[-1].replace(',', '.'))
            for i, part in enumerate(reversed(parts[:-1])):
                seconds += int(part) * (60 ** (i + 1))
            return seconds
        except ValueError:
            return None
    if ',' in mark_raw:
        try:
            return float(mark_raw.replace(',', '.'))
        except ValueError:
            return None
    try:
        return float(mark_raw)
    except ValueError:
        return None


# has.hr discipline names accumulated 24 years of inconsistent formatting for the *same*
# event: bilingual "Croatian / English" text where only the English side drifted (typos,
# abbreviations), thousands written with a dot ("1.000 m"), and relay names sometimes
# missing their "m" suffix. DISCIPLINE_RAW_ALIASES below fixes a handful of one-off
# malformed/abbreviated raw strings (confirmed against the real DB: each maps to an
# established discipline that continues using the well-formed name in adjacent years) before
# the general normalization runs. Genuinely different specs (e.g. different hurdle heights,
# different implement weights) are deliberately NOT merged -- confirmed empirically by
# checking whether two similar-looking names ever share the same age group across years;
# see the migration conversation for the specific checks run.
DISCIPLINE_RAW_ALIASES = {
    '300 m pr.(76,2) / 300 m H.(76,2)':
        '300 m prepone (h=0,762m) / 300 m Hurdles (h=0,762m)',
    '100 m p.(76,2)D / 100 m H.(76,2)G':
        '100 m prepone (h=0,762m) / 100 m Hurdles (h=0,762m)',
    '100 m prepone (h=0,762m/d=8,5m / 100 m Hurdles (h=0,762m/d=8,5)':
        '100 m prepone (h=0,762m/d=8,5m) / 100 m Hurdles (h=0,762m/d=8,5m)',
    '100 m prepone (h=0,762m/d=8,5m / 100 m Hurdles (h=0,762m/d=8,5m)':
        '100 m prepone (h=0,762m/d=8,5m) / 100 m Hurdles (h=0,762m/d=8,5m)',
}

# 'kadeti' (dd) always uses the h=0,762m/d=8m spec for 60mH in every other year -- their one
# 2008 file just omitted the annotation entirely, unlike 'jj' whose 2008 bare form genuinely
# continues as the long-running unannotated senior-track name in later years. Age-group-aware
# because the raw text ("60 m prepone") is identical for both and only the age group
# disambiguates which established discipline it belongs to.
_AGE_GROUP_DISCIPLINE_OVERRIDES = {
    ('60 m prepone', 'dd'): '60 m prepone (h=0,762m/d=8m)',
}


def _split_croatian_side(raw):
    """Splits off the Croatian (left) side at the '/' that separates the bilingual name --
    NOT any '/' nested inside a parenthetical annotation like '(h=0,762m/d=8,25m)'."""
    # A hand-written mini-parser rather than a single regex: we walk the
    # string character by character, tracking how many '(' we're currently
    # nested inside (`depth`). Only a '/' seen at depth 0 (i.e. not inside
    # any parentheses) counts as the real Croatian/English separator.
    depth = 0
    for i, ch in enumerate(raw):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
        elif ch == '/' and depth == 0:
            return raw[:i].strip()
    return raw.strip()


def _canonicalize_hd_annotation(match):
    """A "replacement function" for re.sub (see normalize_discipline_name
    below): instead of a fixed replacement string, re.sub can call a
    function with the regex Match object and use whatever it returns --
    handy here since we want to rebuild the annotation from its two
    captured numbers rather than do a fixed text substitution."""
    return f'(h={match.group(1)}m/d={match.group(2)}m)'


def normalize_discipline_name(raw, age_group_code=None):
    """Turns a raw has.hr discipline string (bilingual, inconsistently
    formatted) into one canonical Croatian-only name used throughout the
    rest of the codebase as the `discipline.name` column."""
    override = _AGE_GROUP_DISCIPLINE_OVERRIDES.get((raw, age_group_code))
    if override:
        return override

    raw = DISCIPLINE_RAW_ALIASES.get(raw, raw)
    name = _split_croatian_side(raw)
    name = re.sub(r'\s+', ' ', name).strip()
    # thousands-dot (Croatian formatting): "1.000 m" -> "1000 m" (heights/weights use comma
    # as the decimal separator, e.g. "0,762m", so a dot here is unambiguously a thousands sep)
    # `(?<=\d)` and `(?=\d{3}\b)` are lookbehind/lookahead assertions: they
    # require a digit before and exactly-3-digits-then-a-word-boundary after
    # the matched '.', WITHOUT consuming those characters (so they aren't
    # removed/replaced themselves, just checked for).
    name = re.sub(r'(?<=\d)\.(?=\d{3}\b)', '', name)
    # relay compounds: "4 x 200" / "4 x 200 m" -> "4x200m"
    if re.match(r'^\d+(\s*x\s*\d+)+', name, re.IGNORECASE) or re.match(r'^[\d-]+\s*m?$', name):
        name = re.sub(r'\s*x\s*', 'x', name, flags=re.IGNORECASE)
        name = re.sub(r'(\d)\s+m\b', r'\1m', name)
        if re.search(r'\d$', name):
            name += 'm'
    # hurdle height/distance-to-first-hurdle annotation: normalize "m" suffix presence only
    # -- the numeric values themselves are preserved as-is, since different d= values are
    # confirmed-real distinct specs (e.g. dd/F 100mH genuinely uses both d=8m and d=8,25m
    # across different years), not formatting noise.
    name = re.sub(r'\(h=([\d,]+)m?/d=([\d,]+)m?\)', _canonicalize_hd_annotation, name)
    return name


def _resolve_club(club, location):
    """Some rows (mostly elite results at international meets) have no club field at all in
    the source -- ROW_RE's club group is not optional, so it greedily grabs the first token
    of what is actually the location, leaving location empty (confirmed against raw source,
    e.g. "1,88  Ana Šimić  05.05.1990  Belem  06.05.2012" -- no club column, "Belem" is the
    venue). Real club codes are always all-uppercase; a non-uppercase "club" paired with an
    empty location is the tell that this happened, so route it to IND and restore the
    location text instead of keeping the bogus club code."""
    if location == '' and club and not club.isupper():
        return 'IND', club
    return club, location


def _parse_header(line):
    """Parses an event-header line like "Women / Seniori - 100 m" into
    (category_label, gender_code, discipline_raw_name), or returns None if
    the line doesn't look like a header at all (no " - " separator)."""
    if ' - ' not in line:
        return None
    # str.split(' - ', 1) splits on only the FIRST occurrence of " - "
    # (the `1` = "at most 1 split"), so a discipline name that itself
    # happens to contain " - " further along doesn't get split again.
    left, discipline = line.split(' - ', 1)
    discipline = discipline.strip()
    if 'Women' in left or 'Girls' in left:
        gender = 'F'
    elif 'Mixed' in left:
        gender = 'X'
    elif 'Men' in left or 'Boys' in left:
        gender = 'M'
    else:
        gender = None
    category_label = left.split('/')[0].strip()
    return category_label, gender, discipline


def _iter_plain_lines(path):
    """Yields tag-stripped plain-text lines for the standard <pre> era files.

    This is a *generator function* -- because it uses `yield` instead of
    `return`, calling `_iter_plain_lines(path)` doesn't run the function
    body immediately; it hands back a lazy iterator that produces one line
    at a time as the caller's `for line in _iter_plain_lines(path):` loop
    asks for the next one. This avoids holding the entire file's line list
    in memory all at once (not that it would matter much for these
    file sizes, but it's a common, idiomatic pattern worth recognizing)."""
    # These files aren't UTF-8 -- has.hr serves them in the older Central
    # European "cp1250" encoding, so we must decode with that codec or
    # Croatian diacritics (č, ć, ž, š, đ) would come out garbled.
    # errors='replace' swaps any byte that still doesn't decode cleanly for
    # a placeholder character instead of crashing the whole parse.
    raw = path.read_bytes().decode('cp1250', errors='replace')
    if '<pre>' in raw.lower():
        soup = BeautifulSoup(raw, 'html.parser')
        pre = soup.find('pre')
        # get_text('\n') joins all the text inside the tag using '\n' as the
        # separator between pieces, effectively "flattening" the HTML into
        # plain text while preserving line breaks.
        text = pre.get_text('\n') if pre else soup.get_text('\n')
    else:
        # No <pre> block at all -- just strip out any HTML tags directly
        # with a regex substitution instead of going through BeautifulSoup.
        text = TAG_RE.sub('', raw)
    for line in text.split('\n'):
        yield line


def parse_standard_file(path):
    """Parses a regular-era file (2002 .txt through 2026 .html/.htm)."""
    filename = path.name
    m = FILENAME_RE.match(filename)
    if not m:
        raise ValueError(f"Unrecognized filename pattern: {filename}")
    # .groups() returns every captured group as a tuple in order; unpacked
    # directly into five variables. A leading underscore on `_yy`/`_ext`
    # signals "captured but intentionally unused here" (that information is
    # already available elsewhere -- the year from the containing folder
    # name, the extension isn't needed at all).
    age_group_code, file_gender, _yy, indoor_flag, _ext = m.groups()
    file_gender = 'F' if file_gender.lower() == 'w' else 'M'
    # indoor_flag is either the literal string "d" or None (since it was an
    # optional capture group); bool(None) is False and bool("d") is True,
    # so this converts it into a proper True/False flag.
    indoor = bool(indoor_flag)
    year = int(path.parent.name)  # the file lives in tablice_sezone/<year>/

    rows = []
    # These variables track "current parsing state" as we walk down the
    # file line by line -- they get updated by header lines and condition
    # flags, and are then used when building each ParsedRow.
    category_label = AGE_GROUP_NAMES[age_group_code]
    gender = file_gender
    discipline = None
    rank = 0
    wind_assisted = False
    excluded = False
    last_relay_row = None  # the most recently appended relay ParsedRow, if any

    for raw_line in _iter_plain_lines(path):
        line = raw_line.rstrip()  # drop trailing whitespace/newline only
        if not line.strip():
            continue  # blank line

        if RANK_MARKER_RE.match(line):
            continue  # a standalone "(10)" marker, no data here

        header = None
        # .split(None, 1) splitting on `None` means "split on any run of
        # whitespace" (like str.split() with no arguments), and the `1`
        # again means "at most one split" -- so this grabs just the first
        # whitespace-separated token off the front of the line.
        first_token = line.strip().split(None, 1)[0]
        starts_numeric = bool(re.match(r'^[\d,:]+$', first_token))

        if not starts_numeric and ' - ' in line:
            header = _parse_header(line)

        if header is not None:
            # A new event section started -- reset all the per-event state
            # that shouldn't carry over from the previous discipline.
            category_label, header_gender, discipline = header
            gender = header_gender or file_gender
            rank = 0
            wind_assisted = False
            excluded = False
            last_relay_row = None
            continue

        if not starts_numeric:
            lower = line.lower()
            flag = match_condition_flag(lower)
            if flag is not None:
                if flag == 'WIND':
                    wind_assisted = True
                excluded = True
                rank = 0
                continue
            # Not a header, not a condition flag -- check whether this is a
            # relay leg-runner sub-line instead (only meaningful right after
            # a relay result row, hence checking last_relay_row is set).
            leg_matches = RELAY_LEG_RE.findall(line)
            if leg_matches and last_relay_row is not None:
                for leg_name, leg_year in leg_matches:
                    last_relay_row.relay_legs.append((leg_name.strip(), int(leg_year)))
            continue

        if discipline is None:
            # A numeric-looking line before we've seen any event header at
            # all -- shouldn't normally happen, but skip defensively rather
            # than crash on it.
            continue

        row_match = ROW_RE.match(line)
        if row_match:
            gd = row_match.groupdict()  # {'mark': ..., 'wind': ..., 'name': ..., ...}
            birth_year, birth_date = _parse_birth(gd['birth'], year)
            # Rank only advances for *counted* (non-excluded) results, so an
            # excluded row doesn't consume/shift the rank numbers of the
            # legitimate results around it.
            rank += 0 if excluded else 1
            club_code, location = _resolve_club(gd['club'], gd['location'].strip())
            rows.append(ParsedRow(
                age_group_code=age_group_code,
                age_group_name=category_label,
                gender=gender,
                indoor=indoor,
                year=year,
                discipline=discipline,
                rank=None if excluded else rank,
                mark_raw=gd['mark'],
                wind=gd['wind'],
                wind_assisted=wind_assisted,
                exclude_from_ranking=excluded,
                is_relay=False,
                name=gd['name'].strip(),
                birth_year=birth_year,
                birth_date=birth_date,
                club_code=club_code,
                relay_team_name=None,
                location=location,
                perf_date=_to_iso(gd['date']),
                source_file=str(path),
            ))
            last_relay_row = None
            continue

        relay_match = RELAY_RE.match(line)
        if relay_match:
            gd = relay_match.groupdict()
            rank += 0 if excluded else 1
            new_row = ParsedRow(
                age_group_code=age_group_code,
                age_group_name=category_label,
                gender=gender,
                indoor=indoor,
                year=year,
                discipline=discipline,
                rank=None if excluded else rank,
                mark_raw=gd['mark'],
                wind=None,
                wind_assisted=wind_assisted,
                exclude_from_ranking=excluded,
                is_relay=True,
                name=None,
                birth_year=None,
                birth_date=None,
                club_code=None,
                relay_team_name=gd['team'].strip(),
                location=gd['location'].strip(),
                perf_date=_to_iso(gd['date']),
                source_file=str(path),
            )
            rows.append(new_row)
            # Remember this row so the NEXT line, if it turns out to be a
            # leg-runner sub-line, knows which relay result to attach to.
            last_relay_row = new_row
            continue

        # unrecognized data-ish line (e.g. decathlon per-discipline splits) -- skip
        last_relay_row = None

    return rows


def parse_legacy_indoor_2002(path, gender):
    """Parses the 2002 TMIn02.htm / TWIn02.htm legacy raw-<table> indoor files (seniors only)."""
    raw = path.read_bytes().decode('cp1250', errors='replace')
    soup = BeautifulSoup(raw, 'html.parser')
    year = 2002
    rows = []
    discipline = None
    rank = 0

    # Unlike the <pre>-text files, this legacy format is a real HTML
    # <table>, so we walk it row by row (<tr>) and cell by cell (<td>)
    # instead of regex-matching plain text lines.
    for tr in soup.find_all('tr'):
        cells = [td.get_text(' ', strip=True) for td in tr.find_all('td')]
        if not cells:
            continue
        if len(cells) == 1:
            # A single-cell row is a section/discipline heading (or a
            # "Rezultat..." column-header row we want to ignore).
            text = cells[0]
            if text and not text.lower().startswith('rezultat'):
                discipline = re.sub(r'\s+', ' ', text).strip()
                rank = 0
            continue
        if len(cells) < 6:
            continue
        # Unpack exactly the first 6 cells by position (this legacy table
        # layout is fixed-column, unlike the newer files).
        mark, name, born, club, venue, date = cells[:6]
        if mark.lower().startswith('rezultat') or born.lower().startswith('ro'):
            continue
        if not re.match(r'^[\d,:]+$', mark):
            continue
        if discipline is None:
            continue
        birth_year = _infer_century(born, year) if born.strip().isdigit() else None
        rank += 1
        club_code = club.strip() or 'IND'
        rows.append(ParsedRow(
            age_group_code='ss',
            age_group_name=AGE_GROUP_NAMES['ss'],
            gender=gender,
            indoor=True,
            year=year,
            discipline=discipline,
            rank=rank,
            mark_raw=mark,
            wind=None,
            wind_assisted=False,
            exclude_from_ranking=False,
            is_relay=False,
            name=re.sub(r'\s+', ' ', name).strip(),
            birth_year=birth_year,
            birth_date=None,
            club_code=club_code,
            relay_team_name=None,
            location=venue.strip(),
            perf_date=_to_iso(date.strip()) if re.match(r'^\d{2}\.\d{2}\.\d{4}$', date.strip()) else None,
            source_file=str(path),
        ))

    return rows


def parse_file(path):
    """Entry point used by build_db.py: dispatches to the right parser based
    on the filename, so callers don't need to know about the legacy-2002
    special case themselves."""
    path = Path(path)
    if path.name == 'TMIn02.htm':
        return parse_legacy_indoor_2002(path, 'M')
    if path.name == 'TWIn02.htm':
        return parse_legacy_indoor_2002(path, 'F')
    return parse_standard_file(path)


def parse_clubs_page(path):
    """Parses one page of the has.hr club directory (downloaded by
    downloader.download_clubs into tablice_sezone/klubovi/page_N.html) into a list of
    (code, name, city) tuples. Each club is a card: <h2 class="uk-h3"> has the full name,
    followed by a <p class="uk-text-muted"> with the city and a plain <div> with the code."""
    raw = Path(path).read_text(encoding='utf-8', errors='replace')
    soup = BeautifulSoup(raw, 'html.parser')

    clubs = []
    # soup.find_all(...) returns every matching tag on the page; for each
    # club "card" we navigate sideways from the heading to its sibling tags
    # (find_next_sibling) rather than searching the whole document again.
    for h2 in soup.find_all('h2', class_='uk-h3'):
        name_tag = h2.find('a')
        name = name_tag.get_text(strip=True) if name_tag else h2.get_text(strip=True)

        city_p = h2.find_next_sibling('p', class_='uk-text-muted')
        city = city_p.get_text(strip=True) if city_p else None

        code_div = city_p.find_next_sibling('div') if city_p else None
        code = code_div.get_text(strip=True) if code_div else None

        if code:
            clubs.append((code, name, city or None))
    return clubs
