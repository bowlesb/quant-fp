-- Per-symbol asset metadata from Alpaca. Exchange/sector-style grouping for
-- cross-sectional work, and (critically for a long/short strategy) which names are
-- shortable / easy-to-borrow so we never plan a short we can't actually take.
CREATE TABLE IF NOT EXISTS asset_metadata (
    symbol         text PRIMARY KEY,
    name           text,
    exchange       text,
    tradable       boolean,
    marginable     boolean,
    shortable      boolean,
    easy_to_borrow boolean,
    fractionable   boolean,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
