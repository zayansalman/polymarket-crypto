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
from polymarket_bot.copytrade import backup as _backup
from polymarket_bot.copytrade import ledger as _ledger
from polymarket_bot.copytrade import targets as _targets
from polymarket_bot.copytrade import trader as _trader

log = structlog.get_logger(__name__)

STRATEGY = "copy_macro_daily"
AUTOCOPY_STRATEGY = "copy_autocopy"

# The daily Up-or-Down families these wallets were measured on. A fill outside
# them is still shown, tagged 'other', rather than silently dropped — a target
# quietly changing what it trades is the failure mode that killed two earlier
# candidates, so it has to be visible.
FOLLOWED_SLUG_MARKER = "-up-or-down-on-"


def _fill_key(tx: str, size: float, price: float) -> str:
    """Stable identity for one fill.

    Built from PARSED values on both sides of the comparison: the API returns
    10 and 10.0 interchangeably, and keying off the raw JSON meant a fill never
    matched itself.
    """
    return f"{tx}:{size:.6f}:{price:.6f}"


@dataclass
class ObservedFill:
    """One fill by the target, as the dashboard shows it."""

    tx: str
    ts: int
    wallet: str
    """Which followed wallet made it. The watcher polls several and merges
    their fills into one list, so without this every per-wallet number on the
    dashboard is really a number about whichever wallet was polled last."""
    side: str
    outcome: str
    size: float
    price: float
    title: str
    slug: str
    condition_id: str
    token_id: str
    followed: bool
    """True when this is one of the daily Up-or-Down markets we mirror."""
    observed_at: float = 0.0
    backfill: bool = False
    resolves_at: int | None = None
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
    watching: list[str] = field(default_factory=list)
    drift: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    """address -> (label, fills on measured markets, fills seen). A target that
    has moved to markets it was never measured on cannot be copied on that
    evidence, and this is the number that says so."""
    copies_opened: int = 0
    settled_total: int = 0
    last_snapshot: float = 0.0
    enabled: bool = False
    autocopy: bool = False
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

    def fills_for(self, wallet: str) -> list[ObservedFill]:
        """This wallet's fills, newest first."""
        w = wallet.lower()
        return [f for f in self.fills if f.wallet.lower() == w]

    def recent_count(self, wallet: str, *, since: float) -> int:
        """Fills this wallet made at or after ``since`` (epoch seconds)."""
        return sum(1 for f in self.fills_for(wallet) if f.ts >= since)

    def find_fill(self, tx: str) -> ObservedFill | None:
        """Look one fill up by transaction hash — what the Copy button posts."""
        for f in self.fills:
            if f.tx == tx:
                return f
        return None


class CopyWatcher:
    """Polls one target wallet and keeps the latest observations in memory."""

    MAX_FILLS = 200

    def __init__(self) -> None:
        self.state = WatcherState()
        # Per target: the newest fill timestamp already processed, plus the
        # transaction keys seen AT that exact second (the API returns whole
        # seconds, so several fills can share the watermark).
        self._watermark: dict[str, int] = {}
        self._at_watermark: dict[str, set[str]] = {}
        self._polled: set[str] = set()

    @staticmethod
    def _is_followed(slug: str, target) -> bool:
        return _is_followed_impl(slug, target)

    async def _addresses(self) -> list[str]:
        """Which wallets to watch this tick.

        Following all of them is the default: each is an independent experiment
        and the point of a paper lab is running more than one at once.
        """
        if bool(await _knobs.get("copy_follow_all")):
            return list(_targets.TARGETS)
        raw = str(await _knobs.get("copy_target_wallet") or "").strip().lower()
        return [raw if _targets.get(raw) else _targets.DEFAULT_TARGET]

    async def poll_once(self, client: httpx.AsyncClient) -> None:
        """One pass over every watched wallet, then settle anything resolved."""
        self.state.enabled = await _strategies.enabled(STRATEGY)
        self.state.autocopy = await _strategies.enabled(AUTOCOPY_STRATEGY)
        # Off stops OBSERVING, never settlement. Returning here used to skip
        # the settle/requote block below, so switching the watcher off left
        # every open copy unsettled — the one thing the switch contract in
        # ``polymarket_bot.strategies`` promises can never happen.
        if self.state.enabled:
            addresses = await self._addresses()
            self.state.watching = addresses
            for address in addresses:
                await self._poll_target(client, address)
        try:
            await _trader.requote_due(client)
        except Exception:  # noqa: BLE001
            log.exception("copytrade.requote_failed")
        # Snapshot the record periodically. The live file has been cleared
        # once already with nothing to restore from.
        if time.time() - self.state.last_snapshot > 1800:
            self.state.last_snapshot = time.time()
            try:
                await asyncio.to_thread(_backup.write_snapshot)
            except Exception:  # noqa: BLE001
                log.exception("copytrade.snapshot_failed")

        try:
            await _trader.settle_skips(client)
        except Exception:  # noqa: BLE001
            log.exception("copytrade.skip_settle_failed")
        try:
            settled = await _trader.settle_due(client)
            if settled:
                self.state.settled_total += settled
        except Exception:  # noqa: BLE001
            log.exception("copytrade.settle_failed")

    async def _poll_target(self, client: httpx.AsyncClient, address: str) -> None:
        target = _targets.get(address)
        self.state.target = address
        self.state.label = target.label if target else address[:10]

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
        is_backfill = address not in self._polled
        mark = self._watermark.get(address, 0)
        at_mark = self._at_watermark.get(address, set())
        fresh: list[ObservedFill] = []
        for row in rows:
            if row.get("type") != "TRADE":
                continue
            tx = str(row.get("transactionHash") or "")
            slug = str(row.get("slug") or "")
            ts = int(row.get("timestamp") or 0)
            size = float(row.get("size") or 0.0)
            price = float(row.get("price") or 0.0)
            key = _fill_key(tx, size, price)
            # A timestamp watermark is bounded by construction. The previous
            # key-set was global across every target and was trimmed once it
            # passed a cap, which made thousands of old fills look new again on
            # the next poll.
            if ts < mark or (ts == mark and key in at_mark):
                continue
            fresh.append(
                ObservedFill(
                    tx=tx,
                    ts=int(row.get("timestamp") or 0),
                    wallet=address,
                    side=str(row.get("side") or ""),
                    outcome=str(row.get("outcome") or ""),
                    size=size,
                    price=price,
                    title=str(row.get("title") or ""),
                    slug=slug,
                    condition_id=str(row.get("conditionId") or ""),
                    token_id=str(row.get("asset") or ""),
                    followed=self._is_followed(slug, target),
                    observed_at=now,
                    backfill=is_backfill,
                )
            )

        if rows:
            # Only now is this target's history actually known. Marking it on a
            # failed or empty fetch would leave the watermark at 0, and the next
            # successful poll would treat every past fill as new.
            self._polled.add(address)

        if fresh:
            newest = max(f.ts for f in fresh)
            if newest > mark:
                self._watermark[address] = newest
                self._at_watermark[address] = {
                    _fill_key(f.tx, f.size, f.price) for f in fresh if f.ts == newest
                }
            else:
                at_mark |= {
                    _fill_key(f.tx, f.size, f.price) for f in fresh if f.ts == mark
                }
                self._at_watermark[address] = at_mark

        # Copy anything new and followable. Backfill is history — copying it
        # would book positions in markets that already settled.
        max_age = float(await _knobs.get("copy_max_fill_age_seconds"))
        # Autocopy is a separate switch from watching (#copy-wallets card).
        # Off, the fills still arrive and still render — each one carries a
        # Copy button instead, so the operator books the ones they want.
        autocopy = await _strategies.enabled(AUTOCOPY_STRATEGY)
        if not is_backfill and autocopy:
            for f in fresh:
                # Belt and braces against any bookkeeping slip: a fill this old
                # is unfollowable by definition, so never spend a book lookup
                # or a ledger row on it.
                if max_age > 0 and f.lag_seconds > max_age:
                    continue
                try:
                    if await _trader.consider(client, f, address):
                        self.state.copies_opened += 1
                except Exception:  # noqa: BLE001
                    log.exception("copytrade.copy_failed", tx=f.tx[:14])

        followed_n = sum(1 for f in fresh if f.followed)
        prev = self.state.drift.get(address, (self.state.label, 0, 0))
        self.state.drift[address] = (
            self.state.label, prev[1] + followed_n, prev[2] + len(fresh)
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



def _is_followed_impl(slug: str, target) -> bool:
    """Whether a fill is on a market family this target was measured on.

    Intraday targets trade ``*-up-or-down-<date>-<hour>am-et`` and the 15m
    family; daily targets trade ``*-up-or-down-on-<date>``. Anything else is a
    market the wallet was never measured on and must not be copied blind.
    """
    if target is not None and target.cadence == "intraday":
        return "-up-or-down-" in slug or "-updown-15m-" in slug
    return FOLLOWED_SLUG_MARKER in slug


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    """Poll until ``stop_event`` is set (or forever if ``None``)."""
    await _ledger.init()
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
