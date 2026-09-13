"""Unit tests for the venue recorder (tools/venue_recorder.py).

The recorder's job is to capture things that cannot be recovered later: full
book depth, the trade tape, and the reference price paired with the book at
receipt. Every test here guards a specific way this project has already been
burned.

  - ``classify`` must never silently assert a rung. C7 killed an entire thesis
    because market structure was asserted from memory instead of queried, so the
    classifier is required to carry the evidence that produced its guess.
  - ``_levels`` must keep every level and read best-price from the LAST element.
    CLOB arrays run worst-to-best; the 5m recorder kept only that last element,
    which is why no true L2 depth exists in this project's history.
  - the schema must round-trip a book whose paired reference price is missing,
    because a recorder that crashes on a gap turns a gap into an outage.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite
import pytest

from tools.venue_recorder import (
    SCHEMA,
    _epoch,
    _levels,
    _tokens,
    classify,
    persist_markets,
)


# --------------------------------------------------------------------------- #
# classify — the guard against another C7
# --------------------------------------------------------------------------- #


def test_classify_reads_the_rung_off_a_structured_slug():
    asset, family, rung, evidence = classify({"slug": "btc-updown-5m-1766162100"})
    assert asset == "btc"
    assert family == "btc-updown-5m"
    assert rung == "5m"
    assert "btc-updown-5m-1766162100" in evidence


def test_classify_derives_named_date_window_length_from_venue_dates():
    """Named-date markets do not state their window length; it is derived.

    The live venue's "Up or Down" family runs ~49h windows, not the 24h the
    design's "daily rung" assumes — so the derived number must come from the
    venue's own dates, never from the word "daily".
    """
    _, _, rung, evidence = classify(
        {
            "slug": "bitcoin-up-or-down-may-20-2026-6am-et",
            "question": "Bitcoin Up or Down - May 20, 6AM ET",
            "startDate": "2026-05-18T10:02:08",
            "endDate": "2026-05-20T11:00:00",
        }
    )
    assert rung == "~49h"
    assert "endDate-startDate" in evidence


def test_classify_never_guesses_without_recording_evidence():
    """An unrecognised slug is 'unknown' plus its evidence — never a rung."""
    _, _, rung, evidence = classify({"slug": "some-unrelated-market"})
    assert rung == "unknown"
    assert "some-unrelated-market" in evidence


def test_classify_marks_a_1h_family_if_one_ever_appears():
    """No 1h family existed at the 2026-08-13 probe. If one is listed, catch it."""
    _, family, rung, _ = classify({"slug": "btc-updown-1h-1766163600"})
    assert rung == "1h"
    assert family == "btc-updown-1h"


# --------------------------------------------------------------------------- #
# _levels — full depth, and the worst-to-best convention
# --------------------------------------------------------------------------- #


def test_levels_keeps_every_level_and_totals_depth():
    raw = [
        {"price": "0.90", "size": "100"},
        {"price": "0.94", "size": "50"},
        {"price": "0.95", "size": "10"},
    ]
    levels, best, total, n = _levels(raw)
    assert n == 3, "every level must survive — the touch alone is the old bug"
    assert levels == [[0.90, 100.0], [0.94, 50.0], [0.95, 10.0]]
    assert best == 0.95, "CLOB arrays are worst-to-best; best is the LAST element"
    assert total == 160.0


def test_levels_survives_a_malformed_level_without_losing_the_rest():
    levels, best, total, n = _levels(
        [{"price": "0.90", "size": "100"}, {"nope": 1}, {"price": "0.95", "size": "5"}]
    )
    assert n == 2 and best == 0.95 and total == 105.0


def test_levels_on_an_empty_book_is_empty_not_an_error():
    assert _levels([]) == ([], None, 0.0, 0)
    assert _levels(None) == ([], None, 0.0, 0)


def test_recorded_levels_reconstruct_a_size_dependent_cost_curve():
    """The C6 deliverable: average fill must worsen with size, from stored data."""
    levels, _, _, _ = _levels(
        [
            {"price": "0.999", "size": "1484"},
            {"price": "0.998", "size": "252"},
            {"price": "0.955", "size": "199"},
            {"price": "0.95", "size": "10"},
        ]
    )
    best_first = list(reversed(levels))
    cum = spent = 0.0
    avg_fills = []
    for price, size in best_first:
        cum += size
        spent += price * size
        avg_fills.append(spent / cum)
    assert avg_fills == sorted(avg_fills), "taking more size must cost more"
    assert avg_fills[0] == pytest.approx(0.95)
    assert avg_fills[-1] > avg_fills[0] + 0.02, "slippage must exceed the cost stack here"


# --------------------------------------------------------------------------- #
# small parsers
# --------------------------------------------------------------------------- #


def test_tokens_parses_the_json_encoded_pair():
    assert _tokens({"clobTokenIds": '["111", "222"]'}) == ("111", "222")
    assert _tokens({"clobTokenIds": ["111", "222"]}) == ("111", "222")
    assert _tokens({"clobTokenIds": "not json"}) == (None, None)
    assert _tokens({}) == (None, None)


def test_epoch_handles_both_iso_forms_and_junk():
    assert _epoch("2026-05-20T11:00:00Z") == _epoch("2026-05-20T11:00:00+00:00")
    assert _epoch("nonsense") is None
    assert _epoch(None) is None


# --------------------------------------------------------------------------- #
# persistence
# --------------------------------------------------------------------------- #


def _market(slug: str, cond: str) -> dict:
    return {
        "slug": slug,
        "conditionId": cond,
        "question": "Bitcoin Up or Down - May 20, 6AM ET",
        "clobTokenIds": '["tok_up", "tok_down"]',
        "startDate": "2026-05-18T10:02:08",
        "endDate": "2026-05-20T11:00:00",
        "liquidityNum": 13666.6,
        "volumeNum": 26933.7,
        "acceptingOrders": True,
    }


def test_persist_writes_targets_tape_and_a_census_row(tmp_path: Path):
    async def run() -> None:
        db_path = tmp_path / "t.db"
        async with aiosqlite.connect(db_path) as db:
            await db.executescript(SCHEMA)
            targets, tape = await persist_markets(
                db, [_market("bitcoin-up-or-down-may-20-2026-6am-et", "0xabc")]
            )

            assert len(targets) == 2, "one target per outcome token"
            assert {t.role for t in targets} == {"up", "down"}
            assert all(t.symbol == "BTCUSDT" for t in targets), "S must be settlement-aligned"

            assert len(tape) == 1, "the tape is per market, keyed on conditionId"
            assert tape[0].condition_id == "0xabc"
            assert tape[0].roles == {"tok_up": "up", "tok_down": "down"}

            # The census is a measurement in its own right.
            rows = await (await db.execute(
                "SELECT family, rung_guess, n_markets, med_liquidity FROM rec_discovery"
            )).fetchall()
            assert len(rows) == 1
            assert rows[0][1] == "~49h", "the census records the derived window, not 'daily'"
            assert rows[0][3] == pytest.approx(13666.6)

            # Evidence is stored next to the guess so it can be re-derived offline.
            ev = await (await db.execute(
                "SELECT rung_evidence, condition_id FROM rec_markets"
            )).fetchall()
            assert "endDate-startDate" in ev[0][0]
            assert ev[0][1] == "0xabc"

    asyncio.run(run())


def test_persist_is_idempotent_and_refreshes_liquidity(tmp_path: Path):
    async def run() -> None:
        async with aiosqlite.connect(tmp_path / "t.db") as db:
            await db.executescript(SCHEMA)
            m = _market("btc-updown-5m-1766162100", "0xdef")
            await persist_markets(db, [m])
            m2 = dict(m, liquidityNum=999.0)
            await persist_markets(db, [m2])

            rows = await (await db.execute(
                "SELECT COUNT(*), MAX(liquidity) FROM rec_markets"
            )).fetchall()
            assert rows[0][0] == 1, "re-seeing a market must not duplicate it"
            assert rows[0][1] == pytest.approx(999.0), "liquidity must refresh"

            # Two scans, two census rows: the census is a time series.
            n = await (await db.execute("SELECT COUNT(*) FROM rec_discovery")).fetchall()
            assert n[0][0] == 2

    asyncio.run(run())


def test_book_row_round_trips_with_no_paired_reference_price(tmp_path: Path):
    """A missing S must store as NULL, not crash or fabricate a value."""

    async def run() -> None:
        async with aiosqlite.connect(tmp_path / "t.db") as db:
            await db.executescript(SCHEMA)
            await db.execute(
                """INSERT INTO rec_books
                   (slug, token_id, role, book_recv_ms, bids_json, asks_json,
                    bid_levels, ask_levels, s_mid, s_age_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                ("s", "t", "up", 1, json.dumps([[0.9, 1.0]]), json.dumps([]), 1, 0, None, None),
            )
            await db.commit()
            row = await (await db.execute(
                "SELECT bids_json, s_mid, s_age_ms FROM rec_books"
            )).fetchall()
            assert json.loads(row[0][0]) == [[0.9, 1.0]]
            assert row[0][1] is None and row[0][2] is None

    asyncio.run(run())
