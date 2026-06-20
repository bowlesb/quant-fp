# Bus Feature Access — name-addressed, fingerprint-decoupled consume

**Status:** SPEC / GATED. No live wiring in this PR. Goes to Lead review + Ben review BEFORE any build;
this touches LIVE strategy consumption, so it is gated like `STRATEGY_EXECUTION_ABSTRACTION.md`.

**Author:** strategy-core owner. **Off:** `origin/main` @ `b4fb88f`. **Fingerprint impact:** NONE — this
is consumer-side only. The wire format, the producer, and the schema/registry are unchanged; the feature
fingerprint `0x...694` is untouched. (The producer publishes ONE extra Redis key per fingerprint — a
side artifact, not a wire change.)

**Audit resolution (BatteryAudit, 2026-06-19 — core decouple VERIFIED; 5 gaps closed):**
- **B1 [HIGH] publish race →** §2.2: producer SETs+confirms `bus:schema:<fp>` BEFORE the first frame of that
  fp (publish-then-emit); consumer treats `UnknownSchema` as retry-with-backoff, never a hard stop.
- **B2 [HIGH / regression] version-bump silent corruption →** §2.6: contract pins `(name, version)`;
  `assert_compatible` RED on a version change of a CONSUMED feature (safe default), + an optional
  value-identical fast-path (noted follow-up). Restores the loud-failure the fp gate gave us, at feature
  granularity.
- **B3 [MED] contract drift / green-by-omission →** §2.6: contract derived from the model's construction
  constant (identity test); gate FAILS-CLOSED if an expected-live strategy hasn't published its contract.
- **B4 [MED] cache eviction →** §2.2: `bus:schema:<fp>` keys are no-TTL + eviction-exempt (explicit).
- **B5 [LOW] present-but-NaN →** §2.4: `to_model_vector` guarantees PRESENCE, not finiteness; warmup gating
  owns finiteness — documented so nobody assumes an all-finite vector.

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
  set). One tiny key per fingerprint, a few thousand short lines, written once.
- This is a **side artifact**, not a wire change: it does not touch the frame bytes, the registry, or the
  fingerprint computation. It is the publish of an *existing* object (`BusSchema`) the producer already
  holds.

**B1 — publish-then-emit ordering (must-fix).** The schema key MUST be visible before the first frame of a
new fingerprint, or the transition frame (or a reconnect reading retained `MAXLEN≈4h` frames) can reach a
consumer before `bus:schema:<fp>` exists → `UnknownSchema` → a strategy stops trading mid-session: exactly
the failure this design prevents. Two-sided fix:

- **Producer (strict ordering):** before the FIRST `XADD` of a given fingerprint, the `BusPublisher` SETs
  `bus:schema:<fp>` and CONFIRMS the write (the `SET` ack returns, or a read-back) — *publish-then-emit*.
  This is checked once per fingerprint (a `_published_fps` set guard), so it adds nothing to the steady-
  state publish path. A fingerprint never emits a frame whose schema isn't already resolvable.
- **Consumer (never hard-stop on a lag):** a decode that can't yet resolve `bus:schema:<fp>` raises
  `UnknownSchema`, which the consumer treats as **retry-with-backoff** — re-poll the registry a few times
  (short capped backoff), NOT a hard stop. The frame is held/re-tried (the stream entry isn't acked-past
  until resolved), so a brief resolve lag (or a not-yet-replicated key) self-heals instead of killing the
  container. Only a genuinely unresolvable fingerprint after the bounded retries surfaces as an operational
  error (logged loudly), and even then the container keeps polling other frames rather than crashing.

**B4 — eviction-resistance (must be explicit).** `bus:schema:<fp>` keys carry **no TTL** and MUST be exempt
from Redis key eviction — either the bus runs on a Redis instance with `maxmemory-policy noeviction`, or the
schema keys live on a non-evictable backend (a small dedicated namespace/instance, or the durable registry
backend below). The frame streams are independently `MAXLEN`-trimmed (bounded memory) and do not depend on
this; the schema keys are tiny and few (one per fingerprint ever seen), so exempting them is cheap. A
`SchemaRegistry` abstraction wraps the lookup so a future durable backend (a DB table, a file) can replace
Redis without touching consumers.

- **Bootstrap / same-fingerprint fallback:** if `bus:schema:<fp>` is still unresolvable for a fingerprint a
  consumer sees AND that fingerprint == the consumer's OWN compiled schema, the consumer falls back to its
  compiled schema (today's behavior — the offsets are knowably correct). For any OTHER fingerprint, only
  the B1 retry-with-backoff applies; there is no compiled fallback for a fingerprint the consumer wasn't
  built against (it has no trustworthy offset map for it).

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
- **B5 — present-but-NaN is NOT this layer's job (document it).** `to_model_vector` guarantees the requested
  features are PRESENT and correctly placed; it does **not** guarantee they are FINITE. A feature that is
  present but `NaN` that minute (warmup / sparse) passes `has(name)` and is fed as `NaN` to the model — this
  is correct: finiteness is the responsibility of the strategy's warmup gate (don't bet until the needed
  features are finite, `STRATEGY_CONTAINERS.md`), not of the name-resolution layer. Nobody should assume
  `to_model_vector` returns an all-finite vector.

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

**The declared-feature contract — pins (name, VERSION) (B2 must-fix).** Each strategy DECLARES the exact
features it consumes, each pinned to the group VERSION it was certified against — its contract with the bus.
Name alone is NOT sufficient (see B2 below); the contract is a list of `(name, version)`:

```python
# strategies/<name>/contract.py
STRATEGY_FEATURES: tuple[FeatureReq, ...] = (
    FeatureReq("ret_1m", version="v3"),
    FeatureReq("volume_zscore_5m", version="v3"),
)   # smoke, e.g.  (FeatureReq = a tiny frozen (name, version) dataclass)
```

The names in this contract are the SAME `expected_names` the strategy passes to `to_model_vector` — see
B3 (single source of truth, derived from the model's construction constant, not a parallel list).

> **B2 [HIGH / REGRESSION — the important one].** The fingerprint is a blake2b over `group:name:VERSION`,
> so a **version bump changes the fingerprint but keeps the names**. Today that halts the strategy (fp
> reject) — a loud, safe stop. A name-only resolve would SILENTLY feed the model the new-version,
> *differently-computed* feature — silent model corruption replacing a loud halt. The fix: the contract
> pins `version`, and `assert_compatible` checks the candidate field's version == the declared version, so
> a version change of a CONSUMED feature is **RED** (the strategy then opts in deliberately, exactly like an
> addition). This restores the loud-failure property the old fp gate gave us — at feature granularity.

**The reusable check — name AND version (B2).** A pure function; no Redis, no live state:

```python
class IncompatibleSchema(Exception):
    """A live strategy needs a feature the candidate lacks, or at a different version (exact list)."""

def assert_compatible(
    candidate: BusSchema, declared: Sequence[FeatureReq], *, strategy: str,
    value_identical_bumps: Mapping[str, str] = {},   # name -> version the producer annotated value-identical
) -> None:
    missing, changed = [], []
    for req in declared:
        field = candidate.field(req.name)            # None if absent
        if field is None:
            missing.append(req.name)
        elif field.version != req.version and value_identical_bumps.get(req.name) != field.version:
            changed.append((req.name, req.version, field.version))   # version bump, NOT value-identical
    if missing or changed:
        raise IncompatibleSchema(
            f"strategy '{strategy}' vs candidate {candidate.fingerprint:#018x}: "
            f"missing={missing} version_changed={changed}"
        )
```

i.e. every declared `(name, version)` must be present in the candidate AT THE SAME VERSION. Subset on names
(not equal) is why additions are safe; the version check is why a re-computation of a consumed feature can
never slip in silently.

**Version bumps are usually value-identical restructures (#203-style) — the safe-default + opt-in fast-path
nuance.** Most of our version bumps are value-identical (the feature's numbers don't change; only its
internal computation/registration did). For those, forcing a strategy opt-in is friction, not safety. So the
spec is:

- **SAFE DEFAULT (in v1, required): RED on ANY consumed-feature version change.** A version bump of a
  feature a live strategy declares blocks the deploy until the strategy opts in (bumps its `FeatureReq`
  version and rebuilds) — identical ergonomics to adopting an addition. This is the must-have B2 fix.
- **OPTIONAL value-identical fast-path (the friction-reducer): the producer ANNOTATES a bump as
  value-identical** (a `bus:value_identical:<fp>` set, or a per-field flag the producer asserts when it
  knows the numbers are unchanged — e.g. a #203-style restructure verified equal in CI). The gate then
  auto-passes that specific bump (the `value_identical_bumps` arg above). **Recommendation: ship the safe
  default in v1; the value-identical fast-path is a noted FOLLOW-UP** — it requires a producer-side
  value-identity assertion we should design carefully (a wrong annotation re-introduces the silent-
  corruption risk), so it should not gate the v1 build. The safe default alone fully closes B2.

**The pre-deploy gate.** Before relaunching `fc` on a new feature set, run — in CI or the deploy script —
`assert_compatible(candidate_schema, contract, strategy=name)` for EVERY live strategy's declared contract
against the NEW candidate fingerprint's schema:

- **GREEN** (every live strategy's `(name, version)` set resolves in the candidate) → `fc` deploys freely.
  Additions are non-breaking by construction; strategies are untouched and keep trading across the change.
- **RED** → block the deploy and name the EXACT feature(s) at fault per strategy: `missing` (removed/
  renamed) and `version_changed` (a consumed feature was re-computed). Surfaced *precisely, before deploy* —
  never a silent break, never a runtime decode crash, never a silent re-computation fed to a model.
- **FAILS-CLOSED on a missing contract (B3).** The gate is parameterized with the set of strategies it
  EXPECTS to be live. If any expected strategy hasn't published its contract (it's down, mid-restart, or
  never registered), the gate is **RED**, not green-by-omission — a down strategy must never be cleared by
  silence. (Operationally: bring the strategy up / confirm its contract before the deploy proceeds.)

**What this replaces.** The manual "rebuild fc + 3 strategies, atomically" coordination collapses to:

```
rebuild fc  →  run compat gate (assert_compatible per strategy)  →  GREEN: relaunch fc  /  RED: block + name the feature
```

Strategies do not move unless THEY choose to adopt a new (or re-versioned) feature — they bump their own
`STRATEGY_FEATURES` and rebuild on their own schedule. The error surface of a feature deploy is reduced to
**one precise, checkable condition**: does every live strategy's declared `(name, version)` set resolve in
the new schema (with version-identical fields), and has every expected strategy published its contract?

**Single source of truth — contract derived from the model's construction constant (B3).** The declared
contract must NOT be a parallel hand-kept list that can drift from what the model actually reads. It is
derived from the SAME constant the model is CONSTRUCTED with. E.g. `MockMLModel(feature_names=...)` already
takes its features as a constructor arg today — the contract IS that constant (the strategy exposes
`model.required_features()` / the constant the model was built from), and `to_model_vector` is called with
that same list. A unit test asserts the published contract == the model's construction list (identity), so
the gate can never pass while the model would read something different at runtime.

**Where the gate gets the contracts.** Each running strategy publishes its declared `(name, version)`
contract to Redis (`strategy:features:<name>`) on startup; the gate reads the LIVE set — so it checks what
is ACTUALLY running, not a possibly-stale static manifest. The gate is given the EXPECTED-live strategy set
and **fails closed** if any expected strategy's contract is absent (B3). A static import of each strategy's
constant is the CI smoke-check (runnable with no live cluster), but the deploy gate uses the live published
contracts.

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
5. **Unknown schema retries, never hard-stops (B1).** A fingerprint not yet resolvable → the consumer
   retries-with-backoff and SUCCEEDS once the fake registry is populated mid-test (the frame is not dropped,
   the container does not stop). A fp == the consumer's own compiled schema with a missing key → falls back
   to the compiled schema. Only an unresolvable fp after bounded retries surfaces a logged operational error
   while the consumer keeps polling other frames.
6. **`FeatureRow` parity.** `FeatureView` satisfies the `FeatureRow` protocol; the re-homed
   `VwapReversionModel` / `MockMLModel` produce identical `Prediction`s reading a `FeatureView` vs the
   current `FeatureVector` on the same values (parity-by-construction at the decision layer).
7. **Compat gate GREEN on additions.** `assert_compatible(candidate_schema, declared={A, B})` where the
   candidate ADDS features but still contains {A, B} → passes (no raise). The deploy is cleared.
8. **Compat gate RED names the exact feature.** A candidate schema that REMOVED/renamed `B` → declared {A,
   B} raises `IncompatibleSchema` whose message names `B` and the candidate fingerprint. A multi-strategy
   gate run reports every affected strategy + its missing names, and blocks.
9. **Contract == model-input list (B3).** A strategy's declared `STRATEGY_FEATURES` names equal the
   `expected_names` it passes to `to_model_vector`, both derived from the model's construction constant — a
   test asserts identity, so the gate can never pass while the model would read something different.
10. **⭐ Version-bumped consumed feature → RED (B2, the regression test).** Candidate keeps name `B` but at a
    DIFFERENT version than the contract pins. `assert_compatible` raises `IncompatibleSchema` with `B` in
    `version_changed` (declared vs candidate version). And: a bump annotated value-identical in
    `value_identical_bumps` → auto-passes (the fast-path). Proves a silent re-computation can't slip in.
11. **Publish-then-emit ordering (B1).** With a registry where the schema for `fp_new` is written only AFTER
    the first frame, the consumer's retry-with-backoff still resolves it (no stop); and a producer test
    asserts `SET bus:schema:<fp>` is confirmed before the first `XADD` of that fp.
12. **Gate fails closed on a missing contract (B3).** The gate, told strategies {smoke, reversion,
    overnight_beta} are expected live, with only two having published a contract → RED (names the strategy
    whose contract is absent), NOT green-by-omission.

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
                         for every EXPECTED-live strategy  →  GREEN: relaunch fc  /  RED: block + name the feature
   ```
   On GREEN, `fc` ships alone: on first emit of the new fingerprint it publishes `bus:schema:<fp>` and
   confirms it BEFORE the first frame (B1 publish-then-emit); consumers resolve + cache it (retry-with-
   backoff bridges any momentary lag) and keep reading their declared, version-pinned names. **No strategy
   rebuild, no coordinated window, no fingerprint coordination.** A strategy moves only when IT chooses to
   adopt a new (or re-versioned) feature — bumps its own contract + rebuilds on its own schedule.

**The vision this lands (Ben):** feature PRs merge and `fc` deploys CONTINUOUSLY, gated by the automated
compat check. Active strategies are never at risk and never forced to rebuild. The entire risk surface of a
feature deploy is reduced to one precise, automatable condition — *does every expected-live strategy's
declared `(name, version)` set resolve in the new schema, and has each published its contract?* — checked
BEFORE the deploy, naming the exact incompatible feature (missing OR version-changed) if not.

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
| `quantlib/bus/schema.py`               | `BusSchema.to_json()/from_json()`; `offsets()` map + `field(name)` (version) accessor |
| `quantlib/bus/registry.py` (new)       | `SchemaRegistry` (Redis + fake), per-fingerprint cache; **B4** non-evicting/no-TTL keys |
| `quantlib/bus/view.py` (new)           | `FeatureView`, `to_model_vector`, `MissingFeature`/`UnknownSchema` (**B5**: present≠finite) |
| `quantlib/bus/codec.py`                | `decode_view` (resolve-not-reject); keep `decode` for migration      |
| `quantlib/bus/publisher.py`            | **B1** publish-then-emit: SET+confirm `bus:schema:<fp>` before first `XADD` of that fp |
| `quantlib/bus/consumer.py`             | `BusConsumer` returns `FeatureView`s via the registry; **B1** `UnknownSchema` = retry-with-backoff, never hard-stop |
| `quantlib/bus/compat.py` (new)         | `FeatureReq(name, version)`; `assert_compatible` (**B2** name AND version) / `IncompatibleSchema`; multi-strategy gate, **B3** fails-closed on a missing contract + optional value-identical fast-path |
| `strategies/{smoke,reversion,overnight_beta}/` | declare `STRATEGY_FEATURES` `(name, version)` contract derived from the model's construction constant (**B3**); publish `strategy:features:<name>` on startup; consume `FeatureView` |
| `ops/` deploy script / CI              | run the compat gate (with the EXPECTED-live strategy set, fail-closed) against the candidate fingerprint before relaunching `fc` |
| `tests/test_bus_feature_access.py` (new) | §4 proof tests 1–12 (incl. **B2** version-bump→RED, **B1** retry+ordering, **B3** fail-closed) |
| `tests/bench_bus_decode.py` (new)      | §3 throughput benchmark (gates the build)                           |
