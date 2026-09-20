"""Every figure quoted beside another must cover the SAME rows.

This bug has shipped four times: SUM(real_pnl) vs SUM(pnl); the target's P&L
over 2 rows vs ours over 26; filled and unfilled orders averaged together;
per-target totals hidden behind an empty breakdown. Each looked like a finding
and each was a mismatched population.

SQL aggregates make it easy — SUM() silently skips NULLs, so two sums over the
same table can cover different trades and still sit on the same line. These
tests build a ledger with deliberately mixed NULLs and pin the invariant.
"""

from __future__ import annotations

import pytest

import db as _db  # type: ignore[import-untyped]
from polymarket_bot.copytrade import ledger as _ledger


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Point the ledger at a throwaway database.

    These tests DELETE rows. Without this they run against the live operator
    database and destroy real paper-trading history — which is exactly what
    happened once. Autouse so a new test in this file cannot forget it.
    """
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "copytrade_test.db")
    return tmp_path


async def _row(**kw):
    base = dict(
        target="0xt", target_label="t-x", tx=f"0x{kw.pop('i')}", condition_id="0xc",
        token_id="1", window_slug="s", title="m", outcome="Up",
        their_size=10.0, their_price=0.50, our_price=0.50, size=10.0,
        fee=0.1, cost_usd=5.1, slippage=0.0, their_ts=1, our_ts=1,
        resolves_at=None,
    )
    base.update(kw)
    await _ledger.record(**base)


@pytest.mark.asyncio
async def test_realistic_total_is_paired_with_the_paper_total_for_the_same_rows():
    await _ledger.init()
    async with _db.connect() as c:
        await c.execute("DELETE FROM copy_trades")
        await c.commit()

    for i in range(4):
        await _row(i=i)
    rows = await _ledger.open_rows()
    assert len(rows) == 4

    # Two rows get re-quoted and settle with a realistic P&L; two never do.
    for r in rows[:2]:
        await _ledger.set_requote(r["id"], price=0.52, fee=0.1, cost=5.3,
                                  slippage=0.02, size=10.0, now=10)
    for n, r in enumerate(rows):
        real = -0.30 if n < 2 else None
        await _ledger.settle(r["id"], won=False, pnl=-5.10, real_pnl=real,
                             their_pnl=-5.00, now=20)

    s = await _ledger.summary()
    t = s["total"]
    assert t["n"] == 4
    assert t["real_n"] == 2, "real_n must count only rows carrying a realistic P&L"
    # The trap: SUM(pnl) covers 4 rows, SUM(real_pnl) covers 2.
    assert t["pnl"] == pytest.approx(-20.40)
    assert t["real_pnl"] == pytest.approx(-0.60)
    # paper_on_real must cover exactly the rows real_pnl covers, so the two can
    # honestly be shown side by side.
    assert t["paper_on_real"] == pytest.approx(-10.20)
    assert t["paper_on_real"] == pytest.approx(t["pnl"] / 4 * t["real_n"])


@pytest.mark.asyncio
async def test_the_fidelity_comparison_covers_one_population():
    """Their return and ours must be totalled over the same matched rows."""
    await _ledger.init()
    async with _db.connect() as c:
        await c.execute("DELETE FROM copy_trades")
        await c.commit()

    for i in range(3):
        await _row(i=i)
    rows = await _ledger.open_rows()
    # Only the first row records the target's own result.
    for n, r in enumerate(rows):
        await _ledger.settle(r["id"], won=True, pnl=4.90,
                             real_pnl=4.70 if n == 0 else None,
                             their_pnl=5.00 if n == 0 else None, now=20)

    t = (await _ledger.summary())["total"]
    assert t["matched_n"] == 1
    assert t["their_pnl"] == pytest.approx(5.00)
    # Ours over the SAME single row — not 3 rows' worth of P&L.
    assert t["matched_pnl"] == pytest.approx(4.70)
    assert t["matched_staked"] == pytest.approx(5.10)
    assert t["their_staked"] == pytest.approx(5.00)


@pytest.mark.asyncio
async def test_an_unfilled_copy_settles_flat_not_as_a_winner():
    """The single biggest way a paper ledger flatters itself."""
    await _ledger.init()
    async with _db.connect() as c:
        await c.execute("DELETE FROM copy_trades")
        await c.commit()

    await _row(i=9)
    r = (await _ledger.open_rows())[0]
    # Book gone at re-quote: nothing obtainable.
    await _ledger.set_requote(r["id"], price=None, fee=0.0, cost=0.0,
                              slippage=None, size=0.0, now=10)
    await _ledger.settle(r["id"], won=True, pnl=4.90, real_pnl=0.0, now=20)

    t = (await _ledger.summary())["total"]
    assert t["pnl"] == pytest.approx(4.90), "paper books the win"
    assert t["real_pnl"] == pytest.approx(0.0), "reality books nothing"
    assert t["never_filled"] == 1
