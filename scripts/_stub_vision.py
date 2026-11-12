#!/usr/bin/env python3
"""_stub_vision.py — stub mínimo para validar las dos demos de webcam.

Scratch (prefijo `_`): no es parte del hub. Simula sólo lo que necesitan
/d/dedos y /d/yolo para arrancar sin backends reales:

  GET  /d/dedos, /d/yolo        -> web/pages/dedos.html, web/pages/yolo.html
  GET  /assets/...              -> web/assets/... (css, js, woff2, svg)
  GET  /api/vision/config       -> prompt, image_max_tokens, grammar, fps_cap
  POST /api/vision/frame        -> dígito 0-5 + prefill/decode/tok_per_s falsos
  GET  /api/yolo/config         -> models, default, classes_coco
  POST /api/yolo/frame          -> boxes aleatorios plausibles + ms
  GET  /api/state               -> ram + models (para el chip de estado)
  POST /api/models/{id}/load    -> 200 | 409 {ok:false, reason:"ram"}
  POST /api/models/{id}/unload  -> {ok:true}

El 409 model_not_loaded aparece si el modelo está descargado (--not-loaded o
unload), que es justamente el camino que prueban los botones "Cargar modelo".

Latencia: los ms simulados son los del SPEC (prefill ~2,6 s con imagen nueva,
~0,15 s con imagen repetida por KV cache; YOLO26s ~160 ms). Para no esperar
2,6 s por frame en el browser, se duerme sólo una fracción (--sleep-scale),
pero los números devueltos siguen siendo los del SPEC.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import struct
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")

PAGES = {
    "/d/dedos": os.path.join(WEB, "pages", "dedos.html"),
    "/d/yolo": os.path.join(WEB, "pages", "yolo.html"),
    "/d/dedos.html": os.path.join(WEB, "pages", "dedos.html"),
    "/d/yolo.html": os.path.join(WEB, "pages", "yolo.html"),
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".ico": "image/x-icon",
}

MODELS = {
    "onejev-vlm": {"label": "OneJev 1.6 0.8B VLM", "kind": "vlm", "est_ram_mb": 1150, "port": 8181},
    "yolo": {"label": "YOLO26 (ultralytics)", "kind": "vision", "est_ram_mb": 900, "port": None},
    # vecinos, sólo para que /api/state se parezca al real
    "lfm3-1.7b": {"label": "LFM3 1.7B", "kind": "llm", "est_ram_mb": 1800, "port": 8182},
    "mnn-pocket": {"label": "MNN-Pocket 0.3B", "kind": "llm", "est_ram_mb": 450, "port": 8183},
    "parakeet-ultra": {"label": "Parakeet ultra", "kind": "asr", "est_ram_mb": 900, "port": None},
    "embeddinggemma": {"label": "EmbeddingGemma-2", "kind": "embed", "est_ram_mb": 450, "port": 8185},
}

BUDGET_MB = 4000
FREE_MB = 4800

PROMPT_DEFAULT = "Contá los dedos que se ven en la imagen. Respondé sólo con un dígito entre 0 y 5."

COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "bus", "truck", "traffic light",
    "chair", "couch", "potted plant", "bed", "tv", "laptop", "mouse",
    "remote", "keyboard", "cup", "bottle", "book", "scissors", "backpack", "tie",
]

YOLO_BASE_MS = {"yolo26n": 95.0, "yolo26s": 160.0, "yolo26m": 235.0}

JPEG_SOF = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


class State:
    def __init__(self, not_loaded: bool) -> None:
        # sólo los dos modelos de estas demos arrancan cargados: así queda RAM
        # libre de sobra para probar el botón "Cargar modelo" (y el 409 ram).
        demo = ("onejev-vlm", "yolo")
        self.loaded: dict[str, bool] = {
            mid: (mid in demo and not not_loaded) for mid in MODELS
        }
        self.vision_cfg = {
            "prompt": PROMPT_DEFAULT,
            "image_max_tokens": 96,
            "grammar": "root ::= [0-5]",
            "fps_cap": 2,
        }
        self.yolo_cfg = {
            "models": ["yolo26n", "yolo26s", "yolo26m"],
            "default": "yolo26s",
            "classes_coco": 80,
        }
        self.last_image_key: str | None = None
        self.frames = 0


STATE = State(False)
SLEEP_SCALE = 0.12
QUIET = False


def log(msg: str) -> None:
    if not QUIET:
        print(msg, flush=True)


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Alto/ancho del JPEG, para que los boxes caigan en el espacio real del frame."""
    if not data.startswith(b"\xff\xd8"):
        return None
    i, n = 2, len(data)
    while i < n - 8:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > n:
            return None
        seglen = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in JPEG_SOF:
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return int(w), int(h)
        i += 2 + seglen
    return None


def decode_image(body: dict) -> tuple[bytes, tuple[int, int]]:
    b64 = body.get("image_b64") or ""
    if "," in b64[:64] and b64.startswith("data:"):
        b64 = b64.split(",", 1)[1]
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raw = b""
    return raw, jpeg_size(raw) or (640, 360)


def sleep_for(ms: float) -> None:
    time.sleep(min(float(ms), 4000.0) / 1000.0 * SLEEP_SCALE)


def used_mb() -> int:
    return sum(m["est_ram_mb"] for mid, m in MODELS.items() if STATE.loaded.get(mid))


def model_list() -> list[dict]:
    out = []
    for mid, m in MODELS.items():
        on = STATE.loaded.get(mid)
        out.append(
            {
                "id": mid,
                "label": m["label"],
                "kind": m["kind"],
                "quant": "Q4_K_M",
                "size_mb": m["est_ram_mb"],
                "est_ram_mb": m["est_ram_mb"],
                "port": m["port"],
                "status": "ready" if on else "off",
                "ready_ms": 12000 if on else None,
                "last_error": None,
            }
        )
    return out


def api_state() -> dict:
    used = used_mb()
    return {
        "ok": True,
        "ram": {
            "total_mb": 14336,
            "available_mb": max(0, FREE_MB - used),
            "used_models_mb": used,
            "budget_mb": BUDGET_MB,
        },
        "models": model_list(),
        "demos": [
            {"id": "dedos", "path": "/d/dedos", "title": "Dedos en vivo", "models": ["onejev-vlm"]},
            {"id": "yolo", "path": "/d/yolo", "title": "YOLO con webcam", "models": ["yolo"]},
        ],
    }


def vision_frame(body: dict) -> dict:
    raw, _dims = decode_image(body)
    if not raw:
        return {"ok": False, "reason": "bad_request", "detail": "image_b64 vacío o no es base64"}

    imt = int(body.get("image_max_tokens") or STATE.vision_cfg["image_max_tokens"])
    prompt_tokens = imt + 16

    key = hashlib.sha1(raw).hexdigest()[:16]
    cached = STATE.last_image_key == key
    STATE.last_image_key = key

    if cached:
        ms_prefill = random.uniform(110, 230)
    else:
        ms_prefill = 2600.0 * (imt / 96.0) * random.uniform(0.90, 1.10)

    completion_tokens = random.randint(1, 3)
    ms_decode = completion_tokens * random.uniform(75, 125)
    ms_total = ms_prefill + ms_decode + random.uniform(4, 18)

    sleep_for(ms_total)
    STATE.frames += 1

    answer = str(random.randint(0, 5))
    return {
        "ok": True,
        "answer": answer,
        "raw": "thought: cuento los dedos\nanswer: " + answer,
        "ms_prefill": round(ms_prefill, 1),
        "ms_decode": round(ms_decode, 1),
        "ms_total": round(ms_total, 1),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "tok_per_s": round(completion_tokens * 1000.0 / ms_decode, 2),
        "model": "onejev-vlm",
        "kv_cache_hit": cached,
    }


def yolo_frame(body: dict) -> dict:
    raw, dims = decode_image(body)
    if not raw:
        return {"ok": False, "reason": "bad_request", "detail": "image_b64 vacío o no es base64"}

    model = str(body.get("model") or STATE.yolo_cfg["default"])
    if model not in YOLO_BASE_MS:
        return {"ok": False, "reason": "bad_request", "detail": "modelo desconocido: " + model}
    imgsz = int(body.get("imgsz") or 640)
    conf = float(body.get("conf") if body.get("conf") is not None else 0.35)

    ms = YOLO_BASE_MS[model] * (max(160, min(imgsz, 1280)) / 640.0) ** 1.6 * random.uniform(0.9, 1.15)
    sleep_for(ms)
    STATE.frames += 1

    w, h = dims
    n = random.choices([0, 1, 2, 3, 4], weights=[14, 30, 30, 17, 9])[0]
    boxes = []
    counts: dict[str, int] = {}
    for _ in range(n):
        bw = random.uniform(0.12, 0.5) * w
        bh = random.uniform(0.18, 0.7) * h
        x1 = random.uniform(0, max(1.0, w - bw))
        y1 = random.uniform(0, max(1.0, h - bh))
        cls_name = random.choice(COCO_NAMES)
        c = round(random.uniform(max(conf, 0.28), 0.98), 3)
        boxes.append(
            {
                "cls": COCO_NAMES.index(cls_name),
                "name": cls_name,
                "conf": c,
                "xyxy": [round(x1, 1), round(y1, 1), round(x1 + bw, 1), round(y1 + bh, 1)],
            }
        )
        counts[cls_name] = counts.get(cls_name, 0) + 1

    return {
        "ok": True,
        "ms": round(ms, 1),
        "model": model,
        "imgsz": imgsz,
        "conf": conf,
        "iou": float(body.get("iou") if body.get("iou") is not None else 0.5),
        "frame": {"w": w, "h": h},
        "boxes": boxes,
        "counts": counts,
    }


def load_model(mid: str) -> tuple[int, dict]:
    if mid not in MODELS:
        return 404, {"ok": False, "reason": "not_found", "model": mid}
    need = MODELS[mid]["est_ram_mb"]
    free = max(0, FREE_MB - used_mb())
    if STATE.loaded[mid]:
        return 200, {"ok": True, "model": dict(MODELS[mid], id=mid, status="ready"), "note": "ya estaba cargado"}
    if need > free:
        return 409, {"ok": False, "reason": "ram", "need_mb": need, "free_mb": free, "model": mid}
    time.sleep(0.35)
    STATE.loaded[mid] = True
    return 200, {"ok": True, "model": dict(MODELS[mid], id=mid, status="ready", ready_ms=12000)}


def unload_model(mid: str) -> tuple[int, dict]:
    if mid not in MODELS:
        return 404, {"ok": False, "reason": "not_found", "model": mid}
    STATE.loaded[mid] = False
    return 200, {"ok": True, "model": dict(MODELS[mid], id=mid, status="off")}


def not_loaded(mid: str) -> tuple[int, dict]:
    return 409, {"ok": False, "reason": "model_not_loaded", "model": mid}


MODEL_RE = re.compile(r"^/api/models/([^/]+)/(load|unload)$")


class Handler(BaseHTTPRequestHandler):
    server_version = "StubVision/1.0"

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self._last_status = status
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _static(self, path: str) -> None:
        target = None
        if path in PAGES:
            target = PAGES[path]
        elif path.startswith("/assets/"):
            rel = unquote(path[len("/assets/"):]).lstrip("/")
            cand = os.path.normpath(os.path.join(WEB, "assets", rel))
            inside = os.path.commonpath([WEB, cand]) == WEB if cand != WEB else False
            if inside and os.path.isfile(cand):
                target = cand
        elif path == "/":
            target = os.path.join(WEB, "pages", "launcher.html")

        if not target or not os.path.isfile(target):
            self._json(404, {"ok": False, "reason": "not_found", "path": path})
            return
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as fh:
            data = fh.read()
        self._send(200, data, CONTENT_TYPES.get(ext, "application/octet-stream"))

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json(200, {"ok": True, "ts": round(time.time(), 3)})
        elif path == "/api/state":
            self._json(200, api_state())
        elif path == "/api/vision/config":
            self._json(200, dict(STATE.vision_cfg, ok=True))
        elif path == "/api/yolo/config":
            self._json(200, dict(STATE.yolo_cfg, ok=True))
        elif path.startswith("/api/"):
            self._json(404, {"ok": False, "reason": "not_found", "path": path})
        else:
            self._static(path)
        log(f"GET  {path} -> {self._status()}")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _status(self) -> int:
        return getattr(self, "_last_status", 200)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self._read_body()
        status = 200

        if path == "/api/vision/frame":
            if not STATE.loaded["onejev-vlm"]:
                status, payload = not_loaded("onejev-vlm")
            else:
                payload = vision_frame(body)
                status = 200 if payload.get("ok") else 400
        elif path == "/api/yolo/frame":
            if not STATE.loaded["yolo"]:
                status, payload = not_loaded("yolo")
            else:
                payload = yolo_frame(body)
                status = 200 if payload.get("ok") else 400
        elif MODEL_RE.match(path):
            m = MODEL_RE.match(path)
            mid, action = unquote(m.group(1)), m.group(2)
            status, payload = load_model(mid) if action == "load" else unload_model(mid)
        else:
            status, payload = 404, {"ok": False, "reason": "not_found", "path": path}

        self._last_status = status
        self._json(status, payload)
        log(f"POST {path} -> {status} {brief(payload)}")


def brief(payload: dict) -> str:
    if not payload.get("ok"):
        return "{" + str(payload.get("reason")) + "}"
    for k in ("answer", "ms", "model"):
        if k in payload:
            return "{" + k + "=" + str(payload[k]) + "}"
    return "{" + str(len(payload)) + " keys}"


def main() -> int:
    global SLEEP_SCALE, QUIET, STATE

    ap = argparse.ArgumentParser(description="Stub de /d/dedos y /d/yolo (scratch).")
    ap.add_argument("--port", type=int, default=8193)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--not-loaded", action="store_true",
                    help="arranca con onejev-vlm y yolo descargados (para probar el 409 model_not_loaded)")
    ap.add_argument("--sleep-scale", type=float, default=0.12,
                    help="fracción de la latencia simulada que se duerme de verdad (0 apaga la espera)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    SLEEP_SCALE = max(0.0, args.sleep_scale)
    QUIET = args.quiet
    STATE = State(args.not_loaded)

    random.seed(20261112)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"stub vision en http://{args.host}:{args.port}  "
          f"(dedos={os.path.isfile(PAGES['/d/dedos'])}, yolo={os.path.isfile(PAGES['/d/yolo'])}) "
          f"modelos_cargados={[m for m, on in STATE.loaded.items() if on]}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
