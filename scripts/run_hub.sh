#!/usr/bin/env bash
# Arranca el hub de demos (127.0.0.1:8180). Todo queda dentro del workspace.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

export TMPDIR="$ROOT/.tmp"
mkdir -p "$TMPDIR" logs
export LD_LIBRARY_PATH="$ROOT/vendor/llama.cpp/build/bin${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# OpenBLAS lee esto al cargar: 2 threads ganan al default de 8 para el escaneo coseno.
export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-2}"
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-2}"
export DSH_THREADS="${DSH_THREADS:-4}"          # threads de llama.cpp y de parakeet
export DSH_RAM_BUDGET_MB="${DSH_RAM_BUDGET_MB:-4096}"
# Contexto de los LLM. 16384 para que un agente como pi con todas sus
# tools (~10.100 tokens de system prompt) entre sin recortar. 8192 alcanza para
# el chat solo, y ahorra ~230-330 MB de KV cache por modelo.
export DSH_CTX_LLM="${DSH_CTX_LLM:-16384}"
export YOLO_CONFIG_DIR="${YOLO_CONFIG_DIR:-$ROOT/.tmp/ultralytics}"

PY="${PY:-$ROOT/.venv/bin/python}"
echo "[hub] en http://127.0.0.1:8180  ·  RAM presupuesto: ${DSH_RAM_BUDGET_MB} MB  ·  threads: ${DSH_THREADS}"
exec "$PY" -m uvicorn --app-dir "$ROOT/hub" server:app \
  --host 127.0.0.1 --port 8180 --log-level "${LOG_LEVEL:-warning}"
