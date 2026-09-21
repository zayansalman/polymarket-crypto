# Strategy docs

One document per strategy family in `polymarket_bot/inventory.py`, at `docs/strategies/<key>.md`.
The dashboard serves them at `/strategy-docs`; each row on the MY STRATEGIES card links to its doc.

Every doc has the same sections: **What it does**, **How it was formed** (who proposed it, when,
and what led to it), **How it works**, **Sources**, **Changelog**. Optional: Parameters, Evidence
so far, Known weaknesses.

## Keeping a doc in step with its code

The block between the `GENERATED:strategy` markers holds the strategy's name, status, switch,
code path and a fingerprint of that code. The newest changelog entry must cite the same
fingerprint. Change the code, the name, the status or the switch and
`tests/unit/test_strategy_docs.py` fails until the doc is updated:

```bash
python tools/strategy_docs.py stamp <key> "what changed and why"
```

That refreshes the block and adds a dated changelog entry. Update the prose in the same commit.
A new family needs `python tools/strategy_docs.py new <key>` and every section filled in.
`python tools/strategy_docs.py check` lists anything out of step.

## Sourcing

Every claim traces to a source: a commit, issue, script, dataset, paper or URL. An idea from
the operator is credited "Zayan (operator), date"; one proposed by an AI session "Claude, date".
