"""On-chain fill detection via Polygon ``OrderFilled`` logs (#182).

Why this exists: the ``data-api`` activity feed a copier would naively poll is
**~20 seconds stale** (measured over 357 fills: p25 12s, median 20s, p90 47s).
Slippage tracks that lag directly — median 9.56c in the 11-30s bucket versus
2.82c at 0-2s — so the feed, not the trading logic, is the dominant cost.

The Polymarket CLOB WebSocket is sub-second but publishes **no identity**
(``last_trade_price`` carries asset, price, size and tx hash, and no maker or
taker). It cannot drive a copier on its own.

The CTF Exchange's ``OrderFilled`` event carries both addresses and is indexed,
so a log subscription filtered on one wallet gives identity at block time —
roughly 2 seconds on Polygon. That is the floor: the event only becomes
attributable once settled, so a copier races the confirmation of something the
target already did, never the target.

Pure decoding here; transport lives in ``tools/copytrade_onchain.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# OrderFilled topic0, taken from a REAL receipt rather than derived from a
# guessed signature. Computing keccak of the documented ABI gave
# 0xd0a08e8c... which matches nothing on chain — the deployed event differs.
# Verified against tx 0xa8058f2e24.. where the decode reproduces the data-api's
# reported 18.61 shares @ 0.95 exactly.
ORDER_FILLED_TOPIC = (
    "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
)

# The live Polymarket exchange, also read off a real receipt. The widely-cited
# 0x4bfb41d5.. CTF Exchange address emits nothing for this account.
EXCHANGE = "0xe111180000d2663c0091e4f400237545b87b996b"
EXCHANGES = (EXCHANGE,)

# USDC is asset id 0 in the exchange's accounting; any other id is an outcome
# token. Which side holds the zero tells us whether the maker bought or sold.
COLLATERAL_ASSET_ID = 0

USDC_DECIMALS = 6
SHARE_DECIMALS = 6


def address_topic(address: str) -> str:
    """Left-pad an address into a 32-byte log topic for indexed filtering."""
    return "0x" + "0" * 24 + address.lower().removeprefix("0x")


@dataclass(frozen=True)
class OnChainFill:
    """One decoded ``OrderFilled`` event.

    Attributes:
        tx_hash: Settlement transaction.
        block_number: Block it landed in.
        maker: Address whose resting order was filled.
        taker: Address that crossed.
        token_id: Outcome token that changed hands.
        shares: Outcome-token quantity.
        usdc: Collateral that moved.
        maker_bought: Whether the maker received outcome tokens (paid USDC).
        fee: Fee charged to this side. **Zero identifies a maker fill.**
            Confirmed on chain: in one matched trade the target paid 0 while the
            crossing counterparty paid 0.0619, so this field reveals maker/taker
            in real time — something the data-api only exposes indirectly.
    """

    tx_hash: str
    block_number: int
    maker: str
    taker: str
    token_id: str
    shares: float
    usdc: float
    maker_bought: bool
    fee: float = 0.0

    @property
    def is_maker_fill(self) -> bool:
        """Whether this side rested (fee-free) rather than crossed."""
        return self.fee <= 0.0

    @property
    def price(self) -> float:
        """USDC per share — the fill price. 0.0 when no shares moved."""
        return (self.usdc / self.shares) if self.shares else 0.0


def _u256(data: str, index: int) -> int:
    """Read the ``index``-th 32-byte word from ABI-encoded log data."""
    body = data.removeprefix("0x")
    word = body[index * 64 : (index + 1) * 64]
    return int(word, 16) if word else 0


def decode_order_filled(log: dict[str, Any]) -> OnChainFill | None:
    """Decode one ``OrderFilled`` log, or ``None`` if it is not one.

    Layout: ``orderHash``, ``maker`` and ``taker`` are indexed (topics 1-3);
    ``makerAssetId``, ``takerAssetId``, ``makerAmountFilled``,
    ``takerAmountFilled`` and ``fee`` are in data words 0-4.

    Whichever side carries asset id 0 is the USDC leg, and that tells us the
    direction: a maker paying USDC bought the outcome token.
    """
    topics = log.get("topics") or []
    if len(topics) < 4 or topics[0].lower() != ORDER_FILLED_TOPIC:
        return None
    data = log.get("data") or "0x"

    maker_asset = _u256(data, 0)
    taker_asset = _u256(data, 1)
    maker_amount = _u256(data, 2)
    taker_amount = _u256(data, 3)
    fee = _u256(data, 4) / 10**USDC_DECIMALS

    if maker_asset == COLLATERAL_ASSET_ID:
        # Maker paid USDC, received outcome tokens.
        maker_bought = True
        token_id = str(taker_asset)
        usdc = maker_amount / 10**USDC_DECIMALS
        shares = taker_amount / 10**SHARE_DECIMALS
    elif taker_asset == COLLATERAL_ASSET_ID:
        # Maker delivered outcome tokens, received USDC.
        maker_bought = False
        token_id = str(maker_asset)
        usdc = taker_amount / 10**USDC_DECIMALS
        shares = maker_amount / 10**SHARE_DECIMALS
    else:
        return None  # token-for-token: not a copyable collateral trade

    return OnChainFill(
        tx_hash=str(log.get("transactionHash") or ""),
        block_number=int(str(log.get("blockNumber") or "0x0"), 16),
        maker="0x" + topics[2][-40:],
        taker="0x" + topics[3][-40:],
        token_id=token_id,
        shares=shares,
        usdc=usdc,
        maker_bought=maker_bought,
        fee=fee,
    )


def subscription_params(address: str, as_maker: bool = True) -> dict[str, Any]:
    """Build ``eth_subscribe`` params for one wallet's fills.

    ``maker`` is topic 2 and ``taker`` is topic 3, so one subscription covers a
    single role. The target is overwhelmingly a maker (40% of its fills are
    fee-free), but a copier that watches only that role misses the ~35% it
    takes, so callers should run both.
    """
    slot = 2 if as_maker else 3
    topics: list[Any] = [ORDER_FILLED_TOPIC, None, None, None]
    topics[slot] = address_topic(address)
    return {"address": list(EXCHANGES), "topics": topics}
