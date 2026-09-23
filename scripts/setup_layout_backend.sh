#!/usr/bin/env bash
# One-time setup for the Surya layout/reading-order backend.
#
# surya-ocr>=0.20 serves its layout model as a VLM via either Docker+GPU
# (vllm) or a locally-run `llama-server` process (llama.cpp). This project
# uses the llama.cpp path to avoid requiring Docker + nvidia-container-toolkit
# to be configured on the host. This script downloads a prebuilt CUDA
# llama.cpp build into .tools/llamacpp/ (gitignored, self-contained, never
# touches anything system-wide) -- bookocr/layout/surya_layout.py finds it
# there automatically and points Surya's env vars at it.
#
# Re-run this if .tools/llamacpp/ is missing (fresh clone) or if you want to
# bump the llama.cpp build. Requires an NVIDIA GPU + driver; for a CPU-only
# or non-NVIDIA machine, download the matching non-CUDA build from
# https://github.com/ggml-org/llama.cpp/releases instead and skip the
# cudart download -- Surya will still work, just without GPU acceleration.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$PROJECT_ROOT/.tools/llamacpp"
CUDA_VARIANT="${LLAMACPP_CUDA_VARIANT:-cuda-12.8}"  # override if your driver needs a different CUDA build

if [ -d "$TOOLS_DIR" ] && [ -n "$(find "$TOOLS_DIR" -maxdepth 1 -iname 'llama-b*' 2>/dev/null)" ]; then
  echo "Already set up at $TOOLS_DIR (delete it to force a re-download)."
  exit 0
fi

echo "Checking for an NVIDIA GPU..."
if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "No nvidia-smi found. This script assumes an NVIDIA GPU + driver;"
  echo "for CPU-only, manually download a non-CUDA llama.cpp build from"
  echo "https://github.com/ggml-org/llama.cpp/releases into $TOOLS_DIR/"
  exit 1
fi
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader

echo "Fetching latest llama.cpp release tag..."
TAG=$(curl -s "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=1" | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['tag_name'])")
echo "  -> $TAG"

mkdir -p "$TOOLS_DIR"
cd "$TOOLS_DIR"

BASE_URL="https://github.com/ggml-org/llama.cpp/releases/download/$TAG"
echo "Downloading llama-$TAG-bin-ubuntu-$CUDA_VARIANT-x64.tar.gz ..."
curl -sL -o llama.tar.gz "$BASE_URL/llama-$TAG-bin-ubuntu-$CUDA_VARIANT-x64.tar.gz"
echo "Downloading cudart-llama-$TAG-bin-ubuntu-$CUDA_VARIANT-x64.tar.gz ..."
curl -sL -o cudart.tar.gz "$BASE_URL/cudart-llama-$TAG-bin-ubuntu-$CUDA_VARIANT-x64.tar.gz"

tar -xzf llama.tar.gz
tar -xzf cudart.tar.gz
rm llama.tar.gz cudart.tar.gz

BINARY=$(find "$TOOLS_DIR" -maxdepth 2 -iname "llama-server" | head -1)
if [ -z "$BINARY" ]; then
  echo "ERROR: llama-server binary not found after extraction." >&2
  exit 1
fi

echo "Verifying binary runs..."
LD_LIBRARY_PATH="$(dirname "$BINARY"):$(find "$TOOLS_DIR" -maxdepth 1 -iname 'cudart-llama-*' | head -1)" \
  "$BINARY" --version

echo "Done. bookocr will use this automatically -- no env vars to set by hand."
echo "First real conversion will additionally download the surya-2 GGUF model (~1.5GB, one-time, cached by huggingface_hub)."
