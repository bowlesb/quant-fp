# Experiment & Decision Journal

Append-only. Newest entries at the top. Experiments record: hypothesis, config
hash, out-of-sample result, verdict, next step. Decisions record what changed and
why.

---

## 2026-06-10 — Project start

- Decision: build fresh at `~/quant`, ignoring prior repos (Edgar,
  automated-day-trading) per Ben's explicit request.
- Decision: committed approach = cross-sectional short-horizon ML ranking on a
  ~1,000-symbol liquid universe; LightGBM; paper-first with statistical gates.
  Rationale in `ARCHITECTURE.md`.
- Started Phase 0 foundation.
