"""Order-size ticket (#50, #89): the operator's share count, priced live.

EMS-style ticket: one quantity field with steppers and lot presets, then the
cost of that quantity on each side of the selected market's live book. The
persisted share count is re-read every tick, so changes apply without a
restart, in paper AND live. Pure ``render(...)`` transform — the saved size and
the quote are loaded by the caller (``execution_view.py``).
"""
from __future__ import annotations

from html import escape

from polymarket_bot import market_selection as ms
from polymarket_exec.connectors.updown_quote import UpDownQuote
from polymarket_exec.execution.gate import DEFAULT_TRADE_SHARES
from polymarket_exec.execution.live import DEFAULT_MIN_ORDER_SIZE

# A quote older than this is flagged stale (the poller refreshes every ~2s).
STALE_AFTER_SECONDS = 15.0
# Presets as multiples of the venue minimum — "lots".
_LOT_MULTIPLES = (1, 2, 5, 10)


def _px(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "—"


def _quote_status(quote: UpDownQuote | None, now: float) -> tuple[str, str, str]:
    """(css class, text, tooltip) for the header's quote-status readout."""
    if quote is None:
        return "dim", "loading", "waiting for the first book read"
    if quote.error:
        return "warn", "no quote", quote.error
    age = quote.age_seconds(now)
    if age > STALE_AFTER_SECONDS:
        return "warn", f"stale {age:.0f}s", f"last book read {age:.0f}s ago · {quote.slug}"
    return "live", f"{age:.0f}s", f"CLOB book · {quote.slug}"


def _side_cell(side: str, ask: float | None, shares: float) -> str:
    cost = f"${shares * ask:,.2f}" if ask is not None else "—"
    return (
        "<div class='tk-side'>"
        f"<span class='tag {side}'>BUY {side.upper()}</span>"
        f"<span class='mono dim'>@ {_px(ask)}</span>"
        f"<b class='mono' data-cost='{side}'>{cost}</b>"
        "</div>"
    )


def render(
    *,
    trade_shares_current: float | None,
    asset: str,
    timeframe: str,
    quote: UpDownQuote | None,
    now: float,
) -> str:
    """Render the ORDER SIZE ticket.

    ``trade_shares_current`` is the saved share count (None → ``DEFAULT_TRADE_SHARES``).
    ``quote`` is the latest book read for ``asset``/``timeframe`` (None while the
    first read is pending). ``now`` is epoch seconds, for the quote's age.
    """
    live_min = quote.min_order_size if quote and not quote.error else None
    minsh = live_min or float(DEFAULT_MIN_ORDER_SIZE)
    shares = trade_shares_current if trade_shares_current is not None else DEFAULT_TRADE_SHARES
    fresh = quote is not None and not quote.error
    up_ask = quote.up_ask if fresh else None
    down_ask = quote.down_ask if fresh else None

    status_cls, status_txt, status_tip = _quote_status(quote, now)
    market_lbl = f"{ms.ASSETS.get(asset, asset.upper())} {ms.TIMEFRAMES.get(timeframe, timeframe)}"
    presets = "".join(
        f"<button type='button' class='tk-lot' onclick='pickShares({minsh * m:g})'>{minsh * m:g}</button>"
        for m in _LOT_MULTIPLES
    )
    saved = "saved" if trade_shares_current is not None else "default"
    # A <details> fold, like the ACTIVITY LOG: the ticket is a few controls, not
    # a panel worth a screenful. Rendered open; dashboard.js stores open/closed
    # by ``data-fold`` and re-applies it after every refresh swaps this HTML out.
    return (
        "<details class='card ticket fold' data-fold='ticket' open>"
        "<summary class='card-h'><span class='fold-title'>ORDER SIZE</span>"
        f"<span class='win tk-status {status_cls}' title='{escape(status_tip, quote=True)}'>"
        f"{escape(market_lbl)} · <i class='tk-dot'></i>{escape(status_txt)}</span></summary>"
        "<div class='tk-row'>"
        "<div class='tk-qty'>"
        "<button type='button' class='tk-step' onclick='stepShares(-1)' "
        "aria-label='One share fewer'>−</button>"
        f"<input id='ctl-shares' class='tk-input mono' type='number' step='1' "
        f"min='{minsh:g}' max='1000' value='{shares:g}' data-saved='{shares:g}' "
        "oninput='onSharesInput(this)' onkeydown='if(event.key===\"Enter\")setTradeShares()' "
        "aria-label='Trade size in shares' />"
        "<button type='button' class='tk-step' onclick='stepShares(1)' "
        "aria-label='One share more'>+</button>"
        "<span class='tk-unit'>sh</span>"
        "</div>"
        f"<div class='tk-lots' aria-label='Lot presets'>{presets}</div>"
        "<button id='ctl-apply' class='gr-btn btn-ok tk-apply' "
        "onclick='setTradeShares()' disabled>Apply</button>"
        "</div>"
        f"<div class='tk-cost' id='ctl-cost' data-min='{minsh:g}'"
        f" data-up='{up_ask if up_ask is not None else ''}'"
        f" data-down='{down_ask if down_ask is not None else ''}'>"
        + _side_cell("up", up_ask, shares)
        + _side_cell("down", down_ask, shares)
        + "<div class='tk-side tk-max' title='worst case: every share costs $1.00'>"
        "<span class='tk-l'>MAX</span>"
        f"<b class='mono dim' data-cost='max'>${shares:,.2f}</b></div>"
        "</div>"
        "<div class='tk-foot'>"
        f"<span id='ctl-saved'>{saved} {shares:g} sh</span> · min {minsh:g} sh"
        "</div>"
        "</details>"
    )
