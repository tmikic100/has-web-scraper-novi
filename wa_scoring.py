"""
Parses the World Athletics Scoring Tables of Athletics (2025 revised edition) PDF into a
mark -> points lookup per gender/event column, and scores a mark against it.

The PDF is a literal lookup table (not a formula): each section page has a header line
("Points <col1> <col2> ...>" or the mirrored "<col1> <col2> ... Points") followed by rows of
"<points> <mark-or-dash for each column>" repeated for the whole page. Column headers and
alternating points-first/points-last layout are section-specific and hardcoded below from a
manual read of the book's table of contents / page samples.

Only the sections actually needed to score standard (senior-spec) individual events are parsed;
relay sections and the table of contents/intro pages are skipped.
"""

# json: read/write Python dicts/lists as text in the JSON format (used here as an
#       on-disk cache, so we don't have to re-parse the whole PDF every run).
# re: regular expressions -- pattern matching/searching in text.
# bisect_left: binary-search helper from the standard library (see Scorer.score below).
# Path: object-oriented filesystem paths (see downloader.py's comments for more on this).
import json
import re
from bisect import bisect_left
from pathlib import Path

# pypdf is a third-party library for reading PDF files -- PdfReader lets us
# pull the raw text out of each page.
from pypdf import PdfReader

PDF_PATH = Path(__file__).parent / "World Athletics Scoring Tables of Athletics (3).pdf"
CACHE_PATH = Path(__file__).parent / "wa_scoring_cache.json"

# (name, gender, printed_toc_page, columns) -- printed_toc_page is the 1-indexed page number
# from the book's own table of contents (title page of that section).
#
# These are just plain Python lists of strings -- each string is one column heading exactly
# as it appears on the PDF page, used both to locate the header line (via regex) and to label
# each column's parsed mark->points table afterwards.
SPRINTS_I_COLS = ['50m', '55m', '60m', '100m', '200m', '200m sh']
SPRINTS_II_COLS = ['300m', '300m sh', '400m', '400m sh', '500m', '500m sh']
MD_I_COLS = ['600m', '600m sh', '800m', '800m sh', '1000m', '1000m sh']
MD_II_COLS = ['1500m', '1500m sh', 'Mile', 'Mile sh', '2000m', '2000m sh', '2000m SC', '3000m SC']
LONG_DIST_COLS = ['3000m', '3000m sh', '2 Miles', '2 Miles sh', '5000m', '5000m sh', '10000m']
ROAD_I_COLS = ['Mile', '5 km', '10 km', '15 km', '10 Miles', '20 km']
ROAD_II_COLS = ['HM', '25 km', '30 km', 'Marathon', '100 km']
RW_ROAD_I_COLS = ['3km W', '5km W', '10km W', '15km W', '20km W']
RW_ROAD_II_COLS = ['HMW', '30km W', '35km W', 'MarW', '50km W']
RW_TRACK_I_COLS = ['3000mW', '5000mW', '10,000mW', '15,000mW']
RW_TRACK_II_COLS = ['20,000mW', '30,000mW', '35,000mW', '50,000mW']

# SECTIONS is a list of tuples describing every section of the PDF, in the order
# they appear in the book. Each tuple is (section name, gender code or None,
# the page number where that section's title page is printed, and the list of
# column headings for that section -- or None for sections we deliberately skip).
SECTIONS = [
    ('SPRINTS_I', 'M', 8, SPRINTS_I_COLS),
    ('SPRINTS_II', 'M', 38, SPRINTS_II_COLS),
    ('HURDLES', 'M', 68, ['50mH', '55mH', '60mH', '110mH', '300mH', '400mH']),
    ('RELAYS', 'M', 98, None),  # skipped, not scored
    ('MD_I', 'M', 128, MD_I_COLS),
    ('MD_II', 'M', 158, MD_II_COLS),
    ('LONG_DIST', 'M', 188, LONG_DIST_COLS),
    ('ROAD_I', 'M', 218, ROAD_I_COLS),
    ('ROAD_II', 'M', 248, ROAD_II_COLS),
    ('RW_ROAD_I', 'M', 278, RW_ROAD_I_COLS),
    ('RW_ROAD_II', 'M', 308, RW_ROAD_II_COLS),
    ('RW_TRACK_I', 'M', 337, RW_TRACK_I_COLS),
    ('RW_TRACK_II', 'M', 367, RW_TRACK_II_COLS),
    ('JUMPS', 'M', 397, ['HJ', 'PV', 'LJ', 'TJ', 'SP', 'DT', 'HT', 'JT', 'Hept. sh', 'Dec.']),
    ('SPRINTS_I', 'F', 427, SPRINTS_I_COLS),
    ('SPRINTS_II', 'F', 457, SPRINTS_II_COLS),
    ('HURDLES', 'F', 487, ['50mH', '55mH', '60mH', '100mH', '300mH', '400mH']),
    ('RELAYS', 'F', 517, None),
    ('MD_I', 'F', 547, MD_I_COLS),
    ('MD_II', 'F', 577, MD_II_COLS),
    ('LONG_DIST', 'F', 607, LONG_DIST_COLS),
    ('ROAD_I', 'F', 637, ROAD_I_COLS),
    ('ROAD_II', 'F', 667, ROAD_II_COLS),
    ('RW_ROAD_I', 'F', 697, RW_ROAD_I_COLS),
    ('RW_ROAD_II', 'F', 727, ['HMW', '30km W', '35 km W', 'MarW', '50km W']),
    ('RW_TRACK_I', 'F', 757, RW_TRACK_I_COLS),
    ('RW_TRACK_II', 'F', 787, RW_TRACK_II_COLS),
    ('JUMPS', 'F', 817, ['HJ', 'PV', 'LJ', 'TJ', 'SP', 'DT', 'HT', 'JT', 'Pent. sh', 'Hept.']),
    ('END', None, 846, None),  # sentinel marking where the last real section ends
]

# events scored on distance/height/points (bigger mark = better); everything else is a time
# (smaller mark = better).
# A `set` (curly braces) is used because all we ever do with this is ask
# "is this column in the set?" -- sets answer that near-instantly no matter
# how many entries they hold, unlike scanning a list one item at a time.
HIGHER_IS_BETTER_COLUMNS = {'HJ', 'PV', 'LJ', 'TJ', 'SP', 'DT', 'HT', 'JT', 'Hept.sh', 'Dec.',
                            'Pent.sh', 'Hept.'}


def norm_col(name):
    """Column names sometimes differ in spacing between the men's/women's pages of the same
    table (e.g. men's '35km W' vs women's '35 km W') -- normalize by dropping spaces so both
    sides key to the same column."""
    return name.replace(' ', '')


def _mark_to_float(mark):
    """Converts a mark as printed in the PDF (e.g. '10.23', '1:02:15', or '-'
    for "not applicable") into a plain float number of seconds/metres, or
    None if it can't be parsed / isn't a real value.

    A leading underscore in a function name (`_mark_to_float`) is a Python
    convention meaning "internal helper, not meant to be used from outside
    this file" -- it's not enforced by the language, just a signal to readers."""
    mark = mark.strip()
    if not mark or mark == '-':
        return None
    mark = mark.replace(',', '')  # thousands separators in column headers don't appear in data
    if ':' in mark:
        # Time written as "H:MM:SS.ss" or "MM:SS.ss" -- split on ':' and
        # convert each piece (hours, minutes, seconds) into total seconds.
        parts = mark.split(':')
        try:
            # The last part is always seconds (possibly with a decimal).
            total = float(parts[-1])
            # `parts[:-1]` is every part except the last (a "slice"); we walk
            # it in reverse so the piece right before seconds is minutes
            # (multiply by 60^1), the one before that is hours (60^2), etc.
            # enumerate(..., ) gives us that reversed order paired with an
            # index i we use as the power of 60.
            for i, part in enumerate(reversed(parts[:-1])):
                total += int(part) * (60 ** (i + 1))
            return total
        except ValueError:
            # Something in there wasn't actually a number -- treat as unparseable.
            return None
    try:
        return float(mark)
    except ValueError:
        return None


def _parse_section_pages(reader, gender, start_idx, end_idx, columns):
    """Returns {column_name: {mark_float: points}} for one section's data pages."""
    # re.escape() makes sure any regex-special characters inside a column
    # name (like the "." in "Hept.") are treated as literal text, not as
    # regex syntax. `\s+` matches "one or more whitespace characters" -- the
    # PDF's extracted text doesn't always have exactly one space between
    # words, so this is more forgiving than matching a literal single space.
    # `header_re_first` matches a header line like "Points 50m 55m 60m ...".
    header_re_first = re.compile(
        r'Points\s+' + r'\s+'.join(re.escape(c) for c in columns)
    )
    # `header_re_last` matches the mirrored layout used on some pages:
    # "50m 55m 60m ... Points" (column names first, "Points" last).
    header_re_last = re.compile(
        r'\s+'.join(re.escape(c) for c in columns) + r'\s+Points'
    )
    # A dict comprehension: build {normalized_column_name: {}} for every
    # column in this section in one line, instead of a multi-line for-loop
    # that does `tables[norm_col(c)] = {}` repeatedly.
    tables = {norm_col(c): {} for c in columns}
    ncols = len(columns)

    # range(start, end+1) walks every page index from start_idx through
    # end_idx *inclusive* (range's own upper bound is normally exclusive,
    # hence the "+ 1").
    for idx in range(start_idx, end_idx + 1):
        # extract_text() can return None for a page with no extractable text;
        # `or ''` falls back to an empty string so the rest of the code
        # doesn't have to special-case None.
        text = reader.pages[idx].extract_text() or ''
        m = header_re_first.search(text)
        points_first = True
        if not m:
            m = header_re_last.search(text)
            points_first = False
        if not m:
            # Neither header pattern matched this page (e.g. a blank page,
            # or a page that's part of the section but has no table on it)
            # -- skip it and move to the next page index.
            continue
        # `m.end()` is the index right after the matched header text, so
        # `text[m.end():]` is everything on the page that comes after the
        # header -- i.e. the actual data rows.
        remainder = text[m.end():]
        # re.findall(r'\S+', ...) splits the remaining text into a flat list
        # of whitespace-separated "tokens" (numbers and dashes), losing all
        # information about which line/row each token was originally on --
        # we reconstruct rows below purely by counting tokens.
        tokens = re.findall(r'\S+', remainder)
        # Each row has one "points" token plus one token per column, so the
        # total token count divided by (ncols + 1) tells us how many
        # complete rows are present (integer division `//` discards any
        # leftover partial row at the end, e.g. running footer text).
        nrows = len(tokens) // (ncols + 1)
        for r in range(nrows):
            # A "slice" `tokens[a:b]` pulls out a sub-list from index a up to
            # (but not including) index b -- here, exactly one row's worth
            # of tokens.
            chunk = tokens[r * (ncols + 1):(r + 1) * (ncols + 1)]
            if points_first:
                # chunk[0] is the first element; chunk[1:] is "everything
                # after the first element" (a slice with no upper bound).
                points_tok, marks = chunk[0], chunk[1:]
            else:
                # chunk[-1] is the last element (negative indices count from
                # the end); chunk[:-1] is "everything except the last".
                points_tok, marks = chunk[-1], chunk[:-1]
            try:
                points = int(points_tok)
            except ValueError:
                # This chunk's "points" token wasn't actually a number (PDF
                # text extraction can be messy) -- skip this row entirely.
                continue
            # zip(columns, marks) pairs each column name with the mark token
            # in the same position, so we can walk both lists together.
            for col, mark in zip(columns, marks):
                val = _mark_to_float(mark)
                col_key = norm_col(col)
                # Keep only the FIRST points value seen for a given mark
                # (the tables are printed in best-to-worst points order, so
                # the first occurrence is the correct/highest one; later
                # duplicate marks, if any, are ignored).
                if val is not None and val not in tables[col_key]:
                    tables[col_key][val] = points
    return tables


def build_tables():
    """Parses the whole PDF from scratch (slow -- this is why load_tables()
    below caches the result to a JSON file instead of calling this every
    time). Returns {'M': {column: {mark: points}}, 'F': {...}}."""
    reader = PdfReader(str(PDF_PATH))
    tables = {'M': {}, 'F': {}}
    # enumerate(SECTIONS) gives us both the index `i` and each tuple, unpacked
    # directly into four named variables via "tuple unpacking".
    for i, (name, gender, printed_start, columns) in enumerate(SECTIONS):
        if columns is None:
            continue  # a deliberately-skipped section (relays, or the END sentinel)
        # The PDF's own page numbers (as printed on the page) are 1-indexed,
        # but pypdf's `reader.pages` list is 0-indexed like any Python list,
        # so subtract 1 to convert between the two.
        title_idx = printed_start - 1
        # SECTIONS[i + 1] is the *next* section in the list -- its start page
        # tells us where this section's data pages end (the page right
        # before the next section's own title page).
        next_printed_start = SECTIONS[i + 1][2]
        next_title_idx = next_printed_start - 1
        data_start = title_idx + 1
        data_end = next_title_idx - 1
        section_tables = _parse_section_pages(reader, gender, data_start, data_end, columns)
        # dict.update(other_dict) merges another dict's key/value pairs in,
        # overwriting any keys that already existed -- here, adding this
        # section's columns into the overall per-gender table.
        tables[gender].update(section_tables)
        print(f"{name} ({gender}): parsed {sum(len(v) for v in section_tables.values())} entries "
              f"across {len(columns)} columns [pages {data_start}-{data_end}]")
    return tables


def load_tables(rebuild=False):
    """Loads the mark->points tables, preferring the on-disk JSON cache over
    re-parsing the (slow to read) PDF, unless rebuild=True or no cache exists
    yet."""
    if CACHE_PATH.exists() and not rebuild:
        with open(CACHE_PATH, encoding='utf-8') as f:
            raw = json.load(f)
        # JSON object keys are always strings, even if they started out as
        # numbers -- so after loading, every mark value got turned into a
        # text key like "10.23" instead of the float 10.23. This nested dict
        # comprehension rebuilds the same {gender: {column: {mark: points}}}
        # shape but converts each key back to float so later float-based
        # lookups (mark_value in table) work correctly.
        return {
            gender: {col: {float(k): v for k, v in marks.items()} for col, marks in cols.items()}
            for gender, cols in raw.items()
        }
    tables = build_tables()
    with open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(tables, f)
    return tables


class Scorer:
    """Wraps the parsed mark->points tables and answers "how many points is
    this mark worth?" queries. `class` defines a blueprint for objects that
    bundle together data (self.tables, self._sorted_keys) and behavior
    (the methods below) -- creating `Scorer()` builds one such object."""

    def __init__(self, rebuild=False):
        # __init__ is the constructor: Python calls it automatically when you
        # write `Scorer()`, to set up a fresh instance. `self` refers to
        # "this particular Scorer object being created" -- every method on a
        # class takes `self` as its first parameter so it can read/store data
        # on that specific instance (as opposed to some other Scorer you
        # might also have around).
        self.tables = load_tables(rebuild=rebuild)
        # A cache of sorted mark lists, filled in lazily (only computed the
        # first time a given gender/column combination is actually needed --
        # see _sorted below) rather than up front for every column.
        self._sorted_keys = {}

    def _sorted(self, gender, column):
        """Returns (and caches) the sorted list of every mark on file for one
        gender/column, needed for the "nearest tabulated mark" fallback in
        score() below."""
        key = (gender, column)
        if key not in self._sorted_keys:
            self._sorted_keys[key] = sorted(self.tables[gender][column].keys())
        return self._sorted_keys[key]

    def score(self, gender, column, mark_value):
        """Returns points for mark_value in the given gender/column, or None if unscoreable."""
        column = norm_col(column)
        # Chained .get() calls: the first .get(gender, {}) returns an empty
        # dict if that gender isn't in self.tables (instead of raising an
        # error), so the second .get(column) is always safe to call even if
        # the gender itself was unrecognized.
        table = self.tables.get(gender, {}).get(column)
        if not table or mark_value is None:
            return None
        if mark_value in table:
            # The exact mark is tabulated -- easy case, just look it up.
            return table[mark_value]
        keys = self._sorted(gender, column)
        if not keys:
            return None
        # exact mark not tabulated (happens between adjacent whole-point rows) -- per the book's
        # "should a performance fall between two results, the lower score shall be considered"
        # rule: for a time (lower-is-better) take the next SLOWER tabulated mark; for a
        # distance/height/points event (higher-is-better) take the next SHORTER tabulated mark.
        # Empirically verified against a club's published 2025 rankings (126 rows): this matches
        # 93/100 non-exact marks vs 66/100 for nearest-mark rounding.
        higher_better = column in HIGHER_IS_BETTER_COLUMNS
        # bisect_left(keys, mark_value) does a binary search over the sorted
        # `keys` list and returns the index where mark_value would need to be
        # inserted to keep the list sorted -- i.e. the position of the first
        # tabulated mark that is >= mark_value. Binary search finds this in
        # O(log n) steps instead of scanning every mark one by one.
        pos = bisect_left(keys, mark_value)
        if higher_better:
            # For distance/height events, "the next result down" (a smaller,
            # i.e. worse, tabulated mark) is one position back from `pos`.
            if pos == 0:
                # mark_value is smaller than every tabulated mark -- there is
                # no lower one to fall back to, so it's unscoreable.
                return None
            return table[keys[pos - 1]]
        # For times, "the next result down" (a slower, i.e. worse, tabulated
        # mark) is the one bisect_left already pointed at.
        if pos >= len(keys):
            # mark_value is slower than every tabulated mark -- nothing worse
            # to fall back to.
            return None
        return table[keys[pos]]
