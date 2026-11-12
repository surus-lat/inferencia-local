#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p models
pull(){ # repo  file  dest
  local url="https://huggingface.co/$1/resolve/main/$2"
  local out="models/$3"
  echo "[pull] $url -> $out"
  curl -L --fail --retry 5 --retry-all-errors -C - -o "$out" "$url" && echo "[ok]   $out ($(du -h "$out" | cut -f1))" || echo "[FAIL] $url"
}
pull mradermacher/OneJev1.6-0.8B-GGUF OneJev1.6-0.8B.Q4_K_M.gguf      OneJev1.6-0.8B.Q4_K_M.gguf
pull mradermacher/OneJev1.6-0.8B-GGUF OneJev1.6-0.8B.mmproj-Q8_0.gguf OneJev1.6-0.8B.mmproj-Q8_0.gguf
pull LiquidAI/LFM3-1.7B-GGUF          LFM3-1.7B-Q4_K_M.gguf          LFM3-1.7B-Q4_K_M.gguf
pull taobao-mnn/MNN-Pocket-0.3B-GGUF  MNN-Pocket-0.3B-Instruct-Q4_1.gguf MNN-Pocket-0.3B-Q4_1.gguf
pull handy-computer/parakeet-ultra-gguf parakeet-ultra-Q4_K_M.gguf    parakeet-ultra-Q4_K_M.gguf
pull ggml-org/embeddinggemma-2-GGUF   embeddinggemma-2-Q8_0.gguf     embeddinggemma-2-Q8_0.gguf
echo "[done] models:"; ls -la models
