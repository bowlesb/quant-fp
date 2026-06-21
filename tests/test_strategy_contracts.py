"""Build-time guards on the strategies' declared feature contracts (docs/BUS_FEATURE_ACCESS.md §2.6, B3).

These assert the contracts are a faithful, non-drifting picture of what each strategy actually consumes:
  - the contract NAMES equal the model's construction constant (can't drift from what the model reads);
  - the pinned VERSIONS match the CURRENT schema (a build can't ship a stale pin);
  - every contract feature is present in the live schema (the strategy is deployable on this feature set).
"""

from __future__ import annotations

from quantlib.bus.compat import FeatureReq
from quantlib.bus.schema import default_schema
from quantlib.strategy_core.models.crypto_momentum import CryptoMomentumModel
from quantlib.strategy_core.models.vwap_reversion import VwapReversionModel
from strategies.crypto_momentum.contract import contract_for as crypto_contract_for
from strategies.overnight_beta.contract import STRATEGY_FEATURES as OBETA_FEATURES
from strategies.reversion.contract import contract_for
from strategies.smoke.contract import MODEL_FOLD_FEATURES
from strategies.smoke.contract import STRATEGY_FEATURES as SMOKE_FEATURES

SCHEMA = default_schema()


def _assert_contract_matches_schema(contract: tuple[FeatureReq, ...]) -> None:
    for req in contract:
        field = SCHEMA.field(req.name)
        assert field is not None, f"contract feature '{req.name}' absent from the live schema"
        assert (
            field.version == req.version
        ), f"stale version pin for '{req.name}': contract={req.version} schema={field.version}"


def test_smoke_contract_names_equal_model_fold() -> None:
    assert [req.name for req in SMOKE_FEATURES] == list(MODEL_FOLD_FEATURES)


def test_smoke_contract_matches_current_schema() -> None:
    _assert_contract_matches_schema(SMOKE_FEATURES)


def test_reversion_contract_is_the_models_feature() -> None:
    model = VwapReversionModel(window_m=30)
    contract = contract_for(model)
    assert [req.name for req in contract] == [model.feature_name]
    _assert_contract_matches_schema(contract)


def test_crypto_momentum_contract_is_the_models_feature() -> None:
    model = CryptoMomentumModel(window_m=5)
    contract = crypto_contract_for(model)
    assert [req.name for req in contract] == [model.feature_name]
    _assert_contract_matches_schema(contract)


def test_overnight_beta_contract_is_empty() -> None:
    # overnight_beta consumes no per-minute bus features (daily-return panel from the store).
    assert OBETA_FEATURES == ()
