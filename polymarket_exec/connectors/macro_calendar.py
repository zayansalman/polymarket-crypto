"""Pure parsers for US macro release calendars: BLS/BEA ICS, Census and Fed calendars, ForexFactory.

No I/O. Each parser turns one source's raw payload into ``MacroEvent`` rows with a UTC
epoch-ms time; ForexFactory also yields ``ConsensusRow`` (impact, forecast, previous).
Naive Eastern times are converted with ``zoneinfo`` so DST is handled per date.

Categories are plain labels for the release an event belongs to (``cpi``, ``fomc_decision``,
...), not importance scores. Observation data only; nothing here decides or gates.
"""
from __future__ import annotations

import html as _html
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
# TZIDs seen in agency calendars that are not IANA names.
_TZID_ALIASES = {"US-Eastern": "America/New_York"}

CATEGORIES = (
    "cpi", "ppi", "nfp", "jolts", "jobless_claims", "retail_sales", "gdp", "pce",
    "fomc_decision", "fomc_press_conference", "fomc_minutes", "fed_chair_speech", "fed_speech",
    "fed_testimony", "beige_book", "other",
)


@dataclass(frozen=True)
class MacroEvent:
    source: str  # bls | bea | census | fed | forexfactory
    category: str
    title: str
    scheduled_at_ms: int
    reference_period: str | None = None


@dataclass(frozen=True)
class ConsensusRow:
    source: str
    country: str
    title: str
    category: str
    scheduled_at_ms: int
    impact: str | None
    forecast: str | None
    previous: str | None


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

_FED_TALK = re.compile(
    r"\b(speech|speaks|remarks|discussion|conversation|panel|testimony|testifies)\b"
)
_FED_CONTEXT = re.compile(r"\b(fed|fomc|federal reserve|governor|chair|chairman)\b")
_CHAIR = re.compile(r"\bchair(man)?\b")
_VICE_CHAIR = re.compile(r"\bvice[\s-]+chair(man)?\b")
_REGIONAL = re.compile(r"\b(by state|by county|puerto rico|distribution of)\b|^state ")


def _is_chair(low: str) -> bool:
    """'Chair'/'Chairman' in the title, not counting any 'Vice Chair'."""
    return bool(_CHAIR.search(_VICE_CHAIR.sub(" ", low)))


def _fed_talk_category(low: str) -> str:
    if re.search(r"\b(testimony|testifies)\b", low):
        return "fed_testimony"
    return "fed_chair_speech" if _is_chair(low) else "fed_speech"


def categorize(title: str) -> str:
    """Label the release an event belongs to, for official and ForexFactory titles alike."""
    low = " ".join(title.split()).lower()
    if "beige book" in low:
        return "beige_book"
    if "fomc" in low or "federal funds rate" in low:
        if "minutes" in low:
            return "fomc_minutes"
        if "press conference" in low:
            return "fomc_press_conference"
        if re.search(r"\b(meeting|statement|federal funds rate|economic projections)\b", low):
            return "fomc_decision"
    if _FED_TALK.search(low) and _FED_CONTEXT.search(low):
        return _fed_talk_category(low)
    if re.search(r"\bcpi\b", low) or "consumer price index" in low:
        return "cpi"
    if re.search(r"\bppi\b", low) or "producer price index" in low:
        return "ppi"
    if low == "employment situation" or (
        "adp" not in low
        and re.search(r"non-?farm (employment change|payrolls)|^unemployment rate$|"
                      r"^average hourly earnings", low)
    ):
        return "nfp"
    if ("jolts" in low or "job openings and labor turnover" in low) and not _REGIONAL.search(low):
        return "jolts"
    if re.search(r"\b(unemployment|jobless|initial) claims\b", low):
        return "jobless_claims"
    if "retail sales" in low or "sales for retail and food services" in low:
        return "retail_sales"
    if (re.search(r"\bgdp\b", low) or "gross domestic product" in low) and not _REGIONAL.search(
        low
    ):
        return "gdp"
    if (
        re.search(r"\bpce\b", low)
        or "personal consumption expenditures" in low
        or "personal income" in low
        or "personal spending" in low
    ) and not _REGIONAL.search(low):
        return "pce"
    return "other"


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _epoch_ms(dt: datetime) -> int:
    return int(dt.timestamp()) * 1000


def _eastern_ms(year: int, month: int, day: int, hour: int, minute: int, second: int = 0) -> int:
    return _epoch_ms(datetime(year, month, day, hour, minute, second, tzinfo=EASTERN))


def _clean(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Minimal RFC 5545 reader
# ---------------------------------------------------------------------------

_ICS_UNESCAPE = re.compile(r"\\([\\;,nN])")
_ICS_LOCAL = re.compile(r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)$")


def _ics_unescape(value: str) -> str:
    return _ICS_UNESCAPE.sub(lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _ics_vevents(text: str) -> list[dict[str, tuple[dict[str, str], str]]]:
    """VEVENT property maps: name -> (params, raw value). First occurrence of a name wins."""
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    events: list[dict[str, tuple[dict[str, str], str]]] = []
    current: dict[str, tuple[dict[str, str], str]] | None = None
    for line in unfolded.splitlines():
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *raw_params = head.split(";")
            params = {}
            for p in raw_params:
                key, _, val = p.partition("=")
                params[key.upper()] = val.strip('"')
            current.setdefault(name.upper(), (params, value))
    return events


def _ics_start_ms(params: dict[str, str], value: str) -> int | None:
    """DTSTART as epoch ms; UTC ('Z'), TZID-local, or floating (taken as Eastern)."""
    if params.get("VALUE", "DATE-TIME").upper() != "DATE-TIME":
        return None  # all-day date: no release time
    m = _ICS_LOCAL.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups()[:6])
    try:
        if m.group(7):
            return _epoch_ms(datetime(y, mo, d, h, mi, s, tzinfo=UTC))
        tzid = params.get("TZID")
        tz = ZoneInfo(_TZID_ALIASES.get(tzid, tzid)) if tzid else EASTERN
        return _epoch_ms(datetime(y, mo, d, h, mi, s, tzinfo=tz))
    except (ValueError, KeyError, OSError):  # bad date, unknown zone
        return None


def _ics_items(text: str) -> list[tuple[str, int]]:
    items = []
    for ev in _ics_vevents(text):
        if "SUMMARY" not in ev or "DTSTART" not in ev:
            continue
        title = _clean(_ics_unescape(ev["SUMMARY"][1]))
        start = _ics_start_ms(*ev["DTSTART"])
        if title and start is not None:
            items.append((title, start))
    return items


def parse_bls_ics(text: str) -> list[MacroEvent]:
    """BLS news release schedule (``bls.ics``)."""
    return [MacroEvent("bls", categorize(t), t, ms) for t, ms in _ics_items(text)]


_MONTH = (r"(?:January|February|March|April|May|June|July|August|September|October|"
          r"November|December)")
_PERIOD = re.compile(
    rf"^(?:{_MONTH}(?: and {_MONTH})? \d{{4}}|{_MONTH} and Annual \d{{4}}|"
    rf"(?:1st|2nd|3rd|4th) [Qq]uarter(?: and [Yy]ear)? \d{{4}}|\d{{4}})$"
)


def _split_period(summary: str) -> tuple[str, str | None]:
    """'GDP (Advance Estimate), 3rd Quarter 2026' -> title + period, only when unambiguous."""
    if ";" in summary:
        return summary, None
    head, sep, tail = summary.rpartition(", ")
    if sep and head and _PERIOD.match(tail):
        return head, tail
    return summary, None


def parse_bea_ics(text: str) -> list[MacroEvent]:
    """BEA release calendar subscription; the reference period is split out when obvious."""
    events = []
    for summary, ms in _ics_items(text):
        title, period = _split_period(summary)
        events.append(MacroEvent("bea", categorize(title), title, ms, period))
    return events


# ---------------------------------------------------------------------------
# Federal Reserve calendar JSON
# ---------------------------------------------------------------------------

_FED_TYPES = {"FOMC", "Speeches", "Testimony", "Beige"}
_FED_TIME = re.compile(r"^(\d{1,2}):(\d{2})\s*([ap])\.?\s*m\.?$", re.IGNORECASE)


def _fed_time(value: Any) -> tuple[int, int] | None:
    m = _FED_TIME.match(str(value or "").strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (1 <= hour <= 12 and minute < 60):
        return None
    hour %= 12
    if m.group(3).lower() == "p":
        hour += 12
    return hour, minute


def _fed_category(title: str, typ: str) -> str:
    category = categorize(title)
    if category != "other":
        return category
    if typ == "Testimony":
        return "fed_testimony"
    if typ == "Speeches":
        return _fed_talk_category(title.lower())
    if typ == "Beige":
        return "beige_book"
    return category


def parse_fed_calendar(obj: Any) -> list[MacroEvent]:
    """federalreserve.gov ``calendar.json`` (already decoded): FOMC, speeches, testimony, Beige."""
    rows = obj.get("events") if isinstance(obj, dict) else None
    if not isinstance(rows, list):
        return []
    events = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in _FED_TYPES:
            continue
        month = str(row.get("month") or "").strip()
        title = _clean(_html.unescape(str(row.get("title") or "")))
        hm = _fed_time(row.get("time"))
        if not re.fullmatch(r"\d{4}-\d{2}", month) or not title or hm is None:
            continue
        year, mon = int(month[:4]), int(month[5:])
        category = _fed_category(title, row["type"])
        for day in str(row.get("days") or "").split(","):
            day = day.strip()
            if not day.isdigit():
                continue
            try:
                ms = _eastern_ms(year, mon, int(day), *hm)
            except ValueError:
                continue
            events.append(MacroEvent("fed", category, title, ms))
    return events


# ---------------------------------------------------------------------------
# Census economic indicator calendar (HTML list view)
# ---------------------------------------------------------------------------

_REAL_DATE = re.compile(rf"\b{_MONTH} \d{{1,2}}, \d{{4}}\b")


@dataclass
class _Cell:
    tag: str
    attrs: dict[str, str | None]
    text: list[str]
    link: list[str]


class _CalendarTable(HTMLParser):
    """Collects the cells of every row of ``<table id="calendar">``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[_Cell]] = []
        self._in_table = False
        self._nested = 0
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._in_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self._in_table:
                self._nested += 1
            elif dict(attrs).get("id") == "calendar":
                self._in_table = True
            return
        if not self._in_table or self._nested:
            return
        if tag == "tr":
            self._close_row()
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._close_cell()
            self._cell = _Cell(tag, dict(attrs), [], [])
        elif tag == "a":
            self._in_link = True

    def handle_endtag(self, tag: str) -> None:
        if not self._in_table:
            return
        if tag == "table":
            if self._nested:
                self._nested -= 1
            else:
                self._close_row()
                self._in_table = False
        elif self._nested:
            return
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_row()
        elif tag == "a":
            self._in_link = False

    def handle_data(self, data: str) -> None:
        if self._cell is not None and not self._nested:
            self._cell.text.append(data)
            if self._in_link:
                self._cell.link.append(data)

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(self._cell)
        self._cell = None
        self._in_link = False

    def _close_row(self) -> None:
        self._close_cell()
        if self._row is not None:
            self.rows.append(self._row)
        self._row = None


def parse_census_calendar(page: str) -> list[MacroEvent]:
    """Census economic indicator calendar list view; rows without a real date are skipped."""
    table = _CalendarTable()
    table.feed(page)
    table.close()
    merged: dict[tuple[str, int], MacroEvent] = {}
    for row in table.rows:
        cells = [c for c in row if c.tag == "td"]
        if len(cells) < 4:
            continue
        key = str(cells[1].attrs.get("sorttable_customkey") or "")
        visible_date = _clean("".join(cells[1].text))
        title = _clean("".join(cells[0].link or cells[0].text))
        period = _clean("".join(cells[3].text)) or None
        if not re.fullmatch(r"\d{12}", key) or not _REAL_DATE.search(visible_date) or not title:
            continue  # e.g. "Suspended" keeps its old sort key
        try:
            ms = _eastern_ms(int(key[:4]), int(key[4:6]), int(key[6:8]), int(key[8:10]),
                             int(key[10:12]))
        except ValueError:
            continue
        prior = merged.get((title, ms))
        if prior is not None and period and prior.reference_period != period:
            # One slot releasing several periods (catch-up after a delay).
            period = f"{prior.reference_period}; {period}" if prior.reference_period else period
        merged[(title, ms)] = MacroEvent("census", categorize(title), title, ms, period)
    return list(merged.values())


# ---------------------------------------------------------------------------
# ForexFactory (faireconomy.media weekly JSON)
# ---------------------------------------------------------------------------


def _blank_to_none(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def parse_ff_week(obj: Any) -> tuple[list[MacroEvent], list[ConsensusRow]]:
    """This week's calendar: USD rows only, as events plus their consensus snapshot."""
    if not isinstance(obj, list):
        return [], []
    events: list[MacroEvent] = []
    consensus: list[ConsensusRow] = []
    for row in obj:
        if not isinstance(row, dict) or row.get("country") != "USD":
            continue
        title = _clean(str(row.get("title") or ""))
        try:
            when = datetime.fromisoformat(str(row.get("date") or ""))
        except ValueError:
            continue
        if not title or when.tzinfo is None:
            continue
        ms = _epoch_ms(when)
        category = categorize(title)
        events.append(MacroEvent("forexfactory", category, title, ms))
        consensus.append(ConsensusRow(
            "forexfactory", "USD", title, category, ms,
            _blank_to_none(row.get("impact")), _blank_to_none(row.get("forecast")),
            _blank_to_none(row.get("previous")),
        ))
    return events, consensus


def ff_week_window(events: Sequence[MacroEvent]) -> tuple[int, int]:
    """Inclusive [start, end] ms that one ForexFactory week pull is complete for.

    The feed lists a Sunday-to-Saturday week in Eastern time. The week is read from the
    earliest row rather than the wall clock, so a pull made around the weekly rollover
    cannot pick the wrong week; a row past that Saturday widens the end to cover it.
    """
    if not events:
        raise ValueError("ff_week_window needs at least one event")
    times = [e.scheduled_at_ms for e in events]
    first = datetime.fromtimestamp(min(times) / 1000, tz=EASTERN).date()
    sunday = first - timedelta(days=(first.weekday() + 1) % 7)
    after = sunday + timedelta(days=7)  # calendar days: a DST week is 167 or 169 hours
    start = _eastern_ms(sunday.year, sunday.month, sunday.day, 0, 0)
    end = _eastern_ms(after.year, after.month, after.day, 0, 0) - 1
    return start, max(end, max(times))
