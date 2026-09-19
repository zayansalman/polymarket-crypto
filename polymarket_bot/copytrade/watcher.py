"""Watch a target wallet's fills and show them, before mirroring anything.

First half of copy-trading: see what the wallet does, in the dashboard, with no
orders and no ledger writes. Everything it observes is on screen — a copier that
decides in private is not something an operator can judge.

Transport is the public data-api activity feed. It runs ~20s behind the chain.
That is a deliberate choice, not a shortcut: the wallets this follows trade DAILY
Up-or-Down markets with a median 2-3.5 hours to settlement, and a tape replay
shows a copier entering 30 minutes late still keeps their edge. The same feed
would be indefensible on 15m/1h markets, where this repo measured 9.56c median
slippage at 11-30s lag against edges under 1c — that case needs the Polygon
websocket in ``pairarb/feed.py``.

Off means "observe nothing new", never "abandon": the switch is read at the top
of the tick, and nothing here holds state that could be stranded.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

import config as _config
from polymarket_bot import runtime_knobs as _knobs
from polymarket_bot import strategies as _strategies
from polymarket_bot.copytrade import targets as _targets

log = structlog.get_logger(__name__)

STRATEGY = "copy_macro_daily"

# The daily Up-or-Down families these wallets were measured on. A fill outside
# them is still shown, tagged 'other', rather than silently dropped — a target
# quietly changing what it trades is the failure mode that killed two earlier
# candidates, so it has to be visible.
FOLLOWED_SLUG_MARKER = "-up-or-down-on-"


@dataclass
class ObservedFill:
    """One fill by the target, as the dashboard shows it."""

    tx: str
    ts: int
    side: str
    outcome: str
    size: float
    price: float
    title: str
    slug: str
    followed: bool
    """True when this is one of the daily Up-or-Down markets we mirror."""
    observed_at: float = 0.0
    backfill: bool = False
    """Seen on the first poll, so its age is the backlog's, not the feed's."""

    @property
    def lag_seconds(self) -> float:
        """How stale the observation was when we first saw it."""
        return max(0.0, self.observed_at - self.ts)

    @property
    def notional(self) -> float:
        return self.size * self.price


@dataclass
class WatcherState:
    """What the dashboard renders. Plain data, no I/O."""

    target: str = ""
    label: str = ""
    enabled: bool = False
    connected: bool = False
    last_poll: float = 0.0
    last_error: str = ""
    polls: int = 0
    fills: list[ObservedFill] = field(default_factory=list)

    @property
    def followed_fills(self) -> list[ObservedFill]:
        return [f for f in self.fills if f.followed]

    @property
    def median_lag(self) -> float:
        """Median feed lag, excluding the first poll's backlog.

        The first poll returns history, so those fills are hours old through no
        fault of the transport. Counting them would make the one number that
        decides whether copying is viable read as unusable on every restart.
        """
        lags = sorted(
            f.lag_seconds for f in self.fills if f.observed_at and not f.backfill
        )
        if not lags:
            return 0.0
        return lags[len(lags) // 2]

    @property
    def live_fills(self) -> int:
        return sum(1 for f in self.fills if not f.backfill)


class CopyWatcher:
    """Polls one target wallet and keeps the latest observations in memory."""

    MAX_FILLS = 200

    def __init__(self) -> None:
        self.state = WatcherState()
        self._seen: set[str] = set()

    async def _target_address(self) -> str:
        raw = await _knobs.get("copy_target_wallet")
        text = str(raw or "").strip().lower()
        if _targets.get(text) is None:
            return _targets.DEFAULT_TARGET
        return text

    async def poll_once(self, client: httpx.AsyncClient) -> None:
        """One pass. Catches its own failures so the loop cannot die here."""
        address = await self._target_address()
        target = _targets.get(address)
        self.state.target = address
        self.state.label = target.label if target else address[:10]
        self.state.enabled = await _strategies.enabled(STRATEGY)
        if not self.state.enabled:
            return

        limit = int(await _knobs.get("copy_observe_limit"))
        try:
            resp = await client.get(
                f"{_config.POLYMARKET_DATA_API}/activity",
                params={"user": address, "limit": limit},
                timeout=20.0,
            )
            resp.raise_for_status()
            rows: list[dict[str, Any]] = resp.json()
        except Exception as exc:  # noqa: BLE001 — surfaced, never raised
            self.state.connected = False
            self.state.last_error = f"{type(exc).__name__}: {exc}"[:160]
            log.warning("copytrade.poll_failed", target=address, error=str(exc))
            return

        now = time.time()
        self.state.connected = True
        self.state.last_error = ""
        self.state.last_poll = now
        self.state.polls += 1

        # The first poll is a backfill of history; only fills seen after that
        # measure the transport.
        is_backfill = self.state.polls == 1
        fresh: list[ObservedFill] = []
        for row in rows:
            if row.get("type") != "TRADE":
                continue
            tx = str(row.get("transactionHash") or "")
            slug = str(row.get("slug") or "")
            key = f"{tx}:{row.get('size')}:{row.get('price')}"
            if key in self._seen:
                continue
            self._seen.add(key)
            fresh.append(
                ObservedFill(
                    tx=tx,
                    ts=int(row.get("timestamp") or 0),
                    side=str(row.get("side") or ""),
                    outcome=str(row.get("outcome") or ""),
                    size=float(row.get("size") or 0.0),
                    price=float(row.get("price") or 0.0),
                    title=str(row.get("title") or ""),
                    slug=slug,
                    followed=FOLLOWED_SLUG_MARKER in slug,
                    observed_at=now,
                    backfill=is_backfill,
                )
            )

        if fresh:
            self.state.fills = (fresh + self.state.fills)[: self.MAX_FILLS]
            log.info(
                "copytrade.observed",
                target=address,
                new=len(fresh),
                followed=sum(1 for f in fresh if f.followed),
                median_lag=round(self.state.median_lag, 1),
            )

        # Bound the dedupe set to the same horizon as the fills we keep.
        if len(self._seen) > self.MAX_FILLS * 10:
            keep = {f"{f.tx}:{f.size}:{f.price}" for f in self.state.fills}
            self._seen = keep


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    """Poll until ``stop_event`` is set (or forever if ``None``)."""
    watcher = CopyWatcher()
    set_current(watcher)
    async with httpx.AsyncClient(timeout=20.0) as client:
        while stop_event is None or not stop_event.is_set():
            # The interval read is inside the guard on purpose: it is a SQLite
            # read that can raise, and nothing supervises this task, so an
            # escape here would stop the watcher silently for the process life.
            try:
                await watcher.poll_once(client)
                interval = float(await _knobs.get("copy_poll_interval_seconds"))
            except Exception:  # noqa: BLE001
                log.exception("copytrade.tick_failed")
                interval = 30.0
            await asyncio.sleep(interval)


_current: CopyWatcher | None = None


def set_current(watcher: CopyWatcher | None) -> None:
    global _current
    _current = watcher


def current() -> CopyWatcher | None:
    return _current
