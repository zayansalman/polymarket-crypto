# Wallet research

Offline screen that finds Polymarket wallets worth copying, and the evidence
behind the ones it picks. Nothing here trades; it only reads public data.

    scan.py      pull every fill on every resolved Up-or-Down market in a window
    rank.py      rank wallets by net-of-fee edge per share, FDR-corrected
    analyze.py   older ranking by total PnL - kept for comparison only
    validate.py  cross-check one wallet against Polymarket's own numbers

## Why rank.py and not analyze.py

`analyze.py` ranks by profit. That reliably surfaces **market makers**, whose
profit is the spread a copier pays, and wallets that had a few good days. Every
one of the six top-PnL wallets reviewed in Sept 2026 failed verification.

`rank.py` ranks by **net edge per share after the taker fee**, and adds three
gates profit-ranking cannot express:

* wallets buying **both** outcomes in >25% of their markets are quoting, not
  forecasting - excluded outright
* significance is corrected across every wallet tested (Benjamini-Hochberg FDR),
  because screening thousands of wallets and keeping the best t-stat just finds
  the top of a noise distribution
* the edge must survive deleting each wallet's three best markets

Polymarket charges takers `shares * 0.07 * p * (1-p)` on crypto and nothing on
makers. That is 1.75c/share at 50c but 0.028c at 99.6c, which is why the only
wallets that survive trade at the extremes.

## Data

Written to `data/wallet_research/` (gitignored - the trade db runs to gigabytes).
