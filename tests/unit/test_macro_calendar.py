"""Macro calendar parsers: official schedules and ForexFactory, pinned to trimmed live captures."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from polymarket_exec.connectors import macro_calendar as mc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "macro"


def _ms(y: int, mo: int, d: int, h: int, mi: int) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()) * 1000


def _read(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("utf-8")


def _by_time_title(events):
    return {(e.scheduled_at_ms, e.title): e for e in events}


@pytest.mark.parametrize(
    ("title", "category"),
    [
        # Official titles
        ("Consumer Price Index", "cpi"),
        ("Producer Price Index", "ppi"),
        ("Employment Situation", "nfp"),
        ("Employment Situation of Veterans", "other"),
        ("Job Openings and Labor Turnover Survey", "jolts"),
        ("State Job Openings and Labor Turnover", "other"),
        ("Advance Monthly Sales for Retail and Food Services", "retail_sales"),
        ("GDP (Advance Estimate)", "gdp"),
        ("Gross Domestic Product by State and Personal Income by State", "other"),
        ("Personal Income and Outlays", "pce"),
        ("Real Personal Consumption Expenditures by State and Real Personal Income by State",
         "other"),
        ("FOMC Meeting", "fomc_decision"),
        ("FOMC Press Conference", "fomc_press_conference"),
        ("FOMC Minutes", "fomc_minutes"),
        ("Beige Book", "beige_book"),
        ("Speech - Chair Jerome H. Powell", "fed_chair_speech"),
        ("Discussion -- Chairman Jerome H. Powell", "fed_chair_speech"),
        ("Speech - Vice Chair for Supervision Michelle W. Bowman", "fed_speech"),
        ("Speech - Vice Chair Philip N. Jefferson", "fed_speech"),
        ("Speech - Governor Christopher J. Waller", "fed_speech"),
        ("Testimony - Chair Jerome H. Powell", "fed_testimony"),
        ("Real Earnings", "other"),
        # ForexFactory titles
        ("CPI m/m", "cpi"),
        ("Core CPI m/m", "cpi"),
        ("PPI m/m", "ppi"),
        ("Non-Farm Employment Change", "nfp"),
        ("ADP Non-Farm Employment Change", "other"),
        ("Unemployment Claims", "jobless_claims"),
        ("Retail Sales m/m", "retail_sales"),
        ("Core Retail Sales m/m", "retail_sales"),
        ("Advance GDP q/q", "gdp"),
        ("Core PCE Price Index m/m", "pce"),
        ("JOLTS Job Openings", "jolts"),
        ("FOMC Statement", "fomc_decision"),
        ("Federal Funds Rate", "fomc_decision"),
        ("FOMC Economic Projections", "fomc_decision"),
        ("FOMC Meeting Minutes", "fomc_minutes"),
        ("Fed Chair Powell Speaks", "fed_chair_speech"),
        ("Fed Chair Powell Testifies", "fed_testimony"),
        ("FOMC Member Bowman Speaks", "fed_speech"),
        ("Philly Fed Manufacturing Index", "other"),
        ("Treasury Sec Bessent Speaks", "other"),
        ("Empire State Manufacturing Index", "other"),
    ],
)
def test_categorize(title: str, category: str) -> None:
    assert mc.categorize(title) == category


def test_bls_ics_eastern_local_times_with_dst() -> None:
    events = mc.parse_bls_ics(_read("bls_schedule.ics"))
    by = _by_time_title(events)
    # Calendar-level SUMMARY and VTIMEZONE DTSTARTs are not events.
    assert all(e.title != "BLS.gov Economic News Release Schedule" for e in events)
    assert len(events) == 7
    winter = by[(_ms(2026, 1, 13, 13, 30), "Consumer Price Index")]  # 8:30 EST
    assert (winter.source, winter.category, winter.reference_period) == ("bls", "cpi", None)
    assert (_ms(2026, 10, 14, 12, 30), "Consumer Price Index") in by  # 8:30 EDT
    assert by[(_ms(2026, 10, 2, 12, 30), "Employment Situation")].category == "nfp"
    assert by[(_ms(2026, 9, 29, 14, 0), "Job Openings and Labor Turnover Survey")].category == "jolts"
    assert by[(_ms(2026, 10, 15, 12, 30), "Producer Price Index")].category == "ppi"
    commas = [e for e in events if e.title.startswith("Labor Market Experience")]
    assert commas[0].title.endswith("Partner Status, and Health for those Born 1980-1984")


def test_bea_ics_unfolds_unescapes_and_splits_obvious_periods() -> None:
    events = mc.parse_bea_ics(_read("bea_schedule.ics"))
    by = _by_time_title(events)
    assert len(events) == 8
    # Folded SUMMARY, VALUE=DATE-TIME UTC form; the trailing "(Advance Estimate)" makes the
    # period not obvious, so the whole summary is the title.
    old = by[(_ms(2025, 1, 30, 13, 30),
              "Gross Domestic Product, 4th Quarter and Year 2024 (Advance Estimate)")]
    assert (old.category, old.reference_period) == ("gdp", None)
    gdp = by[(_ms(2026, 10, 29, 12, 30), "GDP (Advance Estimate)")]
    assert (gdp.source, gdp.category, gdp.reference_period) == ("bea", "gdp", "3rd Quarter 2026")
    pce = by[(_ms(2026, 8, 26, 12, 30), "Personal Income and Outlays")]  # trailing space
    assert (pce.category, pce.reference_period) == ("pce", "July 2026")
    second = by[(_ms(2026, 8, 26, 12, 30), "GDP (Second Estimate) and Corporate Profits")]
    assert second.reference_period == "2nd Quarter 2026"
    # Fold inside an escape ("\" CRLF SP ",") and an escaped semicolon.
    third = [e for e in events if e.title.startswith("GDP (Third Estimate)")][0]
    assert third.title == ("GDP (Third Estimate), Industries, Corporate Profits, State GDP, "
                           "and State Personal Income, 2nd Quarter 2026; State PCE, 2025")
    assert (third.category, third.reference_period) == ("gdp", None)
    state = [e for e in events if e.title.startswith("Real Personal Consumption")][0]
    assert (state.category, state.reference_period) == ("other", "2025")


def test_ics_reader_handles_utc_forms_escapes_and_bad_lines() -> None:
    text = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Line one\\nline two\\\\ \\; done\r\n"
        "DTSTART:20260115T133000Z\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nSUMMARY:No start\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nSUMMARY:All day\r\nDTSTART;VALUE=DATE:20260116\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nSUMMARY:Garbage\r\nDTSTART:not-a-date\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nSUMMARY:Consumer\r\n\tPrice Index\r\n"
        "DTSTART;TZID=America/New_York:20260714T083000\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    events = mc.parse_bls_ics(text)
    assert [(e.title, e.scheduled_at_ms) for e in events] == [
        ("Line one line two\\ ; done", _ms(2026, 1, 15, 13, 30)),
        ("ConsumerPrice Index", _ms(2026, 7, 14, 12, 30)),
    ]
    assert mc.parse_bls_ics("<html>Access Denied</html>") == []


def test_fed_calendar_keeps_fomc_speeches_testimony_beige() -> None:
    obj = json.loads(_read("fed_calendar.json"))
    events = mc.parse_fed_calendar(obj)
    by = _by_time_title(events)
    decision = by[(_ms(2026, 9, 16, 18, 0), "FOMC Meeting")]  # 2:00 p.m. EDT
    assert (decision.source, decision.category) == ("fed", "fomc_decision")
    assert by[(_ms(2026, 12, 9, 19, 0), "FOMC Meeting")].category == "fomc_decision"  # EST
    assert by[(_ms(2026, 9, 16, 18, 30), "FOMC Press Conference")].category == (
        "fomc_press_conference")
    assert by[(_ms(2026, 12, 30, 19, 0), "FOMC Minutes")].category == "fomc_minutes"
    assert by[(_ms(2026, 10, 14, 18, 0), "Beige Book")].category == "beige_book"
    bowman = by[(_ms(2026, 9, 18, 13, 30), "Speech - Vice Chair for Supervision Michelle W. Bowman")]
    assert bowman.category == "fed_speech"
    assert by[(_ms(2026, 9, 3, 12, 30), "Speech - Governor Christopher J. Waller")]
    # Legacy "events" type with empty month, the empty object and the "Other" holiday are skipped.
    assert len(events) == 7
    assert not any("Powell" in e.title or "Holiday" in e.title for e in events)


def test_fed_calendar_expands_days_and_skips_bad_rows() -> None:
    obj = {"events": [
        {"title": "  Speech - Chair Jerome H. Powell ", "time": "10:00 a.m.", "month": "2026-11",
         "days": "3, 10, 17, 24", "type": "Speeches"},
        {"title": "Semiannual Monetary Policy Report", "time": "12:15 p.m.", "month": "2026-07",
         "days": "8", "type": "Testimony"},
        {"title": "Speech - Governor Lisa D. Cook", "time": "TBD", "month": "2026-11",
         "days": "5", "type": "Speeches"},
        {"title": "Speech - Governor Lisa D. Cook", "time": "9:00 a.m.", "month": "2026-11",
         "days": "31", "type": "Speeches"},
        {"title": "FOMC Meeting", "time": "2:00 p.m.", "month": "", "days": "4", "type": "FOMC"},
        "not an object",
    ]}
    events = mc.parse_fed_calendar(obj)
    assert [(e.title, e.scheduled_at_ms, e.category) for e in events] == [
        ("Speech - Chair Jerome H. Powell", _ms(2026, 11, 3, 15, 0), "fed_chair_speech"),
        ("Speech - Chair Jerome H. Powell", _ms(2026, 11, 10, 15, 0), "fed_chair_speech"),
        ("Speech - Chair Jerome H. Powell", _ms(2026, 11, 17, 15, 0), "fed_chair_speech"),
        ("Speech - Chair Jerome H. Powell", _ms(2026, 11, 24, 15, 0), "fed_chair_speech"),
        ("Semiannual Monetary Policy Report", _ms(2026, 7, 8, 16, 15), "fed_testimony"),
    ]
    assert mc.parse_fed_calendar([]) == [] and mc.parse_fed_calendar({"events": None}) == []


def test_census_calendar_rows_skip_suspended_and_merge_same_slot() -> None:
    events = mc.parse_census_calendar(_read("census_calendar.html"))
    by = _by_time_title(events)
    assert len(events) == 3
    retail = by[(_ms(2026, 9, 16, 12, 30), "Advance Monthly Sales for Retail and Food Services")]
    assert (retail.source, retail.category, retail.reference_period) == (
        "census", "retail_sales", "August 2026")
    winter = by[(_ms(2026, 1, 14, 13, 30), "Advance Monthly Sales for Retail and Food Services")]
    assert winter.reference_period == "November 2025"
    nrc = by[(_ms(2026, 1, 9, 13, 30), "New Residential Construction (Building Permits, "
                                       "Housing Starts, and Housing Completions)")]
    assert (nrc.category, nrc.reference_period) == ("other", "September 2025; October 2025")
    assert not any("Steel" in e.title for e in events)  # "Suspended" despite a sort key
    assert mc.parse_census_calendar("<html><body>no table</body></html>") == []


def test_ff_week_keeps_usd_rows_and_consensus() -> None:
    events, consensus = mc.parse_ff_week(json.loads(_read("ff_thisweek.json")))
    assert {e.source for e in events} == {"forexfactory"}
    assert not any(e.title in ("CPI y/y", "BRICS Summit") for e in events)  # GBP, All
    by = _by_time_title(events)
    assert by[(_ms(2026, 9, 16, 12, 30), "Retail Sales m/m")].category == "retail_sales"
    assert by[(_ms(2026, 9, 16, 18, 0), "Federal Funds Rate")].category == "fomc_decision"
    assert by[(_ms(2026, 9, 17, 12, 30), "Unemployment Claims")].category == "jobless_claims"
    assert len(events) == len(consensus) == 8
    claims = [c for c in consensus if c.title == "Unemployment Claims"][0]
    assert claims == mc.ConsensusRow("forexfactory", "USD", "Unemployment Claims",
                                     "jobless_claims", _ms(2026, 9, 17, 12, 30),
                                     "Medium", "209K", "206K")
    statement = [c for c in consensus if c.title == "FOMC Statement"][0]
    assert (statement.impact, statement.forecast, statement.previous) == ("High", None, None)


def _ff_row(iso: str, title: str = "CPI m/m") -> mc.MacroEvent:
    ms = int(datetime.fromisoformat(iso).timestamp()) * 1000
    return mc.MacroEvent("forexfactory", mc.categorize(title), title, ms)


def test_ff_week_window_is_the_eastern_sunday_week_of_the_earliest_row() -> None:
    events, _ = mc.parse_ff_week(json.loads(_read("ff_thisweek.json")))
    # Earliest USD row is Wed 2026-09-16: the week runs Sun 09-13 00:00 EDT to Sun 09-20.
    assert mc.ff_week_window(events) == (_ms(2026, 9, 13, 4, 0), _ms(2026, 9, 20, 4, 0) - 1)
    # A row on the Sunday itself, and one late on Saturday, belong to the same week.
    assert mc.ff_week_window([_ff_row("2026-09-19T23:30:00-04:00"),
                              _ff_row("2026-09-13T17:00:00-04:00")]) == (
        _ms(2026, 9, 13, 4, 0), _ms(2026, 9, 20, 4, 0) - 1)
    # DST ends Sun 2026-11-01: that week starts on EDT midnight and ends on EST midnight.
    assert mc.ff_week_window([_ff_row("2026-11-02T08:30:00-05:00")]) == (
        _ms(2026, 11, 1, 4, 0), _ms(2026, 11, 8, 5, 0) - 1)
    # DST starts Sun 2026-03-08: EST midnight to EDT midnight.
    assert mc.ff_week_window([_ff_row("2026-03-10T08:30:00-04:00")]) == (
        _ms(2026, 3, 8, 5, 0), _ms(2026, 3, 15, 4, 0) - 1)
    # A row past the Saturday widens the window to cover it.
    assert mc.ff_week_window([_ff_row("2026-09-16T08:30:00-04:00"),
                              _ff_row("2026-09-21T10:00:00-04:00")]) == (
        _ms(2026, 9, 13, 4, 0), _ms(2026, 9, 21, 14, 0))
    with pytest.raises(ValueError):
        mc.ff_week_window([])


def test_ff_week_tolerates_junk() -> None:
    assert mc.parse_ff_week({"error": "rate limited"}) == ([], [])
    events, _ = mc.parse_ff_week([
        {"title": "CPI m/m", "country": "USD", "date": "not a date"},
        {"title": "CPI m/m", "country": "USD", "date": "2026-09-16T08:30:00"},  # no offset
        {"title": "", "country": "USD", "date": "2026-09-16T08:30:00-04:00"},
        None,
        {"title": "CPI m/m", "country": "USD", "date": "2026-01-13T08:30:00-05:00"},
    ])
    assert [(e.title, e.scheduled_at_ms) for e in events] == [("CPI m/m", _ms(2026, 1, 13, 13, 30))]
