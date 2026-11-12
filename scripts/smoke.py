#!/usr/bin/env python3
"""Smoke de punta a punta de las 5 demos contra el hub (127.0.0.1:8180).

Carga cada modelo, dispara la demo con un input real, descarga, y reporta latencias.
No toca la cámara ni el micro: usa una imagen y un WAV de prueba.

  python3 scripts/smoke.py                 # todas las demos
  python3 scripts/smoke.py --only yolo     # una
  python3 scripts/smoke.py --keep          # deja los modelos cargados
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("SURUS_HUB", "http://127.0.0.1:8180")


def call(method: str, path: str, body=None, timeout: float = 240.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:                                  # noqa: BLE001
            return e.code, {}
    except Exception as exc:                               # noqa: BLE001
        return 0, {"reason": "conexion", "detail": str(exc)}


def b64(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def load(sid: str) -> tuple[int, dict, float]:
    t = time.time()
    st, r = call("POST", f"/api/models/{sid}/load")
    return st, r, (time.time() - t) * 1000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="dedos|llm|asr|search|yolo")
    ap.add_argument("--keep", action="store_true", help="no descargar los modelos al final")
    args = ap.parse_args()
    want = set(args.only.split(",")) if args.only else {"dedos", "llm", "asr", "search", "yolo"}

    st, health = call("GET", "/api/health", timeout=5)
    if st != 200:
        print(f"✗ el hub no responde en {BASE} (subilo con scripts/run_hub.sh)")
        return 1
    st, s0 = call("GET", "/api/state")
    print(f"hub arriba · RAM disponible {s0['ram']['available_mb']:.0f} MB · "
          f"presupuesto {s0['ram']['budget_mb']} MB")
    loaded: list[str] = []
    fails = 0

    def page(path: str) -> str:
        req = urllib.request.Request(BASE + path)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return f"{r.status} {len(r.read())}B"
        except Exception as exc:                           # noqa: BLE001
            return f"✗ {exc}"

    for d in s0["demos"]:
        print(f"  página {d['path']:9} → {page(d['path'])}")

    if "dedos" in want:
        st, r, ms = load("onejev-vlm")
        if st == 200:
            loaded.append("onejev-vlm")
            print(f"✓ onejev-vlm cargado en {ms:.0f} ms")
            img = b64(os.path.join(ROOT, ".tmp/testimg/circles.png")).replace("\n", "")
            st, r = call("POST", "/api/vision/frame", {"image_b64": img})
            if r.get("ok"):
                print(f"✓ dedos: respuesta={r['answer']!r} prefill={r['ms_prefill']}ms "
                      f"total={r['ms_total']}ms tokens_prompt={r['prompt_tokens']}")
            else:
                fails += 1
                print(f"✗ dedos: {r}")
        else:
            fails += 1
            print(f"✗ onejev-vlm: {r}")

    if "llm" in want:
        st, r, ms = load("mnn-pocket")
        if st == 200:
            loaded.append("mnn-pocket")
            print(f"✓ mnn-pocket cargado en {ms:.0f} ms")
            st, r = call("POST", "/api/llm/chat", {
                "model": "mnn-pocket", "max_tokens": 40, "temperature": 0,
                "messages": [{"role": "user",
                              "content": "Nombrá tres formatos de cuantización."}]})
            if r.get("ok"):
                tt = r.get("timings") or {}
                print(f"✓ llm: {len(r['content'])} chars · "
                      f"prefill {tt.get('prompt_ms', 0):.0f} ms · "
                      f"{tt.get('predicted_per_second', 0):.1f} tok/s")
            else:
                fails += 1
                print(f"✗ llm: {r}")
            # proxy OpenAI-compatible (lo usa pi)
            st, r = call("POST", "/v1/proxy/mnn-pocket/chat/completions", {
                "messages": [{"role": "user", "content": "decí 'listo'"}],
                "max_tokens": 8})
            print(f"  proxy OpenAI → {st} {'ok' if st == 200 else r}")
        else:
            fails += 1
            print(f"✗ mnn-pocket: {r}")

    if "asr" in want:
        st, r, ms = load("parakeet-ultra")
        if st == 200:
            loaded.append("parakeet-ultra")
            print(f"✓ parakeet-ultra cargado en {ms:.0f} ms")
            wav = os.path.join(ROOT, ".tmp/sample_16k.wav")
            if os.path.exists(wav):
                st, r = call("POST", "/api/asr/window",
                             {"audio_b64": b64(wav), "window_start_s": 0.0, "lang": "es"})
                if r.get("ok"):
                    print(f"✓ asr: {r['ms']} ms · {r.get('rtf')}× tiempo real · "
                          f"{len(r.get('words') or [])} palabras")
                    print(f"  texto: {(r.get('text') or '')[:78]}")
                else:
                    fails += 1
                    print(f"✗ asr: {r}")
            else:
                print("… asr: no está .tmp/sample_16k.wav (audio de prueba); se omite")
        else:
            fails += 1
            print(f"✗ parakeet-ultra: {r}")

    if "search" in want:
        st, r, ms = load("embeddinggemma")
        if st == 200:
            loaded.append("embeddinggemma")
            print(f"✓ embeddinggemma cargado en {ms:.0f} ms")
            for scope, etiqueta in ((2946, "1%"), (271980, "100%")):
                st, r = call("POST", "/api/search",
                             {"query": "cambio climático en la Patagonia", "k": 3, "scope": scope})
                if r.get("ok"):
                    top = (r["results"] or [{}])[0]
                    print(f"✓ search {etiqueta}: embed={r['embed_ms']}ms scan={r['scan_ms']}ms "
                          f"sobre {r['scanned_rows']} filas · top: {top.get('title','')[:56]!r} "
                          f"({top.get('score')})")
                else:
                    fails += 1
                    print(f"✗ search {etiqueta}: {r}")
        else:
            fails += 1
            print(f"✗ embeddinggemma: {r}")

    if "yolo" in want:
        st, r, ms = load("yolo")
        if st == 200:
            loaded.append("yolo")
            print(f"✓ yolo cargado en {ms:.0f} ms")
            img = os.path.join(ROOT, ".tmp/bus.jpg")
            if os.path.exists(img):
                st, r = call("POST", "/api/yolo/frame", {"image_b64": b64(img)})
                if r.get("ok"):
                    print(f"✓ yolo: {r['ms']} ms · {len(r['boxes'])} objetos · {r['counts']}")
                else:
                    fails += 1
                    print(f"✗ yolo: {r}")
            else:
                print("… yolo: no está .tmp/bus.jpg; se omite")
        else:
            fails += 1
            print(f"✗ yolo: {r}")

    if not args.keep:
        for sid in loaded:
            call("POST", f"/api/models/{sid}/unload")
        st, s1 = call("GET", "/api/state")
        print(f"descargado todo · RAM modelos ahora {s1['ram']['used_models_mb']:.0f} MB")
    print(f"{'✓ smoke OK' if not fails else f'✗ {fails} fallas'}")
    return 0 if not fails else 2


if __name__ == "__main__":
    sys.exit(main())
