-- Training-data export: one row per (symbol, ts, horizon) joining the historical
-- feature vector to its forward-return label. The modeling code reads this and
-- expands `vector` against feature_sets.names. Only source='historical' features
-- are used for training (the authoritative recompute; see docs/archive/ARCHITECTURE.md).

CREATE OR REPLACE VIEW training_data AS
SELECT
    fv.symbol,
    fv.ts,
    fv.set_version,
    fv.vector,
    l.horizon,
    l.value AS label
FROM feature_vectors fv
JOIN labels l
  ON l.symbol = fv.symbol AND l.ts = fv.ts
WHERE fv.source = 'historical';
