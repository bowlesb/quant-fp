# Torch + CUDA GPU image for the representation-learning lane (RTX 3090, sm_86, CUDA 12.2 driver).
#
# The OFFLINE training half of the repr-learning lane. Inference of any SHIPPED feature does NOT use this
# image — shipped features export encoder weights to numpy and run as a pure-numpy FeatureGroup in the
# normal fp-dev/capture image (CPU, sub-ms, parity-true). This image is for GPU training + research only.
#
# Build:  docker build -t fp-torch -f docker/fp-torch.Dockerfile .
# Verify GPU:  docker run --rm --gpus all fp-torch python -c \
#     "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# Train:  docker run --rm --gpus all -v "$PWD":/app -w /app fp-torch \
#     python experiments/gpu_repr/train_vae.py --panel <path>
#
# cu121 wheels run fine on the 12.2 driver (confirmed in dl_research/.venv). We pin the same torch the
# prototype venv proved, so the container reproduces the venv exactly.
FROM fp-dev

RUN pip install --no-cache-dir \
        torch==2.5.1 \
        --index-url https://download.pytorch.org/whl/cu121 \
    && pip install --no-cache-dir \
        scikit-learn \
        umap-learn

# Sanity: the image must import torch even without a GPU attached (CUDA is checked at runtime via --gpus all).
RUN python -c "import torch; print('torch', torch.__version__, 'built')"
