#!/usr/bin/env bash
set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
WORKDIR="${WORKDIR:-/opt/lotsman-scout-tools}"
NVBANDWIDTH_REF="${NVBANDWIDTH_REF:-v0.9}"
NCCL_TESTS_REF="${NCCL_TESTS_REF:-master}"

mkdir -p "$WORKDIR" "$PREFIX/bin"

if ! command -v nvcc >/dev/null 2>&1; then
  echo "nvcc not found; skipping nvbandwidth and nccl-tests build"
  exit 0
fi

if ! command -v nvbandwidth >/dev/null 2>&1; then
  if [ ! -d "$WORKDIR/nvbandwidth/.git" ]; then
    git clone --depth 1 --branch "$NVBANDWIDTH_REF" \
      https://github.com/NVIDIA/nvbandwidth.git "$WORKDIR/nvbandwidth"
  fi
  cmake -S "$WORKDIR/nvbandwidth" -B "$WORKDIR/nvbandwidth/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$PREFIX"
  cmake --build "$WORKDIR/nvbandwidth/build" --parallel
  cmake --install "$WORKDIR/nvbandwidth/build"
else
  echo "nvbandwidth already installed: $(command -v nvbandwidth)"
fi

if command -v all_reduce_perf >/dev/null 2>&1; then
  echo "nccl-tests already installed: $(command -v all_reduce_perf)"
  exit 0
fi

if [ ! -e /usr/include/nccl.h ] && [ -z "${NCCL_HOME:-}" ]; then
  echo "NCCL headers not found; skipping nccl-tests build"
  exit 0
fi

if [ ! -d "$WORKDIR/nccl-tests/.git" ]; then
  git clone --depth 1 --branch "$NCCL_TESTS_REF" \
    https://github.com/NVIDIA/nccl-tests.git "$WORKDIR/nccl-tests"
fi

make -C "$WORKDIR/nccl-tests" MPI=0
install -m 0755 "$WORKDIR/nccl-tests/build/all_reduce_perf" "$PREFIX/bin/all_reduce_perf"
