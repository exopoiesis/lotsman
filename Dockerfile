# Standalone Lotsman test image. Validates the daemon runs cleanly in a
# Linux container before deploying into per-tool images (infra-qe-gpu,
# infra-cp2k-gpu, ...) where Lotsman is layered on top.
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/exopoiesis/lotsman"
LABEL org.opencontainers.image.description="Lotsman in-container daemon"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# bash is a core runtime dependency — Lotsman invokes scripts via bash.
# Scout uses fio plus standard Linux inventory tools. build-essential/cmake/git
# are intentionally present so CUDA-derived images can build optional NVIDIA
# probes (nvbandwidth, nccl-tests) without changing the Lotsman layer.
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    cmake \
    curl \
    fio \
    git \
    build-essential \
    numactl \
    pciutils \
    procps \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/lotsman

# Copy package source (driven by .dockerignore for what to exclude)
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
COPY proto/ ./proto/

RUN chmod +x src/lotsman/scout/install_gpu_tools.sh

# Install — registers `lotsman` and `marina` console commands via
# [project.scripts]. No editable mode in production image.
RUN pip install --no-cache-dir .

# Runtime layout
RUN mkdir -p /var/lotsman/jobs /etc/lotsman

# Defaults — overridable by env or CMD args
ENV LOTSMAN_HOST_ID=container \
    LOTSMAN_PORT=50051 \
    LOTSMAN_JOBS_DIR=/var/lotsman/jobs

EXPOSE 50051

# Default command. Manifest path is optional — Lotsman returns empty
# Whoami fields if /etc/lotsman/manifest.toml is missing.
CMD ["sh", "-c", "exec lotsman serve --host-id $LOTSMAN_HOST_ID --port $LOTSMAN_PORT --jobs-dir $LOTSMAN_JOBS_DIR --manifest /etc/lotsman/manifest.toml"]
