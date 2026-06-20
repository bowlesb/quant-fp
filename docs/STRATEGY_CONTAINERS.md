# Strategy containers

A *strategy container* is a standalone service that subscribes to the **feature-vector bus** for the
tickers it cares about, decides paper bets from each vector, and records its book to its **own** Postgres
schema. The first one is `smoke-strategy` — deliberately trivial, its job is to prove the apparatus end
to end before real edge is added. This doc is the contract for writing the next one.

**Related docs:** a container's decision logic should be the write-once `decide()` from
`STRATEGY_BATTERY_PORTABILITY.md` (so the same code backtests in the battery and runs live);
`STRATEGY_EXECUTION_ABSTRACTION.md` is the gated design for the production-real STATE + Executor + Feed
those decisions run on.

A new container needs to do four things, none of which require touching any global DB or feature-pipeline
internals:

1. subscribe to vectors for N tickers,
2. register its own tables,
3. consume vectors via the typed accessor,
4. place / manage / finalize paper bets — optionally gated by a model.

---

## 1. Subscribe to vectors for only some tickers

The producer publishes one Redis Stream **per symbol**, keyed `fv:<SYMBOL>` (`quantlib/bus/publisher.py`,
`stream_key`). A consumer `XREAD`s only the streams for its declared symbols, so it never pays to
deserialize the other ~11k tickers.

```python
from quantlib.bus.consumer import BusConsumer

consumer = BusConsumer(["AAPL", "MSFT"], url="redis://quant-redis:6379/0")
while True:
    vectors = consumer.poll(block_ms=1000, count=200)  # only AAPL/MSFT frames, decoded
    for vector in vectors:
        ...
```

- `symbols` is the **only** subscription knob — pass the tickers you want, get back exactly those.
- `start="$"` (the default) means "only frames published after I connect"; `start="0"` replays the
  stream from the beginning (handy for inspection/backfill of the in-Redis window).
- The consumer tracks each stream's last-seen id, so it never re-reads a frame.
- **Make it env-driven.** Smoke reads `SMOKE_SYMBOLS` (comma-separated, see `_env_symbols`); a new
  container should follow the same pattern with its own `*_SYMBOLS` var so the ticker set is pure config,
  set in `docker-compose.strategies.yml`. Default to a small liquid set if unset.

### Schema fingerprint safety

`decode` validates the frame's 64-bit schema fingerprint against the consumer's locally-built
`BusSchema` (`quantlib/bus/{codec,schema}.py`). A container built against a different feature set fails
**loudly** rather than silently misreading offsets — so a container MUST build from the same `quantlib`
the producer runs (the `Dockerfile` `COPY quantlib` does this). Live fingerprint today:
`0x873f2fceb8f00c92` over 728 features in 63 groups.

### What "complete + acceptable" looks like (verified)

Use the inspect CLI (below) to check this for yourself. Observed against the live bus:

- Every frame is **structurally complete**: 728 cells, fingerprint match — never a partial/misaligned
  vector.
- **NaN is expected and normal.** A cell is NaN when its feature is genuinely absent for that
  (symbol, minute). Two honest reasons NaN is high in practice:
  - **Warmup.** A symbol's *first* published minute has ~76% NaN (e.g. `ret_1m` needs a prior bar).
    The *next* minute drops to ~60% NaN as windowed features fill in. A strategy should treat early
    minutes as warmup and not bet until the features it needs are finite.
  - **Per-shard / cross-sectional features.** Breadth and cross-sectional-rank groups need the whole
    universe in one place; when the publish is assembled per shard, those cells can be NaN for symbols
    not in that shard's cross-section. Do **not** assume those are populated unless you have verified it.
- A consuming strategy must therefore **check the specific features it uses for `isfinite`** and skip the
  bar otherwise — never feed a NaN into sizing/PnL.

---

## 2. Register your own tables (self-service, no DB internals)

Use `StrategyStore` (`strategies/lib/store.py`). You give it a strategy name and a list of
`CREATE TABLE` statements that use the `{schema}` placeholder; it creates an **isolated** `strat_<name>`
schema and all the tables idempotently on construction, and hands you safe parameterized read/write. You
never see global DB config or any schema-management code.

```python
from strategies.lib.store import StrategyStore

BETS_TABLE = """
CREATE TABLE IF NOT EXISTS {schema}.bets (
    id     bigserial PRIMARY KEY,
    symbol text NOT NULL,
    ...
)
"""

store = StrategyStore.from_env("mystrat", [BETS_TABLE])   # creates schema "strat_mystrat" + bets
store.execute(f"INSERT INTO {store.schema}.bets (symbol) VALUES (%s)", ("AAPL",))
rows = store.query(f"SELECT symbol FROM {store.schema}.bets")
dicts = store.query_dicts(f"SELECT symbol FROM {store.schema}.bets", ("symbol",))
row = store.execute_returning(f"INSERT INTO {store.schema}.bets (symbol) VALUES (%s) RETURNING id", ("AAPL",))
```

- The per-strategy `strat_<name>` schema is the **isolation boundary**: no table-name collision with the
  executor or any other strategy; can be dropped/migrated independently.
- `from_env` reads `DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD` (set by compose); the author never
  hardcodes connection details.
- All access is parameterized (`%s` placeholders) over short-lived autocommit connections, so a transient
  DB blip never holds a stale handle.
- `register()` is idempotent — safe to call on every startup.
- `drop()` exists for **tests/teardown only** — never call it on a live schema.

`strategies/smoke/bet_store.py` is the worked example: it declares one `bets` table and wraps
`StrategyStore` with typed `record_open / mark_filled / mark_closing / record_close / list_open /
count_open / open_notional` methods.

### The smoke bettor's tables (`strat_smoke`)

One table, `strat_smoke.bets`, status-partitioned by the `status` column
(`open` → `filled` → `closing` → `closed`):

| column | meaning |
|---|---|
| `id` | bigserial PK |
| `symbol`, `side` | the long paper bet |
| `entry_notional` | target dollar size of the open (`SMOKE_NOTIONAL_USD`) — the bet is a NOTIONAL buy |
| `qty` | filled (fractional) shares of the open (NULL until filled) |
| `entry_order_id` (UNIQUE) | Alpaca `client_order_id`, prefix `smoke_` — the idempotency key |
| `entry_ts`, `entry_price` | submit time / avg fill (NULL until filled) |
| `hold_until` | when the time-based exit fires |
| `exit_order_id`, `exit_ts`, `exit_price` | the closing order + its fill |
| `realized_pnl` | `(exit_price - entry_price) * qty` |
| `status` | `open` / `filled` / `closing` / `closed` |

---

## 3. Consume vectors via the accessor

A decoded `FeatureVector` (`quantlib/bus/vector.py`) is addressed by **name**, never by offset/bytes:

```python
vector.value("momentum_fast_1")     # O(1) name -> float (hot path)
vector["momentum_fast_1"]           # same, dict style
vector.momentum.momentum_fast_1     # group.feature attribute access (validates group membership)
vector.array                        # raw float64 numpy view for vectorized math
vector.to_dict()                    # {name: value}
vector.symbol, vector.minute        # identity
```

A typo or wrong-group access **raises** rather than silently resolving. Always `numpy.isfinite`-check
the specific cells you depend on (see the NaN notes above).

---

## 4. Place / manage / finalize paper bets — and the model interface

The smoke loop (`strategies/smoke/strategy.py`) is the template:

- **consume + sample** — poll the bus, remember the latest vector/symbol, log a couple of real features.
- **manage** — for each open bet, capture the entry fill, and when `hold_until` passes submit the closing
  sell (idempotent `client_order_id`), capture the exit fill, compute realized PnL, mark `closed`.
- **maybe place** — a **pure** safety gate (`evaluate_bet_gate`, unit-testable, no I/O) enforces, in
  order: kill switch (`SMOKE_ENABLED=0`), market-hours-only (broker clock), cadence
  (`SMOKE_BET_INTERVAL_SEC`), max concurrent (`SMOKE_MAX_CONCURRENT`), and max total open notional
  (`SMOKE_MAX_TOTAL_NOTIONAL_USD`, checked against ACTUAL open dollar exposure inclusive of the
  prospective bet, so a high-priced symbol can never blow the cap). All bets are **paper only** and
  tiny: a **NOTIONAL** market buy for `SMOKE_NOTIONAL_USD` dollars (fractional shares), so a $50 bet
  costs ~$50 regardless of share price — never a whole share.
- **reconcile on startup** — resume managing store-open bets against the broker, closing any past their
  hold; idempotent so restarts converge.

**Cadence + types:** smoke places at most **one** bet every `SMOKE_BET_INTERVAL_SEC` (default 300s),
notional market-buy long entry (`notional=SMOKE_NOTIONAL_USD`) → time-based market-sell exit of the
filled fractional `qty` after `SMOKE_HOLD_SEC` (default 900s). It only goes
long. Transient bus/broker/db errors are caught specifically and the loop continues; bare `except` is
never used.

### Swapping a real model in for `MockMLModel`

The bet decision can be gated by a model implementing the `Model` protocol (`strategies/lib/model.py`):

```python
class Model(Protocol):
    def predict(self, vector: FeatureVector) -> Prediction: ...   # Prediction.probability in [0, 1]
```

`MockMLModel` returns a **deterministic-but-varied** pseudo-probability from a hash of
`(symbol, minute[, folded feature values])` — no wall-clock, no RNG, NaN-safe — so the betting logic is
exercised by a model-like signal that is reproducible in tests. Day-2, a trained classifier with the
same `predict(vector) -> Prediction` interface drops in unchanged.

Wiring in smoke (behind a flag, **off by default so behaviour is unchanged**):

- `SMOKE_USE_MODEL=0` (default): bet purely on the safety gate (legacy behaviour).
- `SMOKE_USE_MODEL=1`: after the safety gate passes, require `model.predict(latest_vector).probability >
  SMOKE_MODEL_THRESHOLD` (default 0.5) before placing the bet. The safety caps still apply on top — the
  model can only ever *reduce* trading, never bypass a cap.

To go live: construct `SmokeStrategy(..., model=YourTrainedModel())` in the entrypoint and set
`SMOKE_USE_MODEL=1`.

---

## Inspect / debug the bus

`strategies/tools/inspect_bus.py` is a CLI to verify vectors arrive complete with sane values:

```bash
# Follow live vectors for some tickers (pretty-print fingerprint, NaN fraction, min/max, samples):
python -m strategies.tools.inspect_bus --symbols AAPL,MSFT
python -m strategies.tools.inspect_bus --symbols AAPL --once     # one vector then exit
python -m strategies.tools.inspect_bus --symbols AAPL --full     # dump every group.feature = value

# Network-light self-test: publish a known synthetic vector to a private stream prefix and read it back,
# proving the encode/decode + fingerprint path end to end without a live producer or market hours:
python -m strategies.tools.inspect_bus --symbols AAPL,MSFT --synthetic
```

Run it inside a throwaway container with bus access:

```bash
docker run --rm --network quant_default -v "$PWD":/app -w /app --env-file .env -e PYTHONPATH=/app \
  fp-dev python -m strategies.tools.inspect_bus --symbols AAPL,MSFT --synthetic
```

---

## Run a strategy container

```bash
docker build -f docker/fp-dev.Dockerfile -t fp-dev .                          # shared base, once
docker compose -f docker-compose.yml -f docker-compose.strategies.yml up -d --build smoke-strategy
```

Each strategy is its own compose service: env-driven symbols + risk caps + (optional) model flags, its
own `strat_<name>` schema, paper-only Alpaca. Copy the `smoke-strategy` service as the starting point.
