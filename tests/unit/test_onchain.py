"""Tests for on-chain OrderFilled decoding (#182).

Fixtures are REAL log data from Polygon tx 0xa8058f2e24.., where the target
bought 18.61 shares at 0.95 and the data-api independently reports the same.
Both constants in the module were originally wrong — a keccak of the documented
ABI produced a topic that matches nothing on chain, and the widely-cited CTF
Exchange address emits nothing for this account. These tests pin the values that
were read off an actual receipt so a future "cleanup" cannot silently revert
them to the plausible-looking wrong ones.
"""

from __future__ import annotations

from polymarket_bot.pairarb.onchain import (
    ORDER_FILLED_TOPIC,
    address_topic,
    decode_order_filled,
    subscription_params,
)

TARGET = "0xf8af03f1e68ee7162db8983f0d6dd0dc869854c6"
_W = lambda n: f"{n:064x}"  # noqa: E731

# His leg: pays 17.6795 USDC for 18.61 shares, fee 0 (a MAKER fill).
MAKER_LOG = {
    "address": "0xe111180000d2663c0091e4f400237545b87b996b",
    "blockNumber": "0x57bcf32",
    "transactionHash": "0xa8058f2e246d340a361765fec7f81751ab4a9283b1b4287352ec6ca7496c52be",
    "topics": [
        ORDER_FILLED_TOPIC,
        "0x" + "11" * 32,
        address_topic(TARGET),
        address_topic("0x13e0d447520ebe7f8eeaf7817211201b2c585204"),
    ],
    "data": "0x" + _W(0) + _W(5695433996573980141974198621313345683123765940268827396196684692170241278433)
    + _W(17679500) + _W(18610000) + _W(0) + _W(0) + _W(0),
}

# The crossing counterparty on the same trade: pays a 0.0619 fee.
TAKER_LOG = {
    **MAKER_LOG,
    "topics": [
        ORDER_FILLED_TOPIC,
        "0x" + "11" * 32,
        address_topic("0x13e0d447520ebe7f8eeaf7817211201b2c585204"),
        address_topic(TARGET),
    ],
    "data": "0x" + _W(0) + _W(81006273569529095789934590136910053923552049930479859337116940856511826350787)
    + _W(930500) + _W(18610000) + _W(61870) + _W(0) + _W(0),
}


def test_decodes_price_and_size_matching_the_public_api():
    f = decode_order_filled(MAKER_LOG)
    assert f is not None
    assert abs(f.price - 0.95) < 1e-6
    assert abs(f.shares - 18.61) < 1e-6
    assert abs(f.usdc - 17.6795) < 1e-6


def test_zero_fee_identifies_a_maker_fill():
    """The whole strategy rests on makers paying nothing; this is the proof."""
    assert decode_order_filled(MAKER_LOG).is_maker_fill is True
    assert decode_order_filled(MAKER_LOG).fee == 0.0


def test_nonzero_fee_identifies_the_crossing_side():
    t = decode_order_filled(TAKER_LOG)
    assert t is not None
    assert t.is_maker_fill is False
    assert abs(t.fee - 0.06187) < 1e-6


def test_zero_asset_id_side_is_the_usdc_leg_and_marks_direction():
    assert decode_order_filled(MAKER_LOG).maker_bought is True


def test_both_addresses_are_recovered_from_indexed_topics():
    f = decode_order_filled(MAKER_LOG)
    assert f.maker.lower() == TARGET
    assert f.taker.lower() == "0x13e0d447520ebe7f8eeaf7817211201b2c585204"


def test_ignores_logs_that_are_not_order_filled():
    assert decode_order_filled({**MAKER_LOG, "topics": ["0x" + "ab" * 32] * 4}) is None
    assert decode_order_filled({"topics": [], "data": "0x"}) is None


def test_token_for_token_trades_are_declined():
    """Neither side is collateral, so there is no price to copy."""
    log = {**MAKER_LOG, "data": "0x" + _W(7) + _W(9) + _W(1) + _W(1) + _W(0)}
    assert decode_order_filled(log) is None


def test_subscription_filters_the_right_indexed_slot():
    m = subscription_params(TARGET, as_maker=True)
    t = subscription_params(TARGET, as_maker=False)
    assert m["topics"][2] == address_topic(TARGET) and m["topics"][3] is None
    assert t["topics"][3] == address_topic(TARGET) and t["topics"][2] is None
    assert m["address"] == ["0xe111180000d2663c0091e4f400237545b87b996b"]
