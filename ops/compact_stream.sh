#!/usr/bin/env bash
# Driver for SETTLED-DAY STREAM COMPACTION (quantlib.features.compact --settled) — docs/STREAM_COMPACTION.md.
#
# The live append path writes one small file per (shard, minute) for O(1) crash-safe writes, leaving
# ~375k tiny stream files per session day (docs/STREAM_COMPACTION.md). compact_settled_days folds each
# SETTLED day's per-minute files into one reader-transparent data-compacted.parquet (de-dup (symbol,minute)
# keep-last, zstd-19), reclaiming the inodes + read-amplification that bloats the nightly sweep + dashboard.
#
# SAFETY: --settled compacts ONLY days STRICTLY BEFORE today. fc writes exclusively to today's date=
# partition, so a settled day is never being appended to — nothing fc holds open is touched. The fold is
# atomic (os.replace the compacted file in BEFORE deleting the per-minute files) and IDEMPOTENT (an
# already-compacted day folds 0 files), so a re-run or a crash mid-run is safe. It is READER-TRANSPARENT
# (the store still globs data*.parquet) and changes NO schema/format/cell — zero bus-fingerprint impact.
#
# Runs in the baked fp-dev image against the PRODUCTION store volume (read+rewrite of settled stream
# partitions only). NEVER docker-exec's the live feature-computer.
#
#   ops/compact_stream.sh           # compact every settled stream day on disk (< today)
#   SOURCE=backfill ops/compact_stream.sh   # compact settled backfill partitions instead (rarely needed)
set -uo pipefail

REPO="${REPO:-/home/ben/quant-fp}"
IMAGE="${IMAGE:-fp-dev}"
STORE_VOLUME="${STORE_VOLUME:-fp_store_real}"
NETWORK="${NETWORK:-quant_default}"
ENV_FILE="${ENV_FILE:-$REPO/.env}"
SOURCE="${SOURCE:-stream}"

GIT_COMMIT="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || true)"

docker run --rm \
  --network "$NETWORK" \
  --env-file "$ENV_FILE" \
  -e FP_GIT_COMMIT="$GIT_COMMIT" \
  -v "$STORE_VOLUME":/store \
  -v "$REPO":/app -w /app \
  "$IMAGE" \
  python -m quantlib.features.compact /store --settled "$SOURCE"
