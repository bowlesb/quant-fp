# Bus Feature Access — name-addressed, fingerprint-decoupled consume

**Status:** SPEC / GATED. No live wiring in this PR. Goes to Lead review + Ben review BEFORE any build;
this touches LIVE strategy consumption, so it is gated like `STRATEGY_EXECUTION_ABSTRACTION.md`.

**Author:** strategy-core owner. **Off:** `origin/main` @ `b4fb88f`. **Fingerprint impact:** NONE — this
is consumer-side only. The wire format, the producer, and the schema/registry are unchanged; the feature
fingerprint `0x...694` is untouched. (The producer publishes ONE extra Redis key per fingerprint — a
side artifact, not a wire change.)

---

## 1. The problem (why coordinated deploys are forced today)

A bus frame carries a dense `float64` payload in canonical schema-offset order plus the schema
fingerprint in its header (`quantlib/bus/codec.py`). On decode, the consumer hard-rejects any frame whose
fingerprint differs from the consumer's *own* compiled `BusSchema`:

```python
# quantlib/bus/codec.py  (decode)
if fingerprint != schema.fingerprint:
    raise ValueError("schema fingerprint mismatch: ...")
if n_features != schema.n_features:
    raise ValueError("n_features mismatch: ...")
```

Each strategy builds its schema from its *own* baked feature registry: `BusConsumer(config.symbols,
url=bus_url)` defaults `schema=default_schema()` = `BusSchema.from_registry()` over the container's
compiled `quantlib.features.groups`. So:

- The fingerprint is a blake2b over the ordered `group:feature:version` lines (`schema.py`).
- **Adding, removing, renaming, or re-versioning ANY feature changes the fingerprint AND shifts offsets.**
- The instant `fc` (the producer) ships a new feature set, every strategy's `decode` raises on the first
  new frame — the container stops trading until it is rebuilt against the new registry.

Result: a one-feature addition requires a coordinated rebuild of `fc` + all 3 strategy containers, atomic.
That coordinated-deploy tax is what kills cheap feature iteration ([[project-trust-and-edge-state]]'s
"coordinated-deploy timing rule").

**The accessor is already name-addressed** — `FeatureVector.value("ret_1m")`, `vec["ret_1m"]`,
`vec.momentum.momentum_fast_1`, and every strategy reads by name (smoke: `SAMPLE_FEATURES = ["ret_1m",
"volume_zscore_5m"]`; reversion: `vwap_deviation_30m`; overnight_beta: a daily-return panel). The ONLY
thing coupling a consumer to a fingerprint is the **hard reject in `decode`** plus the assumption that the
consumer's compiled schema == the frame's schema. **That assumption is the bug, not the name accessor.**

### What a robust consumer actually needs

A consumer needs exactly two things from a frame:
1. The values of the **specific features it declares it needs** (smoke needs 2; reversion needs 1).
2. A loud failure if a feature it needs is **genuinely absent** from the frame (removed/renamed).

It does NOT need the frame's fingerprint to equal its own. A frame whose feature SET is a *superset* of
what the consumer needs (the common case: features were ADDED) is perfectly readable — as long as the
consumer resolves names against **the frame's** schema, not its own stale one.

---

## 2. The design (Ben's, speed-preserving)

**End goal (Ben):** CONTINUOUS DEPLOYMENT of new features — merge a feature PR, deploy `fc`, with NO
coordinated rebuilds and NO risk to active strategies. Two halves: §2.1–2.5 are the **accessor** that makes
a running strategy robust to a changed fingerprint; §2.6 is the **safety gate** that proves an `fc`-only
deploy is compatible BEFORE it ships. Six moving parts; wire format and producer compute path are unchanged.

### 2.1 Wire format UNCHANGED

The dense `float64` payload stays (`FVB1` magic, fingerprint, minute_us, n_features, symbol, payload).
No per-feature dict on the wire (that would kill throughput and balloon Redis memory). The frame already
self-describes its fingerprint — we just stop treating a *different* fingerprint as fatal.

### 2.2 Schema resolvable BY fingerprint (publish + registry)

On emit, the producer publishes its `BusSchema` (the `name -> offset` map + `n_features` + per-field group/
version) to a resolvable place keyed by fingerprint:

```
Redis key:  bus:schema:<fingerprint_hex>   ->   JSON  {"fingerprint": "0x..694",
                                                       "n_features": 694,
                                                       "fields": [{"name": "...", "offset": 0,
                                                                   "group": "...", "version": "..."}, ...]}
```

- Written **idempotently** by the producer once per fingerprint (it is constant for the life of a feature
  set), e.g. on `BusPublisher` init / first publish. `SET` (no TTL, or a long TTL refreshed on emit) —
  one tiny key per fingerprint, a few thousand short lines, written once.
- This is a **side artifact**, not a wire change: it does not touch the frame bytes, the registry, or the
  fingerprint computation. It is the publish of an *existing* object (`BusSchema`) the producer already
  holds.
- **Bootstrap / resilience:** if `bus:schema:<fp>` is missing for a fingerprint a consumer sees (producer
  hasn't written it yet, or a non-bus producer), the consumer falls back to its OWN compiled schema **iff
  the fingerprint matches** (today's behavior) — and otherwise raises `UnknownSchema(fp)` loudly. A
  registry abstraction (`SchemaRegistry`) wraps the lookup so a future backend (a DB table, a file) can
  replace Redis without touching consumers.

### 2.3 Name-addressed accessor against the FRAME's schema — `FeatureView`

A `FeatureView` resolves features by NAME against **the frame's own** (cached) schema — never a hardcoded
one:

```python
class FeatureView:
    """Name-addressed read over ONE decoded frame, resolved against the FRAME's schema (by fingerprint)."""
    symbol: str
    minute: dt.datetime
    fingerprint: int

    def value(self, name: str) -> float: ...          # O(1); raises MissingFeature if name not in frame
    def get(self, name: str, default: float = nan) -> float: ...   # NaN-safe variant for optional reads
    def has(self, name: str) -> bool: ...
    def to_model_vector(self, expected_names: Sequence[str]) -> np.ndarray: ...   # §2.4
```

`FeatureView` satisfies the existing `FeatureRow` protocol (`.symbol`, `.minute`, `.value(name)`), so the
re-homed cores (`VwapReversionModel.predict`, `MockMLModel.predict`) consume it **unchanged** — the
migration is at the wiring layer, not the decision layer. The single-implementation invariant
(`STRATEGY_BATTERY_PORTABILITY.md`) is preserved: `decide()` still reads features by name; it just reads
them off a view whose offsets came from the frame.

### 2.4 ⭐ Model-input at the boundary — `to_model_vector(expected_names)`

The key point. The CONSUMER declares the ordered feature list its model expects. `to_model_vector` builds
the dense ordered model input from that list, resolved against the frame's schema, ONLY at the model
boundary:

```python
def to_model_vector(self, expected_names: Sequence[str]) -> np.ndarray:
    out = np.empty(len(expected_names), dtype="<f8")
    for i, name in enumerate(expected_names):
        offset = self._schema_map.get(name)          # frame's name->offset map (cached)
        if offset is None:
            raise MissingFeature(name, self.fingerprint)   # consumer needs it, frame lacks it
        out[i] = self._array[offset]
    return out
```

- **Order is the consumer's, not the frame's** — so a trained model fed `[f_a, f_b, f_c]` always gets its
  columns in the order it was trained on, regardless of where they sit in the frame. This is the property
  that makes a value-identical *restructure* (same names, reordered/renumbered offsets — the `#203`-style
  case) a **non-event** for the consumer.
- **A needed feature absent from the frame → `MissingFeature`** (loud, names the feature + fingerprint).
  Never silent NaN-fill for a feature the model contract requires. (A consumer that WANTS NaN-tolerance
  for a genuinely-optional input uses `get(name, default=nan)` explicitly — opt-in, not the default.)
- **Extra features in the frame → ignored** (the additive case: features were added; the consumer simply
  doesn't ask for them).

This is where the decoupling pays off: feature ADDITIONS are non-breaking by construction, because the
consumer only ever asks for its own `expected_names`.

### 2.5 Relax the codec — resolve, don't reject

`decode` (or a new `decode_view`) stops hard-rejecting on fingerprint mismatch. Instead:

1. Parse the header (magic check stays — a non-`FVB1` frame is still fatal; that is genuine corruption).
2. Resolve the frame's schema map via the `SchemaRegistry` (cached per fingerprint — fetch once, then
   O(1)). If unresolvable → `UnknownSchema`.
3. Validate **structural** integrity that is still load-bearing: `n_features` in the header must equal the
   resolved schema's `n_features` (a length/offset-space mismatch is a genuine misalignment and MUST still
   raise — we never silently index past the payload).
4. Return a `FeatureView` bound to that schema map + the payload view.

Per-name alignment is then validated **at access time**: a requested name is resolved against the frame's
schema (`MissingFeature` if absent). So the loud-failure property is **preserved exactly where it matters**
— a name the consumer needs that the frame lacks, or a length mismatch — and **dropped only where it was
wrong** — a *different* fingerprint whose feature set still covers the consumer's needs.

**Invariant restated:** the consumer NEVER silently misaligns. Today's protection (exact fingerprint) is
replaced by a *stronger, name-level* protection (resolve each needed name against the frame's actual
schema). A bug that today is caught by "fingerprint differs" is caught tomorrow by "this name isn't in the
frame" — with a far better error message, and without the false-positive rejections of benign additions.

### 2.6 ⭐ Declared-feature contract + pre-deploy compatibility gate (the safety net)

§2.1–2.5 make a *running* strategy robust to additions. This section makes an `fc`-only **deploy provably
safe BEFORE it ships** — the half that turns "coordinated rebuild" into "continuous deployment". The end
goal (Ben): merge feature PRs and deploy `fc` continuously, with NO coordinated rebuilds and NO risk to
active strategies, gated by one automated check.

**The declared-feature contract.** Each strategy DECLARES the exact feature names it consumes — its
contract with the bus. It already does this implicitly; we make it explicit and machine-readable:

```python
# strategies/<name>/contract.py  (or a STRATEGY_FEATURES constant the container exports)
STRATEGY_FEATURES: tuple[str, ...] = ("ret_1m", "volume_zscore_5m")   # smoke, e.g.
```

The contract is the SAME list the strategy passes to `to_model_vector(expected_names)` (single source of
truth — the declared set IS what the model reads, so they cannot drift). For a strategy that reads a single
feature via `view.value`, the contract is that one name.

**The reusable check.** A pure function — no Redis, no live state:

```python
class IncompatibleSchema(Exception):
    """A live strategy declares features the candidate schema lacks (with the exact missing names)."""

def assert_compatible(candidate_schema: BusSchema, declared: Sequence[str], *, strategy: str) -> None:
    missing = [name for name in declared if not candidate_schema.has(name)]
    if missing:
        raise IncompatibleSchema(
            f"strategy '{strategy}' needs features absent from candidate "
            f"fingerprint {candidate_schema.fingerprint:#018x}: {missing}"
        )
```

i.e. the strategy's declared set must be a **subset** of the candidate schema's names. Subset — not equal —
is exactly why additions are safe and a rename/removal of a *consumed* feature is the only thing that fails.

**The pre-deploy gate.** Before relaunching `fc` on a new feature set, run — in CI or the deploy script —
`assert_compatible(candidate_schema, contract, strategy=name)` for EVERY live strategy's declared contract
against the NEW candidate fingerprint's schema:

- **GREEN** (all subsets resolve) → `fc` deploys freely. Additions are non-breaking by construction;
  strategies are untouched and keep trading across the fingerprint change.
- **RED** → block the deploy and name the EXACT missing/renamed feature each affected strategy needs. A
  genuine incompatibility (a feature a live strategy depends on was removed/renamed) is surfaced
  *precisely, before deploy* — never a silent break, never a runtime decode crash mid-session.

**What this replaces.** The manual "rebuild fc + 3 strategies, atomically" coordination collapses to:

```
rebuild fc  →  run compat gate (assert_compatible per strategy)  →  GREEN: relaunch fc  /  RED: block + name the feature
```

Strategies do not move unless THEY choose to adopt a new feature (at which point they update their own
`STRATEGY_FEATURES` + `to_model_vector` list and rebuild on their own schedule). The entire error surface of
a feature deploy is reduced to **one precise, checkable condition**: is every live strategy's declared set ⊆
the new schema?

**Where the gate gets the contracts.** Two options (decide at build): (a) a small registry the deploy
script imports each strategy's `STRATEGY_FEATURES` from; or (b) each running strategy publishes its declared
contract to Redis (`strategy:features:<name>`) on startup, and the gate reads the live set — which also
covers "what is ACTUALLY running right now" without a static list to keep in sync. (b) is more robust (the
gate checks reality, not a possibly-stale manifest); (a) is simpler and CI-runnable with no live cluster.
Recommend (b) for the deploy gate with (a)'s static import as the CI smoke-check.

---

## 3. Speed analysis (no throughput regression at bus scale)

**Claim:** the steady-state hot path is unchanged; the only added cost is a per-fingerprint schema resolve,
which is amortized to ~zero by caching.

Per-frame decode cost, today vs proposed (steady state, fingerprint already seen):

| Step                          | Today                          | Proposed                                   |
|-------------------------------|--------------------------------|--------------------------------------------|
| header `struct.unpack_from`   | yes                            | yes (identical)                            |
| magic check                   | yes                            | yes (identical)                            |
| fingerprint check             | `== self.fp` (one int compare) | `schema_map = cache[fp]` (one dict get)    |
| `n_features` check            | yes                            | yes (identical)                            |
| payload `np.frombuffer`       | zero-copy view                 | zero-copy view (identical)                 |
| **per-feature read**          | `array[schema.offset(name)]`   | `array[schema_map[name]]` (identical O(1)) |

The per-frame change is **one int-compare → one dict-get on a cached map** — both O(1), nanoseconds, not in
any hot loop that scales with `n_features` or `n_symbols`. The payload stays a zero-copy `np.frombuffer`
view; no decode-time allocation of the float data, exactly as today.

**The one new cost — schema resolve — is paid once per fingerprint, not per frame:**
- First frame of a new fingerprint: one Redis `GET bus:schema:<fp>` (~sub-ms LAN) + a JSON parse into a
  `{name: offset}` dict (a few thousand entries, one-time). Cached thereafter.
- A consumer sees ≤ 2 fingerprints across a deploy window (old + new). So across an entire trading day the
  resolve happens at most twice. Over 10k+ vectors/minute the amortized per-frame overhead is
  ~`2 / (10k * 390)` ≈ negligible.

**`to_model_vector` cost:** `len(expected_names)` dict-gets + an array fill of that length — for a strategy
declaring 2–30 features this is < `n_features` work (smaller than `to_dict()`, which the current
`FeatureVector` already offers). It runs once per decision, not per frame, and only for the names the model
needs.

**Benchmark to PROVE it (build phase, not this spec):** a micro-benchmark decoding N=10k frames/iteration,
measuring p50/p99 decode-to-readable and per-name read latency, asserting the proposed path is within noise
(say ≤ 5%) of the current dense-decode, with the schema cache warm. If a naive form regresses (e.g.
re-resolving per frame, or building the full name dict eagerly when the model needs 2 names), that is the
design tension to surface to Ben ("not at the expense of speed") — the cache + lazy `to_model_vector` are
specifically the mitigations. The benchmark lands as `tests/bench_bus_decode.py` (sandbox `--rm` fp-dev,
`OMP_NUM_THREADS` bounded), gating the build PR.

---

## 4. Proof tests (the decoupling proof)

All pure / in-process (a fake `SchemaRegistry` backed by a dict — no live Redis needed):

1. **Additions are non-breaking.** Consumer declares `expected_names = {A, B}`. Build frame `X` (fp_X,
   features ⊇ {A, B}) and frame `Y` (fp_Y ≠ fp_X, a SUPERSET that ADDS features but still ⊇ {A, B}, with A
   and B at DIFFERENT offsets). `view.to_model_vector([A, B])` returns the SAME, correct values from BOTH
   frames. ← the core decoupling proof.
2. **Value-identical restructure (`#203`-style).** Same feature NAMES, reordered offsets / different
   fingerprint. `to_model_vector` returns identical values (order is the consumer's). No consumer change,
   no rebuild.
3. **Missing-feature errors clearly.** A frame whose schema LACKS a needed name → `to_model_vector([A,
   B_missing])` raises `MissingFeature(B_missing, fp)` (names the feature + fingerprint). Never a silent
   NaN.
4. **Length/structural misalignment still loud.** A frame whose header `n_features` ≠ the resolved schema's
   `n_features` → raises (we never index past the payload).
5. **Unknown schema is loud, with safe same-fingerprint fallback.** Unresolvable fingerprint (not in
   registry, not the consumer's own) → `UnknownSchema(fp)`. A missing key whose fp == the consumer's own
   compiled schema → falls back to the compiled schema (today's behavior, preserved).
6. **`FeatureRow` parity.** `FeatureView` satisfies the `FeatureRow` protocol; the re-homed
   `VwapReversionModel` / `MockMLModel` produce identical `Prediction`s reading a `FeatureView` vs the
   current `FeatureVector` on the same values (parity-by-construction at the decision layer).
7. **Compat gate GREEN on additions.** `assert_compatible(candidate_schema, declared={A, B})` where the
   candidate ADDS features but still contains {A, B} → passes (no raise). The deploy is cleared.
8. **Compat gate RED names the exact feature.** A candidate schema that REMOVED/renamed `B` → declared {A,
   B} raises `IncompatibleSchema` whose message names `B` and the candidate fingerprint. A multi-strategy
   gate run reports every affected strategy + its missing names, and blocks.
9. **Contract == model-input list.** A strategy's declared `STRATEGY_FEATURES` equals the `expected_names`
   it passes to `to_model_vector` (single source of truth) — a test asserts they're the same object/list,
   so the gate can never pass while the model would `MissingFeature` at runtime.

---

## 5. Per-strategy migration sketch

Each strategy declares its **expected feature names** (it already does, implicitly) and assembles its
model input through `to_model_vector` (or stays on `view.value(name)` for the single-feature cores). The
`decide()` logic does not change; only the wiring at the consume boundary does.

### 5.1 smoke (`strategies/smoke/`)
- Today: `SAMPLE_FEATURES = ["ret_1m", "volume_zscore_5m"]`, `MODEL_FOLD_FEATURES = ["ret_1m",
  "volume_zscore_5m"]`; reads via `vector.value(name)`; `MockMLModel(MODEL_FOLD_FEATURES)`.
- Migration: `STRATEGY_FEATURES = ("ret_1m", "volume_zscore_5m")` declared once (the §2.6 contract). The
  poll loop yields `FeatureView`s (consumer resolves the frame's schema). `MockMLModel` consumes the view
  via `FeatureRow` unchanged. `view.to_model_vector(STRATEGY_FEATURES)` if/when a real model replaces the
  mock — the same list, so contract == model input.
- Effect: `fc` can add features freely — smoke keeps reading its 2 by name across the fingerprint change.

### 5.2 reversion (`strategies/reversion/`)
- Today: `VwapReversionModel(window_m=30)` reads `vwap_deviation_30m` via `vector.value(model.feature_name)`.
- Migration: `STRATEGY_FEATURES = (model.feature_name,)`. Same `FeatureRow`-based `predict` on a
  `FeatureView`. The `np.isfinite` warmup guard stays.
- Effect: a feature addition elsewhere never disturbs the single `vwap_deviation_30m` read.

### 5.3 overnight_beta (`strategies/overnight_beta/`)
- Today: consumes a daily-return panel for `compute_beta`; cross-sectional leg selection.
- Migration: declare the daily-return feature name(s) it pulls per symbol; read each via `view.value` /
  `to_model_vector`. Note the cross-section caveat already documented on `BusCrossSection` (audit
  2026-06-19): a cross-sectional LIVE archetype must ensure its symbol set is fully populated for the
  ranked cross-section; the warmup gate (`STRATEGY_CONTAINERS.md`) enforces this independently of this
  change.

### 5.4 `BusConsumer`
- Add a `SchemaRegistry` (Redis-backed by default; a `dict`-backed fake for tests) and have `poll()` return
  `FeatureView`s resolved per-frame, caching maps per fingerprint. Keep a flag/seam so the OLD exact-
  fingerprint `decode` path remains available during migration (belt-and-suspenders, removed after cutover).

---

## 6. Deploy plan — from coordinated rebuild to CONTINUOUS DEPLOYMENT

1. Build behind this spec → Lead review → Ben review → re-audit (gated like the execution design).
2. Producer change (publish `bus:schema:<fp>`) is **fingerprint-neutral** and additive — it can ride a
   normal `fc` rebuild; it does not change frame bytes or the registry.
3. Cut the 3 strategies over to `FeatureView` + the declared `STRATEGY_FEATURES` contract (§2.6) in ONE
   coordinated strategy rebuild — `fc` untouched, fingerprint unchanged. This is the **last** coordinated
   rebuild. Each strategy now publishes its contract (`strategy:features:<name>`) on startup.
4. **After cutover — continuous feature deployment:**
   ```
   feature PR merges  →  rebuild fc candidate  →  COMPAT GATE: assert_compatible(candidate_schema, contract)
                         for every live strategy  →  GREEN: relaunch fc  /  RED: block + name the feature
   ```
   On GREEN, `fc` ships alone: it publishes the new `bus:schema:<fp>`; consumers resolve + cache it on the
   first new frame and keep reading their declared names. **No strategy rebuild, no coordinated window, no
   fingerprint coordination.** A strategy moves only when IT chooses to adopt a new feature (updates its own
   contract + `to_model_vector` list, rebuilds on its own schedule).

**The vision this lands (Ben):** feature PRs merge and `fc` deploys CONTINUOUSLY, gated by the automated
compat check. Active strategies are never at risk and never forced to rebuild. The entire risk surface of a
feature deploy is reduced to one precise, automatable condition — *is every live strategy's declared feature
set ⊆ the new schema?* — checked BEFORE the deploy, naming the exact incompatible feature if not.

---

## 7. Out of scope / explicitly NOT in this spec

- No change to the feature registry, compute path, store, or fingerprint algorithm.
- No change to the wire frame bytes (the `bus:schema` key is a side artifact).
- No execution / state changes (that is `STRATEGY_EXECUTION_ABSTRACTION.md`, separately gated on Ben).
- No live wiring in the spec PR — build is a follow-up, gated on this review.

---

## Appendix A — surfaces touched (build phase)

| File                                   | Change                                                              |
|----------------------------------------|---------------------------------------------------------------------|
| `quantlib/bus/schema.py`               | `BusSchema.to_json()/from_json()`; `offsets()` map accessor          |
| `quantlib/bus/registry.py` (new)       | `SchemaRegistry` (Redis + fake), per-fingerprint cache               |
| `quantlib/bus/view.py` (new)           | `FeatureView`, `to_model_vector`, `MissingFeature`/`UnknownSchema`   |
| `quantlib/bus/codec.py`                | `decode_view` (resolve-not-reject); keep `decode` for migration      |
| `quantlib/bus/publisher.py`            | idempotent `bus:schema:<fp>` publish on init/first emit              |
| `quantlib/bus/consumer.py`             | `BusConsumer` returns `FeatureView`s via the registry               |
| `quantlib/bus/compat.py` (new)         | `assert_compatible` / `IncompatibleSchema` + multi-strategy gate (§2.6) |
| `strategies/{smoke,reversion,overnight_beta}/` | declare `STRATEGY_FEATURES` contract (== `to_model_vector` list); publish `strategy:features:<name>` on startup; consume `FeatureView` |
| `ops/` deploy script / CI              | run the compat gate against the candidate fingerprint before relaunching `fc` |
| `tests/test_bus_feature_access.py` (new) | §4 proof tests (incl. compat-gate GREEN/RED, contract==model-input)         |
| `tests/bench_bus_decode.py` (new)      | §3 throughput benchmark (gates the build)                           |
