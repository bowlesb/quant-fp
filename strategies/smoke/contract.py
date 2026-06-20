"""The smoke strategy's declared feature contract — its (name, version) dependency on the bus, plus the
feature-name constants the model is constructed from (the single source of truth).

``MODEL_FOLD_FEATURES`` is the constant the model is built with AND the contract's names, so the contract
cannot drift from what the model reads (a test asserts the identity). Each name is pinned to the group
version the strategy was built against; a CI check asserts the pins match the current schema at build time,
so a build can never ship a stale pin. At runtime the strategy publishes this contract to
``strategy:features:smoke`` so the pre-deploy compat gate checks what is ACTUALLY running. See
docs/BUS_FEATURE_ACCESS.md §2.6.
"""

from __future__ import annotations

from quantlib.bus.compat import FeatureReq

STRATEGY_NAME = "smoke"

# the model's construction constant — the single source of truth for both the model fold and the contract.
MODEL_FOLD_FEATURES = ["ret_1m", "volume_zscore_5m"]
SAMPLE_FEATURES = MODEL_FOLD_FEATURES  # the eyeball-logged sample reads the same features

# version pins frozen at build time (price_returns 1.0.0, volume 1.1.0) — CI asserts these still match.
_VERSIONS = {"ret_1m": "1.0.0", "volume_zscore_5m": "1.1.0"}

STRATEGY_FEATURES: tuple[FeatureReq, ...] = tuple(
    FeatureReq(name, _VERSIONS[name]) for name in MODEL_FOLD_FEATURES
)
