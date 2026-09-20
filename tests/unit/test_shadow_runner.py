"""Shadow forward-tester runner: build_view mapping + record/settle end-to-end.

Exercises the integration seam the parallel module tests don't cover: a real
``PaperSnapshot`` flowing through ``record_shadow`` into the ledger, and the
net-of-fee settlement of the logged candidates.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

import db as _db
from polymarket_bot import strategy
from polymarket_bot.paper import PaperSnapshot
from polymarket_bot.shadow import runner
from polymarket_bot.shadow.fees import net_pnl_per_share
from polymarket_bot.shadow.ledger import settle_open_shadow


@pytest.fixture
def params() -> strategy.StrategyParams:
    return strategy.StrategyParams(
        min_trade_usd=1.0,
        max_trade_usd=5.0,
        entry_edge_min=0.045,
        entry_edge_max=0.07,
        min_confidence=0.50,
        entry_min_remaining_seconds=60,
        min_entry_price=0.50,
        max_entry_price=0.95,
    )


@pytest_asyncio.fixture
async def test_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "t.db")
    await _db.init_db()
    return _db


def _snapshot(**overrides: object) -> PaperSnapshot:
    base = dict(
        created_at="2026-06-18T00:00:00+00:00",
        window_slug="btc-updown-5m-1700000000",
        market_question="Bitcoin Up or Down?",
        remaining_seconds=120,
        spot_price=64020.0,
        reference_price=63990.0,
        sigma_per_second=2.5e-5,
        market_up_price=0.56,
        market_down_price=0.46,
        fair_up_prob=0.62,
        edge=0.06,
        signal_side="Up",
        confidence=0.67,
        notional_usd=0.0,
        reason="",
        feed_source="binance",
        up_best_bid=0.55,
        up_best_ask=0.56,
        down_best_bid=0.44,
        down_best_ask=0.46,
        quote_source="clob",
    )
    base.update(overrides)
    return PaperSnapshot(**base)  # type: ignore[arg-type]


async def _models_for(db, window_slug: str) -> dict[str, dict]:
    async with db.connect() as conn:
        async with conn.execute(
            "SELECT * FROM model_shadow_positions WHERE window_slug = ?",
            (window_slug,),
        ) as cur:
            return {r["model_id"]: dict(r) for r in await cur.fetchall()}


def test_build_view_maps_fields_and_mid() -> None:
    view = runner.build_view(_snapshot())
    assert view.up_ask == 0.56 and view.down_ask == 0.46
    assert view.spot == 64020.0 and view.reference == 63990.0
    assert view.fair_up == 0.62
    # market_up_price is the MID of the Up book, not the ask.
    assert view.market_up_price == pytest.approx((0.55 + 0.56) / 2)


@pytest.mark.asyncio
async def test_favorite_window_logs_v0_and_cushion(test_db, params) -> None:
    await runner.record_shadow(_snapshot(), params)
    rows = await _models_for(test_db, "btc-updown-5m-1700000000")
    # A clean cushioned favourite: v0 fires AND the cushion gate passes.
    assert "pricing_v0" in rows
    assert "cushion_favorite_v2" in rows
    # 180s into the window: the freshness gates (<=60s) skip v7 AND v8 even
    # though v2 trades — the whole point of the fresh family (#142, #144).
    assert "cushion_fresh_v7" not in rows
    assert "pricing_fresh_v8" not in rows
    assert rows["cushion_favorite_v2"]["side"] == "Up"
    assert rows["cushion_favorite_v2"]["entry_price"] == pytest.approx(0.56)
    assert rows["cushion_favorite_v2"]["shares"] == runner.SHADOW_SHARES


@pytest.mark.asyncio
async def test_fresh_window_logs_v7_too(test_db, params) -> None:
    """First 60s of the window + modest edge claim -> v7 logs alongside v2."""
    await runner.record_shadow(_snapshot(remaining_seconds=250), params)
    rows = await _models_for(test_db, "btc-updown-5m-1700000000")
    assert "cushion_fresh_v7" in rows
    assert rows["cushion_fresh_v7"]["side"] == "Up"
    assert rows["cushion_fresh_v7"]["entry_price"] == pytest.approx(0.56)
    assert rows["cushion_fresh_v7"]["reason"].startswith("fresh 50s;")
    # v8 (freshness alone) fires on the same fresh window.
    assert "pricing_fresh_v8" in rows
    assert rows["pricing_fresh_v8"]["reason"].startswith("fresh 50s;")
    # ...but f45 (≤45s gate, #155) does NOT fire at 50s elapsed — that is the
    # whole point of the tighter freshness window.
    assert "cushion_fresh_v7_f45" not in rows


@pytest.mark.asyncio
async def test_very_fresh_window_logs_f45_alongside_v7(test_db, params) -> None:
    """≤45s into the window: f45 (#155) logs alongside v7, and records the
    decision-time market state for the #122 regime axes."""
    await runner.record_shadow(_snapshot(remaining_seconds=270), params)  # 30s elapsed
    rows = await _models_for(test_db, "btc-updown-5m-1700000000")
    assert "cushion_fresh_v7_f45" in rows
    f45 = rows["cushion_fresh_v7_f45"]
    assert f45["side"] == "Up"
    assert f45["reason"].startswith("fresh 30s;")
    # v7 (≤60s) fires on the same window — additive, not a replacement.
    assert "cushion_fresh_v7" in rows
    # #122: the runner populated the regime columns from the snapshot.
    assert f45["spot_at_decision"] == pytest.approx(64020.0)
    assert f45["reference_at_decision"] == pytest.approx(63990.0)
    assert f45["sigma_per_second"] == pytest.approx(2.5e-5)


@pytest.mark.asyncio
async def test_recording_is_idempotent_per_window(test_db, params) -> None:
    await runner.record_shadow(_snapshot(), params)
    await runner.record_shadow(_snapshot(market_up_price=0.58, up_best_ask=0.58), params)
    rows = await _models_for(test_db, "btc-updown-5m-1700000000")
    # The first signal per (window, model) wins; the second tick is dropped.
    assert rows["cushion_favorite_v2"]["entry_price"] == pytest.approx(0.56)


@pytest.mark.asyncio
async def test_settle_books_net_of_fee_pnl(test_db, params) -> None:
    await runner.record_shadow(_snapshot(), params)
    settled = await settle_open_shadow(
        window_slug="btc-updown-5m-1700000000",
        outcome_side="Up",
        settlement_price=1.0,
        resolved_at="2026-06-18T00:05:00+00:00",
    )
    assert settled >= 2  # v0 + cushion both resolved
    rows = await _models_for(test_db, "btc-updown-5m-1700000000")
    row = rows["cushion_favorite_v2"]
    assert row["state"] == "settled" and row["outcome"] == "Up"
    expected = runner.SHADOW_SHARES * net_pnl_per_share(0.56, won=True)
    assert row["realized_pnl_usd"] == pytest.approx(expected)
    assert row["realized_pnl_usd"] > 0  # cushioned favourite that won, net of fee


def test_model_registry_constants() -> None:
    assert runner.DEFAULT_MODEL == "pricing_v0"
    # Post-surgery roster (#142): control, champion, challenger — retired
    # models (v3/v4/v5/v6) are neither logged nor selectable nor dispatchable.
    # #155 added cushion_fresh_v7_f45 (the #149 replay winner) as a 5th arm;
    # additive only — the racing specs v0/v2/v7/v8 are unchanged.
    expected = [
        "pricing_v0",
        "cushion_favorite_v2",
        "cushion_fresh_v7",
        "pricing_fresh_v8",
        "cushion_fresh_v7_f45",
    ]
    assert list(runner.MODEL_IDS) == expected
    assert set(runner.CANDIDATE_SIGNALS) == {
        "cushion_favorite_v2",
        "cushion_fresh_v7",
        "pricing_fresh_v8",
        "cushion_fresh_v7_f45",
    }
    for retired in (
        "late_convergence_v3",
        "down_skeptic_v4",
        "cushion_drift_v5",
        "down_skeptic_drift_v6",
    ):
        assert retired not in runner.MODEL_IDS
        assert retired not in runner.CANDIDATE_SIGNALS
    assert "pricing_v0" not in runner.CANDIDATE_SIGNALS
    # every logged id has a label + description for the dashboard
    for mid in runner.MODEL_IDS:
        assert mid in runner.MODEL_LABELS and mid in runner.MODEL_DESCRIPTIONS


def test_candidate_signal_dispatch(params) -> None:
    """The live-dispatch helper routes to the candidate, and v0/unknown -> None."""
    view = runner.build_view(_snapshot())  # cushioned Up favourite (see _snapshot)
    # v0 and unknown ids return None — the caller uses the native v0 path.
    assert runner.candidate_signal("pricing_v0", view, params) is None
    assert runner.candidate_signal("not_a_model", view, params) is None
    # a real candidate dispatches to its function.
    sig = runner.candidate_signal("cushion_favorite_v2", view, params)
    assert sig is not None and sig.side == "Up"
