"""Hub de demos de inferencia local · SURUS.

Un solo proceso (127.0.0.1:8180) sirve las 5 webapps y todas las APIs.
Los backends (llama-server, worker ASR, ultralytics) los administra BackendManager.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, Optional

# Debe estar ANTES de importar numpy: este build de OpenBLAS lee las variables de
# entorno al cargar. Con 8 hyperthreads el scan del 1% cuesta ~7 ms; con 2, ~0,7 ms.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("OMP_NUM_THREADS", "2")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backends import DEMOS, REGISTRY, ROOT, BackendManager, _p

app = FastAPI(title="Inferencia local · SURUS")
MGR = BackendManager()

PAGES = {"dedos": "dedos.html", "llm": "llm.html", "asr": "asr.html",
         "search": "search.html", "yolo": "yolo.html"}

VISION_PROMPT_DEFAULT = (
    "Contá los dedos levantados en la mano de la foto. "
    "Si no hay mano o no se distingue, respondé 0."
)
VISION_GRAMMAR = "root ::= [0-5]"


def err(status: int, reason: str, **kw) -> JSONResponse:
    return JSONResponse({"ok": False, "reason": reason, **kw}, status_code=status)


def need(sid: str) -> Optional[JSONResponse]:
    if MGR.is_ready(sid):
        return None
    spec = REGISTRY[sid]
    return err(409, "model_not_loaded", model=sid, label=spec.label,
               est_ram_mb=spec.est_ram_mb,
               detail=f"{spec.label} no está cargado (≈{spec.est_ram_mb} MB).")


def b64_to_file(data_b64: str, suffix: str = ".jpg") -> str:
    raw = base64.b64decode(data_b64.split(",", 1)[-1])
    fd, path = tempfile.mkstemp(suffix=suffix, dir=os.path.join(ROOT, ".tmp"))
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
    return path


# ------------------------------------------------------------------ estado ---
@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "ts": int(time.time())}


@app.get("/api/state")
def state() -> Dict[str, Any]:
    return MGR.state()


FILES = {
    "dedos": ["models/OneJev1.6-0.8B.Q4_K_M.gguf", "models/OneJev1.6-0.8B.mmproj-Q8_0.gguf",
              "vendor/llama.cpp/build/bin/llama-server", "hub/server.py", "hub/backends.py",
              "web/pages/dedos.html", "web/assets/pages/vision.js"],
    "llm": ["models/LFM3-1.7B-Q4_K_M.gguf", "models/MNN-Pocket-0.3B-Q4_1.gguf",
            "vendor/llama.cpp/build/bin/llama-server", "scripts/pi_setup.py",
            "web/pages/llm.html", "web/assets/pages/text.js"],
    "asr": ["models/parakeet-ultra-Q4_K_M.gguf", "vendor/asrtool/build/live-asr",
            "vendor/transcribe.cpp/include/transcribe.h", "hub/asr_worker.py",
            "web/pages/asr.html", "web/assets/pages/asr.js"],
    "search": ["models/embeddinggemma-2-Q8_0.gguf", "models/mmproj-embeddinggemma-2-Q8_0.gguf",
               "data/metadata.parquet", "data/index_full/vectors.f32.bin",
               "hub/search_index.py", "scripts/build_index.py",
               "web/pages/search.html", "web/assets/pages/text.js"],
    "yolo": ["yolo26n.pt", "yolo26s.pt", "yolo26m.pt", "hub/server.py",
             "web/pages/yolo.html", "web/assets/pages/vision.js"],
}


def _file_info(rel: str) -> Dict[str, Any]:
    p = _p(*rel.split("/"))
    try:
        return {"path": rel, "mb": round(os.path.getsize(p) / 1e6, 1), "exists": True}
    except OSError:
        return {"path": rel, "mb": None, "exists": False}


def _versions() -> Dict[str, Any]:
    p = _p("hub", "versions.json")
    try:
        with open(p) as fh:
            return json.load(fh)
    except OSError:
        return {"cpu": {}, "llama_cpp": {}, "transcribe_cpp": {}, "packages": {}}


@app.get("/api/stack")
def stack() -> Dict[str, Any]:
    """El stack de cada demo, del metal a la app, con el comando real de servicio."""
    v = _versions()
    cpu = v.get("cpu") or {}
    lc = v.get("llama_cpp") or {}
    tc = v.get("transcribe_cpp") or {}
    pk = v.get("packages") or {}
    st = MGR.state()
    by_id = {m["id"]: m for m in st["models"]}
    cmd = {k: (by_id.get(k, {}).get("metrics") or {}).get("cmd") or "" for k in by_id}

    ram = st.get("ram") or {}
    metal = [
        f"{cpu.get('name', 'CPU de esta máquina')} · {cpu.get('cores') or st['cpu']['cores']} "
        f"hilos lógicos · AVX2 {'sí' if cpu.get('avx2') else 'no'}",
        f"{cpu.get('mem_total_mb') or ram.get('total_mb') or '?'} MB de RAM · {cpu.get('gpu', 'sin GPU')}",
        "Sin aceleración por GPU: todo el cómputo va al CPU (ggml / libtorch CPU).",
    ]
    if not cpu:
        metal[0] += "  ·  corré scripts/freeze_versions.py para el detalle"
    app_layer = [
        "FastAPI + uvicorn (mismo origen, sin CORS) · hub en 127.0.0.1:8180",
        "Páginas HTML + JS vanilla sin build · cámara y micro con getUserMedia",
        "Marca SURUS: tokens.css local, Inter light + JetBrains Mono",
    ]
    common = {
        "app": app_layer,
        "metal": metal,
    }

    def layers(engine: list) -> Dict[str, Any]:
        return {**common, "engine": engine}

    out = {
        "ok": True,
        "generated_at": v.get("generated_at"),
        "versions": {
            "llama_cpp": f"{lc.get('version', '?')} (build {lc.get('build', '?')}, "
                         f"commit {lc.get('commit', '?')})",
            "transcribe_cpp": f"{tc.get('commit', '?')} ({tc.get('date', '?')})",
            "torch": pk.get("torch"), "ultralytics": pk.get("ultralytics"),
            "fastapi": pk.get("fastapi"), "uvicorn": pk.get("uvicorn"),
            "numpy": pk.get("numpy"), "pyarrow": pk.get("pyarrow"),
            "python": v.get("python"),
        },
        "layers_order": ["metal", "kernels", "engine", "app"],
        # metal y app son idénticas para las 5 demos: se comparten una sola vez
        "common": {"metal": metal, "app": app_layer},
        "demos": {
            "dedos": {
                "title": "Dedos en vivo",
                "models": ["onejev-vlm"],
                "serve_cmd": cmd.get("onejev-vlm"),
                "kernels": ["ggml compilado con GGML_NATIVE (-march=native), sin backend CUDA",
                            "mtmd: vision encoder + proyector mmproj Q8_0",
                            "KV cache en RAM del sistema (f16)"],
                "engine": ["llama.cpp (llama-server) con --mmproj y --image-max-tokens 96",
                           "OneJev 1.6 · 0.8B Q4_K_M, respuesta forzada con GBNF `root ::= [0-5]`"],
            },
            "llm": {
                "title": "LLM servido local",
                "models": ["lfm3-1.7b", "mnn-pocket"],
                "serve_cmd": cmd.get("mnn-pocket"),
                "kernels": ["ggml nativo, CPU-only · hilos: " + str(st["cpu"]["threads"]),
                            "KV cache 8.192 tokens por modelo (DSH_CTX_LLM)"],
                "engine": ["llama.cpp con servidor compatible OpenAI (/v1/chat/completions)",
                           "LFM3 1.7B Q4_K_M (thinking) · MNN-Pocket 0.3B Q4_1",
                           "proxy del hub /v1/proxy/<id> → lo consume pi"],
            },
            "asr": {
                "title": "Transcripción en vivo",
                "models": ["parakeet-ultra"],
                "serve_cmd": cmd.get("parakeet-ultra"),
                "kernels": ["ggml del vendor de transcribe.cpp (GGML_NATIVE)",
                            "frontend mel 16 kHz · FastConformer + cabeza TDT"],
                "engine": ["transcribe.cpp vía worker propio (live-asr, stdin/stdout JSON)",
                           "Parakeet Ultra 0.6B Q4_K_M · offline por ventanas (VAD en el navegador)"],
            },
            "search": {
                "title": "Búsqueda semántica",
                "models": ["embeddinggemma"],
                "serve_cmd": cmd.get("embeddinggemma"),
                "kernels": ["ggml nativo · OpenBLAS a 2 hilos (ganan al default de 8 en este scan)",
                            "matriz float32 leída con mmap (835 MB en disco)"],
                "engine": ["llama.cpp --embedding --pooling mean (+ mmproj para buscar con fotos)",
                           "EmbeddingGemma-2 Q8_0, 768 dims, L2-normalizado",
                           "corpus UNRN/ri-conicet: 271.980 vectores ya calculados"],
            },
            "yolo": {
                "title": "Webcam con YOLO26",
                "models": ["yolo"],
                "serve_cmd": cmd.get("yolo"),
                "kernels": ["libtorch CPU con torch.set_num_threads(4)",
                            "torchvision para NMS · tensor 1×3×640×640"],
                "engine": ["ultralytics en el mismo proceso del hub (sin servidor aparte)",
                           "YOLO26 n / s / m · 80 clases COCO"],
            },
        },
        "runtime_now": {
            "ram": st["ram"], "cpu": st["cpu"],
            "models": [{"id": m["id"], "status": m["status"], "rss_mb": (m["metrics"] or {}).get("rss_mb"),
                        "serve_cmd": (m["metrics"] or {}).get("cmd") or ""} for m in st["models"]],
        },
    }
    for did, d in out["demos"].items():
        d["files"] = [_file_info(f) for f in FILES.get(did, [])]
    return out


@app.post("/api/models/{sid}/load")
def model_load(sid: str) -> JSONResponse:
    if sid not in REGISTRY:
        return err(404, "bad_request", detail=f"modelo desconocido: {sid}")
    try:
        r = MGR.load(sid)
    except Exception as exc:                       # noqa: BLE001
        return err(500, "backend_down", detail=str(exc), model=sid)
    return JSONResponse(r if r.get("ok") else err_body(r), status_code=200 if r.get("ok") else 409)


@app.post("/api/models/{sid}/unload")
def model_unload(sid: str) -> JSONResponse:
    if sid not in REGISTRY:
        return err(404, "bad_request")
    return JSONResponse(MGR.unload(sid))


def err_body(r: Dict[str, Any]) -> Dict[str, Any]:
    return {**r, "detail": f"No hay lugar en el presupuesto de RAM "
                           f"(faltan {abs(r.get('free_mb', 0)):.0f} MB). Descargá algún modelo."}


@app.get("/api/log/{sid}")
def model_log(sid: str, n: int = 200) -> Dict[str, Any]:
    return {"lines": MGR.tail_log(sid, n)}


# ------------------------------------------------------------- demo 1 · VLM ---
@app.get("/api/vision/config")
def vision_config() -> Dict[str, Any]:
    return {"prompt": VISION_PROMPT_DEFAULT, "image_max_tokens": 96,
            "grammar": VISION_GRAMMAR, "fps_cap": 1, "model": "onejev-vlm"}


@app.post("/api/vision/frame")
async def vision_frame(req: Request) -> JSONResponse:
    body = await req.json()
    bad = need("onejev-vlm")
    if bad:
        return bad
    if not body.get("image_b64"):
        return err(400, "bad_request", detail="falta image_b64")
    data_url = "data:image/jpeg;base64," + body["image_b64"].split(",", 1)[-1]
    prompt = body.get("prompt") or VISION_PROMPT_DEFAULT
    payload = {
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": prompt}]}],
        "max_tokens": int(body.get("max_tokens", 8)),
        "temperature": float(body.get("temperature", 0.0)),
        "grammar": body.get("grammar", VISION_GRAMMAR),
    }
    try:
        d = MGR.llama["onejev-vlm"].post("/v1/chat/completions", payload, timeout=120)
    except Exception as exc:                       # noqa: BLE001
        return err(502, "backend_down", detail=str(exc))
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    answer = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    t = d.get("timings") or {}
    return JSONResponse({
        "ok": True, "answer": answer[:8], "raw": (answer or ""),
        "ms_prefill": int(t.get("prompt_ms") or 0), "ms_decode": int(t.get("predicted_ms") or 0),
        "ms_total": int((t.get("prompt_ms") or 0) + (t.get("predicted_ms") or 0)),
        "prompt_tokens": (d.get("usage") or {}).get("prompt_tokens"),
        "completion_tokens": (d.get("usage") or {}).get("completion_tokens"),
        "tok_per_s": round(t.get("predicted_per_second") or 0, 1),
    })


# ------------------------------------------------------------- demo 2 · LLM ---
def _llm_backend(model: str):
    return MGR.llama.get(model if model in MGR.llama else "lfm3-1.7b")


@app.get("/api/llm/config")
def llm_config() -> Dict[str, Any]:
    base = "http://127.0.0.1:8180"
    return {
        "models": [REGISTRY["lfm3-1.7b"].to_public(), REGISTRY["mnn-pocket"].to_public()],
        "pi": {
            "cmd": "pi --provider surus-local --model lfm3-1.7b --no-tools",
            "base_url": f"{base}/v1",
            "provider": "surus-local",
            "models_json": {
                "surus-local": {
                    "baseUrl": f"{base}/v1/proxy/mnn-pocket",
                    "api": "openai-completions",
                    "apiKey": "local",
                    "models": [{"id": "mnn-pocket", "input": ["text"]},
                               {"id": "lfm3-1.7b", "input": ["text"]}],
                }
            },
            "file": "~/.pi/agent/models.json",
            "setup": "python3 scripts/pi_setup.py",
            "notes": "Un solo endpoint compatible OpenAI (POST /v1/chat/completions) que "
                     "elige el backend según el campo model y carga el modelo si entra en el "
                     "presupuesto de RAM. pi habla con tu máquina, no con la nube. "
                     "Pasá --no-tools a pi (su system prompt con tools usa ~10k tokens) o subí "
                     "DSH_CTX_LLM=16384.",
        },
    }


@app.post("/api/llm/chat")
async def llm_chat(req: Request) -> JSONResponse:
    body = await req.json()
    model = body.get("model") or "lfm3-1.7b"
    if model not in ("lfm3-1.7b", "mnn-pocket"):
        return err(400, "bad_request", detail=f"modelo inválido: {model}")
    bad = need(model)
    if bad:
        return bad
    payload = {
        "messages": body.get("messages") or [],
        "max_tokens": int(body.get("max_tokens", 256)),
        "temperature": float(body.get("temperature", 0.6)),
    }
    if model == "lfm3-1.7b" and body.get("thinking") is False:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        d = MGR.llama[model].post("/v1/chat/completions", payload, timeout=240)
    except Exception as exc:                       # noqa: BLE001
        return err(502, "backend_down", detail=str(exc))
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    return JSONResponse({
        "ok": True, "model": model,
        "content": msg.get("content") or "",
        "reasoning": msg.get("reasoning_content") or "",
        "usage": d.get("usage") or {}, "timings": d.get("timings") or {},
    })


_ALIAS: Dict[str, str] = {}


def _alias(model: str) -> Optional[str]:
    """llama-server rechaza cualquier `model` que no sea su alias; se lo forzamos."""
    if model not in _ALIAS:
        try:
            with __import__("urllib").request.urlopen(
                    f"http://127.0.0.1:{MGR.llama[model].spec.port}/props", timeout=3) as r:
                _ALIAS[model] = json.loads(r.read().decode()).get("model_alias") or model
        except Exception:                              # noqa: BLE001
            return None
    return _ALIAS[model]


# Parámetros que los clientes OpenAI modernos mandan y llama.cpp no acepta.
_OPENAI_DROP = {"max_completion_tokens", "store", "stream_options", "service_tier",
                "metadata", "user", "reasoning_effort", "verbosity"}


def _normalize_openai(payload: Dict[str, Any], alias: Optional[str]) -> Dict[str, Any]:
    if alias:
        payload["model"] = alias
    if "max_tokens" not in payload and "max_completion_tokens" in payload:
        payload["max_tokens"] = payload["max_completion_tokens"]
    for k in list(payload):
        if k in _OPENAI_DROP:
            payload.pop(k)
    if not payload.get("max_tokens"):
        payload["max_tokens"] = 512
    return payload


def _sse_passthrough(model: str, payload: Dict[str, Any]):
    """Reenvía el stream SSE de llama-server tal cual (pi pide stream:true)."""
    import urllib.request
    from fastapi.responses import StreamingResponse

    port = MGR.llama[model].spec.port
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"})

    def gen():
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                for line in r:
                    yield line
        except __import__("urllib").error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode()[:400]
            except Exception:                      # noqa: BLE001
                pass
            with open(os.path.join(ROOT, "logs", "sse-error.log"), "w") as fh:
                fh.write(f"{exc.code} {body}\npayload: " + json.dumps(payload, ensure_ascii=False))
            yield ("data: " + json.dumps({"error": f"llama-server {exc.code}",
                                          "detail": body}) + "\n\n").encode()
        except Exception as exc:                   # noqa: BLE001
            yield ("data: " + json.dumps({"error": "el backend cerro el stream",
                                          "detail": str(exc)}) + "\n\n").encode()

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/v1/proxy/{model}/chat/completions")
@app.post("/v1/proxy/{model}/v1/chat/completions")
async def openai_proxy(model: str, req: Request) -> Any:
    if model not in MGR.llama:
        return err(404, "bad_request", detail=f"modelo desconocido: {model}")
    bad = need(model)
    if bad:
        return bad
    raw = await req.body()
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return err(400, "bad_request", detail="body no es JSON")
    alias = _alias(model)
    payload = _normalize_openai(payload, alias)
    if payload.get("stream"):
        return _sse_passthrough(model, payload)
    try:
        path = "/v1/chat/completions"
        out = MGR.llama[model].post(path, payload, timeout=240)
    except __import__("urllib").error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()[:600]
        except Exception:                          # noqa: BLE001
            pass
        with open(os.path.join(ROOT, "logs", "pi-last-body.json"), "w") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
        return err(502, "backend_down", detail=f"llama-server {exc.code}: {body}",
                   dumped="logs/pi-last-body.json")
    except Exception as exc:                       # noqa: BLE001
        return err(502, "backend_down", detail=str(exc))
    return JSONResponse(out)


def _dispatch_target(payload: Dict[str, Any]) -> Optional[str]:
    """Elige el backend según `model`; si no está cargado, lo carga bajo presupuesto."""
    want = (payload.get("model") or "").strip()
    cands = [k for k in MGR.llama if REGISTRY[k].kind == "llm"]
    target = None
    for k in cands:
        if want and (want == k or want == (REGISTRY[k].alias or k)
                     or want.lower() in (REGISTRY[k].label or "").lower()):
            target = k
            break
    if target is None:                       # el cliente devolvió la ruta del peso u otra cosa
        ready = [k for k in cands if MGR.is_ready(k)]
        if len(ready) == 1:
            target = ready[0]
    if target is None:
        return None
    if not MGR.is_ready(target):
        try:
            r = MGR.load(target)             # carga automática: es lo que espera quien usa pi
        except Exception:                    # noqa: BLE001
            return None
        if not r.get("ok"):
            raise OverRam(r)
    return target


class OverRam(Exception):
    def __init__(self, info: Dict[str, Any]):
        self.info = info


@app.post("/v1/chat/completions")
@app.post("/v1/completions")
async def openai_dispatch(req: Request) -> Any:
    """Proxy único model-aware: `pi --provider surus-local --model lfm3-1.7b` funciona solo."""
    raw = await req.body()
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return err(400, "bad_request", detail="body no es JSON")
    try:
        target = _dispatch_target(payload)
    except OverRam as e:
        return JSONResponse({**e.info, "detail": f"No entra {payload.get('model')} en el "
                                                 f"presupuesto de RAM ({e.info.get('need_mb')} MB "
                                                 f"que faltan). Descargá otro modelo o subí "
                                                 f"DSH_RAM_BUDGET_MB."}, status_code=409)
    if target is None:
        return err(404, "bad_request", detail="modelo desconocido o no cargado",
                   disponibles=[REGISTRY[k].alias or k for k in MGR.llama
                                if REGISTRY[k].kind == "llm"])
    payload = _normalize_openai(payload, _alias(target))
    if payload.get("stream"):
        return _sse_passthrough(target, payload)
    try:
        path = "/v1/chat/completions" if req.url.path.endswith("chat/completions") else "/completion"
        out = MGR.llama[target].post(path, payload, timeout=240)
    except Exception as exc:                   # noqa: BLE001
        return err(502, "backend_down", detail=str(exc))
    return JSONResponse(out)


@app.get("/v1/models")
def openai_models() -> Dict[str, Any]:
    data = []
    for k in MGR.llama:
        if REGISTRY[k].kind != "llm":
            continue
        data.append({"id": REGISTRY[k].alias or k, "object": "model",
                     "owned_by": "surus-local", "loaded": MGR.is_ready(k),
                     "quant": REGISTRY[k].quant, "params": REGISTRY[k].params})
    return {"object": "list", "data": data}


@app.get("/v1/proxy/{model}/models")
def openai_proxy_models(model: str) -> JSONResponse:
    if model not in MGR.llama:
        return err(404, "bad_request", detail=f"modelo desconocido: {model}")
    alias = _alias(model) or model
    return JSONResponse({"object": "list",
                         "data": [{"id": alias, "object": "model", "owned_by": "surus-local"}]})


# ------------------------------------------------------------- demo 3 · ASR ---
@app.get("/api/asr/config")
def asr_config() -> Dict[str, Any]:
    return {"model": "parakeet-ultra-Q4_K_M", "rtf_ref": 7.0, "chunk_s": [2, 8],
            "languages": "25 idiomas (es, en, de, fr, it, pt, ...)", "engine": "transcribe.cpp"}


@app.post("/api/asr/window")
async def asr_window(req: Request) -> JSONResponse:
    body = await req.json()
    bad = need("parakeet-ultra")
    if bad:
        return bad
    if not body.get("audio_b64"):
        return err(400, "bad_request", detail="falta audio_b64")
    wav = base64.b64decode(body["audio_b64"].split(",", 1)[-1])
    start = float(body.get("window_start_s") or 0.0)
    try:
        r = MGR.asr.window(wav, lang=body.get("lang") or "es")
    except Exception as exc:                       # noqa: BLE001
        return err(502, "backend_down", detail=str(exc))
    r.setdefault("ok", True)
    r["window_start_s"] = start
    # Los timestamps del worker son relativos al inicio de la ventana; los trasladamos
    # a la línea de tiempo acumulada del micrófono.
    for w in r.get("words") or []:
        for k in ("t0", "t1"):
            if isinstance(w.get(k), (int, float)):
                w[k] = round(w[k] + start, 3)
    return JSONResponse(r)


# ----------------------------------------------------------- demo 4 · search ---
_SEARCH = {"idx": None}


def _search_index():
    """Índice completo (271.980 vectores, mmap). data/index es prefijo exacto."""
    if _SEARCH["idx"] is None:
        from search_index import SearchIndex          # scope del compañero search-index
        idx = SearchIndex(_p("data", "index_full"))
        idx.load()
        _SEARCH["idx"] = idx
    return _SEARCH["idx"]


def _pdf_url(rel: Optional[str]) -> Optional[str]:
    if not rel:
        return None
    return ("https://huggingface.co/datasets/UNRN/ri-conicet/resolve/main/" + rel
            if rel.startswith("papers/") else rel)


@app.get("/api/search/config")
def search_config() -> Dict[str, Any]:
    try:
        cfg = _search_index().config()
    except Exception as exc:                         # noqa: BLE001
        cfg = {"error": str(exc), "rows_total": 294595, "indexed_rows": 0, "dims": 768,
               "model": "embeddinggemma-2-Q8_0", "corpus": "UNRN/ri-conicet",
               "scopes": {"p1": 2946, "p5": 14730, "p25": 73649, "full": 294595}}
    cfg.setdefault("corpus", "UNRN/ri-conicet")
    return cfg


@app.post("/api/search")
async def search(req: Request) -> JSONResponse:
    body = await req.json()
    bad = need("embeddinggemma")
    if bad:
        return bad
    q = (body.get("query") or "").strip()
    img = (body.get("image_b64") or "").split(",", 1)[-1].strip()
    if not q and not img:
        return err(400, "bad_request", detail="mandá query o image_b64")
    k = max(1, min(int(body.get("k", 10)), 50))
    scan = int(body.get("scope") or 2946)
    idx = _search_index()
    t0 = time.time()
    # Con imagen, el vector sale atravesando el proyector de visión (mmproj): la
    # comparación con los resúmenes escritos es cross-modal y el score esperado es menor.
    if img:
        mime = "image/png" if img.startswith("iVBOR") else "image/jpeg"
        payload = {"input": [{"content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img}"}}]}]}
        mode = "imagen"
    else:
        payload = {"input": [q]}
        mode = "texto"
    try:
        d = MGR.llama["embeddinggemma"].post("/v1/embeddings", payload, timeout=300)
        vec = d["data"][0]["embedding"]
    except Exception as exc:                         # noqa: BLE001
        return err(502, "backend_down", detail=str(exc))
    n = sum(x * x for x in vec) ** 0.5
    if n:
        vec = [x / n for x in vec]        # el índice está L2-normalizado: coseno = producto punto
    embed_ms = int((time.time() - t0) * 1000)
    try:
        res = idx.search(vec, k=k, scan_rows=scan)
    except Exception as exc:                         # noqa: BLE001
        return err(500, "backend_down", detail=f"índice: {exc}")
    scan_ms = int(idx.last_scan_ms)
    for r in res:
        r["handle"] = f"http://hdl.handle.net/11336/{r['item_id']}"
        r["pdf_url"] = _pdf_url(r.get("pdf"))
    return JSONResponse({"ok": True, "mode": mode, "query": q or None,
                         "took_ms": embed_ms + scan_ms, "embed_ms": embed_ms,
                         "scan_ms": scan_ms, "scanned_rows": min(scan, idx.n), "dims": len(vec),
                         "k": k, "results": res})


# ------------------------------------------------------------- demo 5 · YOLO --
@app.get("/api/yolo/config")
def yolo_config() -> Dict[str, Any]:
    return {"models": ["yolo26n", "yolo26s", "yolo26m"], "default": MGR.yolo_name,
            "classes_coco": 80, "imgsz": [320, 480, 640], "ref_ms": 160}


@app.post("/api/yolo/frame")
async def yolo_frame(req: Request) -> JSONResponse:
    body = await req.json()
    bad = need("yolo")
    if bad:
        return bad
    if not body.get("image_b64"):
        return err(400, "bad_request", detail="falta image_b64")
    model = body.get("model") or MGR.yolo_name
    if model not in ("yolo26n", "yolo26s", "yolo26m"):
        return err(400, "bad_request", detail=f"modelo inválido: {model}")
    path = b64_to_file(body["image_b64"], ".jpg")
    try:
        r = MGR.yolo_frame(path, float(body.get("conf", 0.35)), float(body.get("iou", 0.5)),
                           int(body.get("imgsz", 640)), model)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if r is None:
        return err(409, **MGR.over_ram("yolo"))
    ms, boxes = r
    counts: Dict[str, int] = {}
    for b in boxes:
        counts[b["name"]] = counts.get(b["name"], 0) + 1
    return JSONResponse({"ok": True, "ms": ms, "model": model, "imgsz": int(body.get("imgsz", 640)),
                         "boxes": boxes, "counts": counts})


# -------------------------------------------------------------- estáticos ----
@app.get("/")
def launcher():
    path = _p("web", "pages", "launcher.html")
    if not os.path.exists(path):
        return err(503, "bad_request", detail="falta launcher.html (lo están construyendo ahora)")
    return FileResponse(path)


@app.get("/d/{demo}")
def demo_page(demo: str):
    f = PAGES.get(demo)
    if not f:
        return err(404, "bad_request", detail=f"demo desconocida: {demo}")
    path = _p("web", "pages", f)
    if not os.path.exists(path):
        return err(503, "bad_request", detail=f"falta {f} (la están construyendo ahora)")
    return FileResponse(path)


app.mount("/assets", StaticFiles(directory=_p("web", "assets"), html=False), name="assets")


@app.get("/favicon.svg")
def favicon():
    return FileResponse(_p("web", "assets", "surus", "surus-symbol.svg"), media_type="image/svg+xml")
