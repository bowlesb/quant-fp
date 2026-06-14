# GPU variant of fp-dev: adds the polars GPU engine (cudf-polars) so the SAME polars code can run on the
# RTX 3090 via `.collect(engine="gpu")`. Used for the backfill spike (GPU's home is the huge historical
# batch). Build:  docker build -t fp-gpu -f docker/fp-gpu.Dockerfile .   Run:  docker run --gpus all ...
FROM fp-dev
# polars[gpu] pulls cudf-polars-cu12 but NOT the nvJitLink CUDA runtime lib it loads at import — add it
# explicitly or `.collect(engine="gpu")` fails with "libnvJitLink.so not found".
RUN pip install --no-cache-dir "polars[gpu]" nvidia-nvjitlink-cu12 --extra-index-url=https://pypi.nvidia.com
