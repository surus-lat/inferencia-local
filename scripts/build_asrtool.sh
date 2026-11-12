#!/usr/bin/env bash
# Compila el worker ASR (live-asr) que usa el hub de SURUS.
# Idempotente: si ya está buildado y nada cambió, cmake --build no recompila nada.
#
#   ROOT/scripts/build_asrtool.sh            -> ROOT/vendor/asrtool/build/live-asr
#   ASR_CLEAN=1 ROOT/scripts/build_asrtool.sh  -> build desde cero
#
# Requiere que vendor/transcribe.cpp ya esté compilado (libtranscribe.a +
# libggml*.a en vendor/transcribe.cpp/build). Si falta, lo configura y builda.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
TOOL_DIR="${ROOT}/vendor/asrtool"
TC_DIR="${ROOT}/vendor/transcribe.cpp"
BUILD_DIR="${TOOL_DIR}/build"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 4)}"

log() { printf '[build_asrtool] %s\n' "$*"; }

if [[ ! -f "${TC_DIR}/include/transcribe.h" ]]; then
    log "ERROR: no existe ${TC_DIR}/include/transcribe.h (falta el vendor)"
    exit 1
fi

# 1) Asegurar las libs de transcribe.cpp (estáticas). Si ya están, no se toca nada.
if [[ ! -f "${TC_DIR}/build/src/libtranscribe.a" || ! -f "${TC_DIR}/build/ggml/src/libggml-base.a" ]]; then
    log "compilando vendor transcribe.cpp (primera vez, tarda varios minutos)"
    cmake -S "${TC_DIR}" -B "${TC_DIR}/build" \
        -DCMAKE_BUILD_TYPE=Release \
        -DGGML_NATIVE=ON \
        -DTRANSCRIBE_BUILD_TESTS=OFF
    cmake --build "${TC_DIR}/build" -j "${JOBS}" --target transcribe
fi

# 2) Configurar + compilar live-asr.
if [[ "${ASR_CLEAN:-0}" == "1" ]]; then
    log "limpiando ${BUILD_DIR}"
    rm -rf "${BUILD_DIR}"
fi

cmake -S "${TOOL_DIR}" -B "${BUILD_DIR}" -DCMAKE_BUILD_TYPE=Release
cmake --build "${BUILD_DIR}" -j "${JOBS}" --target live-asr

BIN="${BUILD_DIR}/live-asr"
if [[ ! -x "${BIN}" ]]; then
    log "ERROR: no se encontró el ejecutable ${BIN}"
    exit 1
fi
log "OK: ${BIN}"
