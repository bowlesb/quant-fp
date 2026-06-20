"""The overnight_beta strategy's declared feature contract — EMPTY: it consumes no FEATURE-BUS features.

overnight_beta computes its beta quintiles from a trailing daily-return panel it loads directly from the
broker (``self._panel.load()``), not from the per-minute feature bus. So its bus-feature dependency is the
empty set — it is trivially compatible with any feature-set fingerprint. It still PUBLISHES this (empty)
contract to ``strategy:features:overnight_beta`` at startup so the pre-deploy gate sees it as present and
accounted-for (not flagged absent and failing the deploy closed). See docs/BUS_FEATURE_ACCESS.md §2.6.
"""

from __future__ import annotations

from quantlib.bus.compat import FeatureReq

STRATEGY_NAME = "overnight_beta"

STRATEGY_FEATURES: tuple[FeatureReq, ...] = ()
