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
  - a line mentioning "vjetra" / "wind assisted" flags subsequent rows as wind-assisted until the
    next event header.
  - anything else (blank lines, "(10)" rank markers, decathlon per-discipline detail lines,
    banner text) is skipped.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

FILENAME_RE = re.compile(r'^(ss|ms|jj|mj|dd|md)(m|w)(\d{2})(d)?\.(html?|txt)$', re.IGNORECASE)

AGE_GROUP_NAMES = {
    'ss': 'Seniori',
    'ms': 'Mlađi seniori',
    'jj': 'Juniori',
    'mj': 'Mlađi juniori',
    'dd': 'Kadeti',
    'md': 'Mlađi kadeti',
}

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

RELAY_LEG_RE = re.compile(r'([^,()]+?)\s*\((\d{4})\)')

RANK_MARKER_RE = re.compile(r'^\s*\(\s*\d+\s*\)\s*$')

TAG_RE = re.compile(r'<[^>]+>')


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
    is_relay: bool
    name: str | None
    birth_year: int | None
    birth_date: str | None
    club_code: str | None
    relay_team_name: str | None
    location: str
    perf_date: str | None
    source_file: str
    relay_legs: list = field(default_factory=list)  # list of (name, birth_year)


def _to_iso(date_str):
    if not date_str:
        return None
    day, month, year = date_str.split('.')
    return f"{year}-{month}-{day}"


def _infer_century(two_digit_year, file_year):
    threshold = file_year % 100
    yy = int(two_digit_year)
    return 2000 + yy if yy <= threshold else 1900 + yy


def _parse_birth(birth_raw, file_year):
    if len(birth_raw) == 4:
        return int(birth_raw), None
    day, month, year = birth_raw.split('.')
    return int(year), _to_iso(birth_raw)


def normalize_mark(mark_raw):
    if mark_raw is None:
        return None
    if ':' in mark_raw:
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


def _parse_header(line):
    if ' - ' not in line:
        return None
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
    """Yields tag-stripped plain-text lines for the standard <pre> era files."""
    raw = path.read_bytes().decode('cp1250', errors='replace')
    if '<pre>' in raw.lower():
        soup = BeautifulSoup(raw, 'html.parser')
        pre = soup.find('pre')
        text = pre.get_text('\n') if pre else soup.get_text('\n')
    else:
        text = TAG_RE.sub('', raw)
    for line in text.split('\n'):
        yield line


def parse_standard_file(path):
    """Parses a regular-era file (2002 .txt through 2026 .html/.htm)."""
    filename = path.name
    m = FILENAME_RE.match(filename)
    if not m:
        raise ValueError(f"Unrecognized filename pattern: {filename}")
    age_group_code, file_gender, _yy, indoor_flag, _ext = m.groups()
    file_gender = 'F' if file_gender.lower() == 'w' else 'M'
    indoor = bool(indoor_flag)
    year = int(path.parent.name)

    rows = []
    category_label = AGE_GROUP_NAMES[age_group_code]
    gender = file_gender
    discipline = None
    rank = 0
    wind_assisted = False
    last_relay_row = None

    for raw_line in _iter_plain_lines(path):
        line = raw_line.rstrip()
        if not line.strip():
            continue

        if RANK_MARKER_RE.match(line):
            continue

        header = None
        first_token = line.strip().split(None, 1)[0]
        starts_numeric = bool(re.match(r'^[\d,:]+$', first_token))

        if not starts_numeric and ' - ' in line:
            header = _parse_header(line)

        if header is not None:
            category_label, header_gender, discipline = header
            gender = header_gender or file_gender
            rank = 0
            wind_assisted = False
            last_relay_row = None
            continue

        if not starts_numeric:
            lower = line.lower()
            if 'vjetra' in lower or 'wind assisted' in lower:
                wind_assisted = True
                rank = 0
                continue
            leg_matches = RELAY_LEG_RE.findall(line)
            if leg_matches and last_relay_row is not None:
                for leg_name, leg_year in leg_matches:
                    last_relay_row.relay_legs.append((leg_name.strip(), int(leg_year)))
            continue

        if discipline is None:
            continue

        row_match = ROW_RE.match(line)
        if row_match:
            gd = row_match.groupdict()
            birth_year, birth_date = _parse_birth(gd['birth'], year)
            rank += 0 if wind_assisted else 1
            rows.append(ParsedRow(
                age_group_code=age_group_code,
                age_group_name=category_label,
                gender=gender,
                indoor=indoor,
                year=year,
                discipline=discipline,
                rank=None if wind_assisted else rank,
                mark_raw=gd['mark'],
                wind=gd['wind'],
                wind_assisted=wind_assisted,
                is_relay=False,
                name=gd['name'].strip(),
                birth_year=birth_year,
                birth_date=birth_date,
                club_code=gd['club'],
                relay_team_name=None,
                location=gd['location'].strip(),
                perf_date=_to_iso(gd['date']),
                source_file=str(path),
            ))
            last_relay_row = None
            continue

        relay_match = RELAY_RE.match(line)
        if relay_match:
            gd = relay_match.groupdict()
            rank += 0 if wind_assisted else 1
            new_row = ParsedRow(
                age_group_code=age_group_code,
                age_group_name=category_label,
                gender=gender,
                indoor=indoor,
                year=year,
                discipline=discipline,
                rank=None if wind_assisted else rank,
                mark_raw=gd['mark'],
                wind=None,
                wind_assisted=wind_assisted,
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

    for tr in soup.find_all('tr'):
        cells = [td.get_text(' ', strip=True) for td in tr.find_all('td')]
        if not cells:
            continue
        if len(cells) == 1:
            text = cells[0]
            if text and not text.lower().startswith('rezultat'):
                discipline = re.sub(r'\s+', ' ', text).strip()
                rank = 0
            continue
        if len(cells) < 6:
            continue
        mark, name, born, club, venue, date = cells[:6]
        if mark.lower().startswith('rezultat') or born.lower().startswith('ro'):
            continue
        if not re.match(r'^[\d,:]+$', mark):
            continue
        if discipline is None:
            continue
        birth_year = _infer_century(born, year) if born.strip().isdigit() else None
        rank += 1
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
            is_relay=False,
            name=re.sub(r'\s+', ' ', name).strip(),
            birth_year=birth_year,
            birth_date=None,
            club_code=club.strip(),
            relay_team_name=None,
            location=venue.strip(),
            perf_date=_to_iso(date.strip()) if re.match(r'^\d{2}\.\d{2}\.\d{4}$', date.strip()) else None,
            source_file=str(path),
        ))

    return rows


def parse_file(path):
    path = Path(path)
    if path.name == 'TMIn02.htm':
        return parse_legacy_indoor_2002(path, 'M')
    if path.name == 'TWIn02.htm':
        return parse_legacy_indoor_2002(path, 'F')
    return parse_standard_file(path)
