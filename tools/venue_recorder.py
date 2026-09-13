"""Venue recorder — the research program's one blocking build item (C17).

Records what the venue actually lists, at full depth, with the reference price
timestamp-aligned to book receipt. Recorded time cannot be backfilled, so this
captures more than any current question needs.

Three things it does that the 5m-era recorder
(``btc_5m_fv/storage/recorder.py``) does not, each traceable to a correction:

  full depth   every level of both books, not the touch. The 5m recorder kept
               only the best level (``polymarket_bot/paper.py::_best_level``), which is
               why no true L2 depth exists anywhere in this project's history.
               C6 requires the cost stack to become a size-dependent function
               "measured from recorded L2"; C17 records that the spec asking for
               that was unsatisfiable from a top-of-book capture.

  aligned S    the reference price is snapshotted AT BOOK RECEIPT and stored in
               the same row as the book, carrying both the exchange event time
               and our receipt time. C13: taking S at decision time against a
               quote of unknown age is errors-in-variables, and it produces
               k-hat > 0 under the exact null M2' exists to reject (~0.36 against
               a tested 0.5), concentrated in burst states — where the strategy
               claims to trade. ``s_age_ms`` is that bias term, made measurable.

  discovery    the ladder is DISCOVERED, never assumed. Every scan writes a
               census row. C7 killed a whole thesis because market structure was
               asserted from memory instead of a live query; a 2026-08-13 probe
               found no ``*-updown-1h-*`` family on the venue at all, against a
               design whose program of record is "1h + daily". Classification is
               stored alongside the raw evidence that produced it so it can be
               redone offline without re-recording.

Scope is deliberately wider than the program of record: recording a family that
turns out to be irrelevant costs storage, while failing to record one that turns
out to matter costs the calendar time it covered.

Usage::

    python tools/venue_recorder.py --discover          # census only, no writes
    python tools/venue_recorder.py --once              # one capture cycle
    python tools/venue_recorder.py --run               # continuous, >=1 Hz
    python tools/venue_recorder.py --run --assets btc,eth --hz 2
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import re
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import aiosqlite
import httpx

try:  # websockets is a hard dep of the project; degrade to REST if absent
    import websockets
except Exception:  # pragma: no cover - exercised only on a broken install
    websockets = None  # type: ignore[assignment]

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
BINANCE_REST = "https://api.binance.com"
BINANCE_WS = "wss://stream.binance.com:9443/stream"

UA = {"User-Agent": "polymarket-research-recorder/1.0", "Accept": "application/json"}

# Underlying spot symbol per Polymarket asset token. The reference series must be
# the one the venue settles on: the in-scope rungs settle on Binance spot candles,
# not Chainlink (C18).
SPOT_SYMBOL = {
    "btc": "BTCUSDT",
    "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT",
    "ethereum": "ETHUSDT",
    "sol": "SOLUSDT",
    "solana": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "dogecoin": "DOGEUSDT",
    "bnb": "BNBUSDT",
    "hype": "HYPEUSDT",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rec_markets (
    slug            TEXT PRIMARY KEY,
    condition_id    TEXT,
    asset           TEXT,
    family          TEXT,
    rung_guess      TEXT,
    rung_evidence   TEXT,
    question        TEXT,
    token_up        TEXT,
    token_down      TEXT,
    start_ts        INTEGER,
    end_ts          INTEGER,
    first_seen_ms   INTEGER,
    last_seen_ms    INTEGER,
    liquidity       REAL,
    volume          REAL,
    raw_json        TEXT
);

-- One row per (market, token) per capture. bids/asks hold EVERY level exactly as
-- the venue returned them (worst-to-best), so depth curves can be rebuilt offline.
CREATE TABLE IF NOT EXISTS rec_books (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    role            TEXT,
    book_recv_ms    INTEGER NOT NULL,
    venue_ts        INTEGER,
    book_hash       TEXT,
    bids_json       TEXT,
    asks_json       TEXT,
    best_bid        REAL,
    best_ask        REAL,
    bid_levels      INTEGER,
    ask_levels      INTEGER,
    bid_depth       REAL,
    ask_depth       REAL,
    s_symbol        TEXT,
    s_mid           REAL,
    s_bid           REAL,
    s_ask           REAL,
    s_exchange_ms   INTEGER,
    s_recv_ms       INTEGER,
    s_age_ms        INTEGER,
    s_source        TEXT
);
CREATE INDEX IF NOT EXISTS idx_books_slug_ts ON rec_books(slug, book_recv_ms);

CREATE TABLE IF NOT EXISTS rec_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slug            TEXT,
    token_id        TEXT,
    role            TEXT,
    trade_id        TEXT UNIQUE,
    price           REAL,
    size            REAL,
    side            TEXT,
    venue_ts        INTEGER,
    recv_ms         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trades_slug_ts ON rec_trades(slug, venue_ts);

-- The venue-structure census. This table IS a measurement, not bookkeeping: it
-- is what would have caught C7, and what caught the missing 1h family.
CREATE TABLE IF NOT EXISTS rec_discovery (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_ms         INTEGER NOT NULL,
    family          TEXT,
    rung_guess      TEXT,
    n_markets       INTEGER,
    n_accepting     INTEGER,
    med_liquidity   REAL,
    med_volume      REAL,
    example_slug    TEXT
);
CREATE INDEX IF NOT EXISTS idx_discovery_scan ON rec_discovery(scan_ms);

-- Capture-health counters. A gap in the record must be visible as a gap, never
-- inferred from missing rows.
CREATE TABLE IF NOT EXISTS rec_health (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms           INTEGER NOT NULL,
    cycle_ms        REAL,
    books_ok        INTEGER,
    books_err       INTEGER,
    trades_new      INTEGER,
    s_stale_ms      INTEGER,
    note            TEXT
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# Market discovery
# --------------------------------------------------------------------------- #

_UPDOWN_SLUG = re.compile(r"^(?P<asset>[a-z]+)-updown-(?P<rung>\d+[smhd])-(?P<ts>\d+)$")
_NAMED_SLUG = re.compile(r"^(?P<asset>[a-z]+)-up-or-down-", re.IGNORECASE)


def classify(market: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return (asset, family, rung_guess, rung_evidence).

    The guess is recorded next to the evidence that produced it. Nothing
    downstream should trust ``rung_guess`` without being able to re-derive it —
    that is the whole lesson of C7.
    """
    slug = (market.get("slug") or "").lower()
    question = market.get("question") or ""

    m = _UPDOWN_SLUG.match(slug)
    if m:
        asset = m.group("asset")
        rung = m.group("rung")
        return asset, f"{asset}-updown-{rung}", rung, f"slug pattern {slug!r}"

    m = _NAMED_SLUG.match(slug)
    if m:
        asset = m.group("asset").lower()
        family = "-".join(p for p in slug.split("-") if not p.isdigit())
        # Named-date markets state their settlement hour in the question, e.g.
        # "Bitcoin Up or Down - May 20, 6AM ET". Window length is not stated, so
        # it is derived from the venue's own dates where both are present.
        rung = "named"
        evidence = f"slug pattern {slug!r}; question {question!r}"
        start, end = _epoch(market.get("startDate")), _epoch(market.get("endDate"))
        if start and end:
            hours = (end - start) / 3600.0
            rung = f"~{hours:.0f}h"
            evidence += f"; endDate-startDate = {hours:.1f}h"
        return asset, family, rung, evidence

    family = "-".join(p for p in slug.split("-") if not p.isdigit())
    return "", family, "unknown", f"unmatched slug {slug!r}"


def _epoch(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _tokens(market: dict[str, Any]) -> tuple[str | None, str | None]:
    raw = market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, None
    if isinstance(raw, list) and len(raw) >= 2:
        return str(raw[0]), str(raw[1])
    return None, None


def _is_updown(market: dict[str, Any]) -> bool:
    q = (market.get("question") or "").lower()
    s = (market.get("slug") or "").lower()
    return "up or down" in q or "updown" in s


async def discover(client: httpx.AsyncClient, max_pages: int = 25) -> list[dict[str, Any]]:
    """Page the Gamma API for every open up/down market the venue lists.

    Two passes, because one is provably not enough: the API refuses offsets past
    ~2100, so an unordered sweep silently truncates. Ordering by ``endDate``
    ascending surfaces the soonest-expiring markets — the ones with a live book —
    while the unordered pass catches long-dated families the first pass' ceiling
    would cut off. A family missed here is a family never recorded, and recorded
    time cannot be backfilled, so the redundancy is deliberate.
    """
    found: dict[str, dict[str, Any]] = {}
    passes = (
        {"active": "true", "closed": "false", "order": "endDate", "ascending": "true"},
        {"active": "true", "closed": "false"},
        {"closed": "false", "order": "endDate", "ascending": "true"},
    )
    for base in passes:
        for page in range(max_pages):
            params = dict(base, limit="100", offset=str(page * 100))
            try:
                r = await client.get(f"{GAMMA_API}/markets", params=params)
                r.raise_for_status()
                batch = r.json()
            except Exception:  # noqa: BLE001
                break  # offset ceiling or transient — the next pass covers it
            if not isinstance(batch, list) or not batch:
                break
            for m in batch:
                if _is_updown(m) and m.get("slug"):
                    found.setdefault(str(m["slug"]), m)
    return list(found.values())


# --------------------------------------------------------------------------- #
# Reference price — kept hot so it can be sampled AT book receipt (C13)
# --------------------------------------------------------------------------- #


@dataclass
class SpotQuote:
    symbol: str
    bid: float
    ask: float
    exchange_ms: int
    recv_ms: int
    source: str
    update_id: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


class SpotFeed:
    """Latest best bid/ask per symbol, with the exchange's own event time.

    The book poller samples this at the instant a book arrives, so every stored
    book carries the age of the reference price it is paired with. That age is
    the errors-in-variables term C13 identified; it cannot be recovered later
    from data recorded without it.
    """

    def __init__(self, symbols: Iterable[str]) -> None:
        self.symbols = sorted(set(symbols))
        self._quotes: dict[str, SpotQuote] = {}
        self._task: asyncio.Task[None] | None = None

    def get(self, symbol: str) -> SpotQuote | None:
        return self._quotes.get(symbol)

    def start(self) -> None:
        if websockets is not None and self.symbols:
            self._task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _ws_loop(self) -> None:
        streams = "/".join(f"{s.lower()}@bookTicker" for s in self.symbols)
        url = f"{BINANCE_WS}?streams={streams}"
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20) as ws:
                    backoff = 1.0
                    async for raw in ws:
                        recv = now_ms()
                        try:
                            payload = json.loads(raw).get("data", {})
                            sym = payload["s"]
                            self._quotes[sym] = SpotQuote(
                                symbol=sym,
                                bid=float(payload["b"]),
                                ask=float(payload["a"]),
                                # Spot @bookTicker carries NO exchange timestamp
                                # (only futures does), so exchange_ms stays 0 and
                                # the age term is measured on our receive clock.
                                # `u` is the book update id, kept so ordering is
                                # recoverable even when two quotes share a ms.
                                exchange_ms=int(payload.get("E") or payload.get("T") or 0),
                                update_id=int(payload.get("u") or 0),
                                recv_ms=recv,
                                source="binance_ws_bookTicker",
                            )
                        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                            continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"  ! spot ws reconnect in {backoff:.0f}s ({exc})", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def rest_refresh(self, client: httpx.AsyncClient) -> None:
        """REST fallback for when the websocket is unavailable.

        Carries no exchange timestamp, so ``s_exchange_ms`` is stored NULL and
        the age term degrades to our own receipt clock. Recorded as a distinct
        source so the two can never be silently pooled in analysis.
        """
        for symbol in self.symbols:
            try:
                r = await client.get(
                    f"{BINANCE_REST}/api/v3/ticker/bookTicker", params={"symbol": symbol}
                )
                r.raise_for_status()
                d = r.json()
                self._quotes[symbol] = SpotQuote(
                    symbol=symbol,
                    bid=float(d["bidPrice"]),
                    ask=float(d["askPrice"]),
                    exchange_ms=0,
                    recv_ms=now_ms(),
                    source="binance_rest_bookTicker",
                )
            except Exception:  # noqa: BLE001
                continue


# --------------------------------------------------------------------------- #
# Book + trade capture
# --------------------------------------------------------------------------- #


def _levels(raw: Any) -> tuple[list[list[float]], float | None, float, int]:
    """Parse a CLOB level array.

    Returns (levels, best_price, total_size, n_levels). CLOB arrays run
    worst-to-best, so the best price is the LAST element — the same convention
    the live executor uses. Every level is kept.
    """
    if not isinstance(raw, list) or not raw:
        return [], None, 0.0, 0
    out: list[list[float]] = []
    total = 0.0
    for lvl in raw:
        try:
            price, size = float(lvl["price"]), float(lvl["size"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append([price, size])
        total += size
    best = out[-1][0] if out else None
    return out, best, total, len(out)


@dataclass
class Target:
    slug: str
    token_id: str
    role: str
    symbol: str | None
    last_trade_seen: str | None = None


@dataclass
class TradeTarget:
    """A market's public tape handle. Keyed on conditionId, not token id."""

    slug: str
    condition_id: str | None
    roles: dict[str, str] = field(default_factory=dict)


@dataclass
class CycleStats:
    books_ok: int = 0
    books_err: int = 0
    trades_new: int = 0
    s_stale_ms: int = 0
    errors: list[str] = field(default_factory=list)


async def capture_book(
    client: httpx.AsyncClient, db: aiosqlite.Connection, target: Target, spot: SpotFeed
) -> bool:
    try:
        r = await client.get(f"{CLOB_API}/book", params={"token_id": target.token_id})
        r.raise_for_status()
        data = r.json()
    except Exception:  # noqa: BLE001
        return False
    recv = now_ms()
    if not isinstance(data, dict):
        return False

    bids, best_bid, bid_depth, n_bids = _levels(data.get("bids"))
    asks, best_ask, ask_depth, n_asks = _levels(data.get("asks"))

    # Sample the reference price AT BOOK RECEIPT — this pairing is the point.
    q = spot.get(target.symbol) if target.symbol else None
    await db.execute(
        """INSERT INTO rec_books (
               slug, token_id, role, book_recv_ms, venue_ts, book_hash,
               bids_json, asks_json, best_bid, best_ask,
               bid_levels, ask_levels, bid_depth, ask_depth,
               s_symbol, s_mid, s_bid, s_ask, s_exchange_ms, s_recv_ms,
               s_age_ms, s_source
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            target.slug,
            target.token_id,
            target.role,
            recv,
            _int_or_none(data.get("timestamp")),
            data.get("hash"),
            json.dumps(bids, separators=(",", ":")),
            json.dumps(asks, separators=(",", ":")),
            best_bid,
            best_ask,
            n_bids,
            n_asks,
            bid_depth,
            ask_depth,
            q.symbol if q else None,
            q.mid if q else None,
            q.bid if q else None,
            q.ask if q else None,
            (q.exchange_ms or None) if q else None,
            q.recv_ms if q else None,
            (recv - q.recv_ms) if q else None,
            q.source if q else None,
        ),
    )
    return True


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


async def capture_trades(
    client: httpx.AsyncClient, db: aiosqlite.Connection, market: TradeTarget
) -> int:
    """Append the public venue trade tape for one market.

    Item 6 of the build list wants a cost model measured from recorded L2; the
    depth curve alone gives the quoted cost, and only the tape says what size
    actually traded through it.

    The tape is keyed on ``conditionId`` and served by the DATA API. The CLOB's
    own ``/trades`` is the authenticated endpoint for one's own fills and returns
    401 to a public caller — an earlier revision of this file polled it and
    silently recorded nothing, which is precisely the failure this project keeps
    finding in its own history. Errors are therefore counted, not swallowed.

    Duplicate ids are ignored, so overlapping polls are safe and no offline
    de-duplication pass is needed.
    """
    if not market.condition_id:
        return 0
    try:
        r = await client.get(
            f"{DATA_API}/trades",
            params={"market": market.condition_id, "limit": "500"},
        )
        r.raise_for_status()
        rows = r.json()
    except Exception:  # noqa: BLE001
        return 0
    if not isinstance(rows, list):
        return 0
    recv = now_ms()
    added = 0
    for t in rows:
        if not isinstance(t, dict):
            continue
        asset = str(t.get("asset") or "")
        # One transaction can carry several maker fills, so the hash alone is not
        # unique. Compose the natural key instead of inventing a surrogate.
        tid = ":".join(
            str(t.get(k) or "")
            for k in ("transactionHash", "asset", "proxyWallet", "price", "size", "timestamp")
        )
        cur = await db.execute(
            """INSERT OR IGNORE INTO rec_trades
               (slug, token_id, role, trade_id, price, size, side, venue_ts, recv_ms)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                market.slug,
                asset,
                market.roles.get(asset),
                tid,
                _float_or_none(t.get("price")),
                _float_or_none(t.get("size")),
                t.get("side"),
                _int_or_none(t.get("timestamp")),
                recv,
            ),
        )
        added += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return added


def _float_or_none(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Persistence of discovery
# --------------------------------------------------------------------------- #


async def persist_markets(
    db: aiosqlite.Connection, markets: list[dict[str, Any]]
) -> tuple[list[Target], list[TradeTarget]]:
    ts = now_ms()
    targets: list[Target] = []
    tape: list[TradeTarget] = []
    census: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for m in markets:
        slug = m.get("slug")
        if not slug:
            continue
        asset, family, rung, evidence = classify(m)
        up, down = _tokens(m)
        await db.execute(
            """INSERT INTO rec_markets (
                   slug, condition_id, asset, family, rung_guess, rung_evidence,
                   question, token_up, token_down, start_ts, end_ts,
                   first_seen_ms, last_seen_ms, liquidity, volume, raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(slug) DO UPDATE SET
                   last_seen_ms = excluded.last_seen_ms,
                   liquidity    = excluded.liquidity,
                   volume       = excluded.volume""",
            (
                slug,
                m.get("conditionId"),
                asset,
                family,
                rung,
                evidence,
                m.get("question"),
                up,
                down,
                _epoch(m.get("startDate")),
                _epoch(m.get("endDate")),
                ts,
                ts,
                _float_or_none(m.get("liquidityNum") or m.get("liquidity")),
                _float_or_none(m.get("volumeNum") or m.get("volume")),
                json.dumps(m, separators=(",", ":")),
            ),
        )
        census.setdefault((family, rung), []).append(m)
        symbol = SPOT_SYMBOL.get(asset)
        roles: dict[str, str] = {}
        if up:
            targets.append(Target(slug, up, "up", symbol))
            roles[up] = "up"
        if down:
            targets.append(Target(slug, down, "down", symbol))
            roles[down] = "down"
        tape.append(TradeTarget(slug, m.get("conditionId"), roles))

    for (family, rung), rows in census.items():
        liq = sorted(_float_or_none(r.get("liquidityNum") or r.get("liquidity")) or 0.0 for r in rows)
        vol = sorted(_float_or_none(r.get("volumeNum") or r.get("volume")) or 0.0 for r in rows)
        await db.execute(
            """INSERT INTO rec_discovery
               (scan_ms, family, rung_guess, n_markets, n_accepting,
                med_liquidity, med_volume, example_slug)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                ts,
                family,
                rung,
                len(rows),
                sum(1 for r in rows if r.get("acceptingOrders")),
                liq[len(liq) // 2] if liq else 0.0,
                vol[len(vol) // 2] if vol else 0.0,
                rows[0].get("slug"),
            ),
        )
    await db.commit()
    return targets, tape


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


async def cmd_discover(args: argparse.Namespace) -> int:
    async with httpx.AsyncClient(timeout=30.0, headers=UA) as client:
        markets = await discover(client)
    if not markets:
        print("No open up/down markets returned by the venue.")
        return 1

    census: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for m in markets:
        _, family, rung, _ = classify(m)
        census.setdefault((family, rung), []).append(m)

    print(f"\n{len(markets)} open up/down markets in {len(census)} families\n")
    print(f"{'family':<34} {'rung':>7} {'n':>4} {'med liq $':>11} {'med vol $':>11}  example")
    print("-" * 100)
    for (family, rung), rows in sorted(census.items(), key=lambda kv: -len(kv[1])):
        liq = sorted(_float_or_none(r.get("liquidityNum") or r.get("liquidity")) or 0.0 for r in rows)
        vol = sorted(_float_or_none(r.get("volumeNum") or r.get("volume")) or 0.0 for r in rows)
        print(
            f"{family[:34]:<34} {rung:>7} {len(rows):>4} "
            f"{liq[len(liq)//2]:>11,.0f} {vol[len(vol)//2]:>11,.0f}  {rows[0].get('slug','')[:28]}"
        )
    print(
        "\nThe program of record assumes a 1h and a daily rung on BTC/ETH. Compare that\n"
        "against the table above before pointing the recorder at a scope (C7, C17)."
    )
    return 0


async def run(args: argparse.Namespace) -> int:
    db_path = Path(args.db).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    wanted = {a.strip().lower() for a in args.assets.split(",")} if args.assets else None
    interval = 1.0 / max(args.hz, 0.01)
    stopping = asyncio.Event()

    def _stop(*_: Any) -> None:
        stopping.set()

    with contextlib.suppress(NotImplementedError, ValueError):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _stop)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        # WAL keeps the writer from blocking concurrent readers doing analysis.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.commit()

        async with httpx.AsyncClient(
            timeout=15.0, headers=UA, limits=httpx.Limits(max_connections=32)
        ) as client:
            markets = await discover(client)
            if wanted:
                markets = [m for m in markets if classify(m)[0] in wanted]
            targets, tape = await persist_markets(db, markets)
            if not targets:
                print("No matching markets to record — nothing to do.", file=sys.stderr)
                return 1

            symbols = {t.symbol for t in targets if t.symbol}
            spot = SpotFeed(symbols)
            spot.start()
            if websockets is None:
                await spot.rest_refresh(client)
            else:
                await asyncio.sleep(1.5)  # let the stream prime before first pairing

            print(
                f"recording {len(targets)} tokens across "
                f"{len({t.slug for t in targets})} markets -> {db_path}"
            )
            print(f"reference symbols: {', '.join(sorted(symbols)) or 'none'}")

            last_discovery = time.time()
            cycles = 0
            try:
                while not stopping.is_set():
                    t0 = time.perf_counter()
                    stats = CycleStats()

                    results = await asyncio.gather(
                        *(capture_book(client, db, t, spot) for t in targets),
                        return_exceptions=True,
                    )
                    for ok in results:
                        if ok is True:
                            stats.books_ok += 1
                        else:
                            stats.books_err += 1

                    # The tape moves far more slowly than the book; polling it
                    # every cycle would burn rate limit for nothing.
                    if cycles % max(int(args.hz * 5), 1) == 0:
                        tape = await asyncio.gather(
                            *(capture_trades(client, db, m) for m in tape),
                            return_exceptions=True,
                        )
                        stats.trades_new = sum(n for n in tape if isinstance(n, int))

                    ages = [
                        now_ms() - q.recv_ms
                        for q in (spot.get(s) for s in symbols)
                        if q is not None
                    ]
                    stats.s_stale_ms = max(ages) if ages else -1

                    cycle_ms = (time.perf_counter() - t0) * 1000.0
                    await db.execute(
                        """INSERT INTO rec_health
                           (ts_ms, cycle_ms, books_ok, books_err, trades_new, s_stale_ms, note)
                           VALUES (?,?,?,?,?,?,?)""",
                        (
                            now_ms(),
                            cycle_ms,
                            stats.books_ok,
                            stats.books_err,
                            stats.trades_new,
                            stats.s_stale_ms,
                            None,
                        ),
                    )
                    await db.commit()
                    cycles += 1

                    if args.once:
                        print(
                            f"one cycle: {stats.books_ok} books ok, {stats.books_err} err, "
                            f"{stats.trades_new} trades, S age {stats.s_stale_ms}ms, "
                            f"{cycle_ms:.0f}ms"
                        )
                        break

                    if cycles % 60 == 0:
                        print(
                            f"[{cycles}] books {stats.books_ok}/{len(targets)} "
                            f"S age {stats.s_stale_ms}ms cycle {cycle_ms:.0f}ms"
                        )

                    # Re-discover periodically: windows roll, and a family
                    # appearing or vanishing is itself the measurement.
                    if time.time() - last_discovery > args.rediscover:
                        fresh = await discover(client)
                        if wanted:
                            fresh = [m for m in fresh if classify(m)[0] in wanted]
                        if fresh:
                            targets, tape = await persist_markets(db, fresh)
                        last_discovery = time.time()

                    await asyncio.sleep(max(0.0, interval - (time.perf_counter() - t0)))
            finally:
                await spot.stop()
                await db.commit()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--discover", action="store_true", help="census the venue ladder, write nothing")
    p.add_argument("--once", action="store_true", help="run exactly one capture cycle")
    p.add_argument("--run", action="store_true", help="record continuously")
    p.add_argument("--db", default="data/venue_archive.db", help="SQLite archive path")
    p.add_argument("--assets", default="", help="comma-separated asset filter, e.g. btc,eth")
    p.add_argument("--hz", type=float, default=1.0, help="capture frequency (>=1 per the spec)")
    p.add_argument("--rediscover", type=float, default=300.0, help="seconds between ladder re-scans")
    args = p.parse_args()

    if args.discover:
        return asyncio.run(cmd_discover(args))
    if args.once or args.run:
        return asyncio.run(run(args))
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
