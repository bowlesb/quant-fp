"""The reversion strategy container — the SECOND strategy, proving the multi-strategy design.

Unlike smoke (no alpha), reversion trades a real signal: intraday VWAP mean-reversion via
``VwapReversionModel`` behind the shared ``Model.predict`` interface. Same apparatus — bus subscription,
its OWN ``strat_reversion`` schema, the same safety caps, paper-only.
"""
