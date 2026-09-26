"""Endpoints and LIVE arming (``ems/execution/endpoints.py``, ``live_control.py``).

LIVE is armed only while it is selected, clicked in this process and the wallet passes; a saved
LIVE or ``BOT_MODE=live`` is never consent; the kill switch turns both endpoints off; a live
venue is opened for orders still to follow even after LIVE is turned off.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from ems import config as _config
from ems import db as _db
from ems.execution import clob, controls, endpoints, live_control
from ems.execution.gate import RiskGate

pytestmark = pytest.mark.asyncio


class Venue:
    mode = "live"


@pytest_asyncio.fixture(autouse=True)
async def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(_db, "DB_PATH", tmp_path / "ep.db")
    monkeypatch.setattr(_config, "KILL_SWITCH_PATH", tmp_path / "KILL")
    monkeypatch.setattr(_config, "BOT_MODE", "paper")
    monkeypatch.setattr(clob, "wallet_problems", lambda: [])
    await _db.init_db()
    live_control.record_click("paper")
    yield tmp_path
    live_control.record_click("paper")


def holder(*, fails: bool = False, clock=None) -> live_control.LiveVenueHolder:
    opened = []

    async def opener():
        opened.append(1)
        if fails:
            raise clob.LiveUnavailable("the exchange sign-in failed (HTTP 401)")
        return Venue()

    h = live_control.LiveVenueHolder(opener=opener, clock=clock or (lambda: 1000.0))
    h.opened = opened  # type: ignore[attr-defined]
    return h


async def points(h, *, orders_open: bool = False):
    gates = {m: RiskGate(m) for m in ("paper", "live")}
    return await endpoints.endpoints(paper=object(), gates=gates, live=h,
                                     live_orders_open=orders_open)


async def test_paper_is_on_and_live_is_off_by_default() -> None:
    h = holder()
    got = await points(h)
    assert got["paper"].active and not got["live"].active
    assert got["live"].state == live_control.NOT_SELECTED and h.opened == []


async def test_a_saved_live_is_not_consent() -> None:
    await live_control.select_mode("live", clicked=False)  # e.g. saved by an earlier run
    got = await points(holder())
    assert got["live"].state == live_control.NOT_CLICKED and not got["live"].active


async def test_bot_mode_live_in_the_env_is_not_consent(monkeypatch) -> None:
    monkeypatch.setattr(_config, "BOT_MODE", "live")
    assert await controls.requested_mode() == "live"
    got = await points(holder())
    assert got["live"].state == live_control.NOT_CLICKED


async def test_a_wallet_that_fails_keeps_live_off(monkeypatch) -> None:
    monkeypatch.setattr(clob, "wallet_problems", lambda: ["POLYMARKET_FUNDER is not set"])
    await live_control.select_mode("live", clicked=True)
    got = await points(holder())
    assert got["live"].state == live_control.NO_WALLET
    assert "POLYMARKET_FUNDER is not set" in got["live"].message


async def test_selected_clicked_and_a_good_wallet_arm_live() -> None:
    await live_control.select_mode("live", clicked=True)
    h = holder()
    got = await points(h)
    assert got["live"].active and got["live"].state == endpoints.ON
    assert isinstance(got["live"].venue, Venue) and h.opened == [1]
    await points(h)
    assert h.opened == [1]  # opened once


async def test_clicking_paper_disarms_live() -> None:
    await live_control.select_mode("live", clicked=True)
    await live_control.select_mode("paper", clicked=True)
    assert not live_control.clicked_live()
    assert not (await points(holder()))["live"].active


async def test_the_venue_opens_to_follow_orders_after_live_is_off() -> None:
    h = holder()
    got = await points(h, orders_open=True)
    assert not got["live"].active and isinstance(got["live"].venue, Venue)


async def test_a_failed_sign_in_is_shown_and_tried_again_later() -> None:
    now = {"t": 1000.0}
    h = holder(fails=True, clock=lambda: now["t"])
    await live_control.select_mode("live", clicked=True)
    got = await points(h)
    assert got["live"].state == endpoints.BOOT_FAILED and "HTTP 401" in got["live"].message
    await points(h)
    assert len(h.opened) == 1  # not before the retry pause
    now["t"] += live_control.RETRY_S
    await points(h)
    assert len(h.opened) == 2


async def test_the_kill_switch_turns_both_off(temp_db) -> None:
    await live_control.select_mode("live", clicked=True)
    (temp_db / "KILL").touch()
    got = await points(holder())
    assert not got["paper"].active and not got["live"].active
    assert got["paper"].state == got["live"].state == endpoints.KILL
    assert got["paper"].venue is not None  # bookkeeping still reaches paper


async def test_without_a_holder_there_is_no_live_endpoint() -> None:
    got = await points(None)
    assert got["live"].state == endpoints.NOT_BUILT and got["live"].venue is None


async def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        await live_control.select_mode("shadow", clicked=True)
