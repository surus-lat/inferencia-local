#!/usr/bin/env python3
"""_stub_asr.py — scratch (prefijo _) para desarrollar /d/asr sin el hub real.

Sirve las páginas de web/ y simula la API de la Demo 3 en 127.0.0.1:8194:

  GET  /                        -> web/pages/asr.html (al principio del path)
  GET  /d/asr                   -> web/pages/asr.html
  GET  /d/<x>                   -> web/pages/<x>.html
  GET  /assets/...              -> web/assets/...
  GET  /api/asr/config          -> {model, rtf_ref, chunk_s}
  GET  /api/health              -> {ok, ts}
  POST /api/asr/window          -> {ok, text, words, ms, rtf, lang}  (latencia ~250 ms)
  POST /api/models/parakeet-ultra/load | /unload

Arranca con parakeet-ultra "descargado" para que la página ejercite el
409 model_not_loaded + botón "Cargar modelo".

Uso:  python3 scripts/_stub_asr.py [puerto]
"""

import base64
import json
import math
import os
import random
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8194

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
}

LOCK = threading.Lock()
STATE = {"loaded": False}

# Fragmentos en español, coherentes con el tema del curso.
CORPUS = [
    "la inferencia local corre en esta laptop sin gpu",
    "parakeet-ultra transcurre la ventana en cpu a siete veces tiempo real",
    "el navegador detecta el silencio y corta la frase",
    "cada ventana viaja como wav de dieciséis kilohertz al hub",
    "el presupuesto de ram limita cuántos modelos cargamos a la vez",
    "las palabras de baja confianza quedan marcadas en el transcripto",
    "esto es detección, corte, chunk y texto",
    "los timestamps y la probabilidad salen del propio modelo",
    "el efecto en vivo no lo da el modelo sino el ciclo de ventanas",
    "mandá la voz y mirá cómo se acumula el texto confirmado",
]
corpus_i = 0


def synth_words(dur_s, lang):
    """Words plausibles: tiempos proporcionales a la longitud, p creíble."""
    global corpus_i
    with LOCK:
        frag = CORPUS[corpus_i % len(CORPUS)]
        corpus_i += 1
    if lang == "en":
        frag = "local inference runs on this laptop without a gpu"
    raw = frag.split()
    total_chars = sum(len(w) for w in raw) or 1
    # repartir la duración dejando un margen de silencio por palabra
    usable = max(0.3, dur_s * 0.86)
    scale = usable / total_chars
    t = max(0.05, (dur_s - usable) * 0.4)
    words = []
    for w in raw:
        wd = max(0.09, len(w) * scale * random.uniform(0.85, 1.15))
        p = random.uniform(0.72, 0.98)
        if random.random() < 0.16:  # algunas palabras dudosas, a propósito
            p = random.uniform(0.40, 0.59)
        words.append({
            "t0": round(t, 3),
            "t1": round(t + wd, 3),
            "word": w,
            "p": round(p, 3),
        })
        t += wd + random.uniform(0.01, 0.05)
    return frag, words


def wav_duration_s(data):
    """Duración desde el header RIFF (falls back al tamaño bruto)."""
    if len(data) >= 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        rate = struct.unpack_from("<I", data, 24)[0] or 16000
        bps = struct.unpack_from("<H", data, 34)[0] or 16
        ch = struct.unpack_from("<H", data, 22)[0] or 1
        nbytes = len(data) - 44
        return max(0.05, nbytes / (rate * ch * (bps / 8)))
    return max(0.05, (len(data) - 44) / 32000.0)


class Handler(BaseHTTPRequestHandler):
    server_version = "SurusStubAsr/1.0"

    def log_message(self, fmt, *args):  # un poco más prolijo
        sys.stderr.write("[stub %s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ------------------------------------------------------------ helpers

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            self.send_json({"ok": False, "reason": "not_found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # -------------------------------------------------------------- rutas

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/health":
            return self.send_json({"ok": True, "ts": round(time.time(), 3)})
        if path == "/api/asr/config":
            return self.send_json({
                "ok": True,
                "model": "parakeet-ultra-Q4_K_M (stub)",
                "rtf_ref": 7.0,
                "chunk_s": 5.0,
            })

        # estáticos: / y /d/<demo> apuntan a web/pages/
        if path in ("/", "/d/asr", "/pages/asr.html"):
            return self.send_file(os.path.join(WEB, "pages", "asr.html"))
        if path.startswith("/d/"):
            name = path[3:].strip("/") or "asr"
            cand = os.path.realpath(os.path.join(WEB, "pages", name + ".html"))
            if cand.startswith(os.path.realpath(WEB)) and os.path.isfile(cand):
                return self.send_file(cand)
            return self.send_json({"ok": False, "reason": "not_found"}, 404)
        if path.startswith("/pages/"):
            name = path[len("/pages/"):]
            cand = os.path.realpath(os.path.join(WEB, "pages", name))
            if cand.startswith(os.path.realpath(WEB)) and os.path.isfile(cand):
                return self.send_file(cand)
            return self.send_json({"ok": False, "reason": "not_found"}, 404)
        if path.startswith("/assets/"):
            cand = os.path.realpath(os.path.join(WEB, path.lstrip("/")))
            if cand.startswith(os.path.realpath(WEB)) and os.path.isfile(cand):
                return self.send_file(cand)
            return self.send_json({"ok": False, "reason": "not_found"}, 404)

        return self.send_json({"ok": False, "reason": "not_found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/models/parakeet-ultra/load":
            time.sleep(0.9)  # simula cargar weights en RAM
            with LOCK:
                STATE["loaded"] = True
            return self.send_json({
                "ok": True,
                "model": {"id": "parakeet-ultra", "status": "ready", "ready_ms": 900},
            })
        if path == "/api/models/parakeet-ultra/unload":
            with LOCK:
                STATE["loaded"] = False
            return self.send_json({"ok": True})

        if path == "/api/asr/window":
            body = self.read_body()
            if not body or not body.get("audio_b64"):
                return self.send_json({"ok": False, "reason": "bad_request",
                                       "detail": "falta audio_b64"}, 400)
            with LOCK:
                loaded = STATE["loaded"]
            if not loaded:
                return self.send_json({
                    "ok": False,
                    "reason": "model_not_loaded",
                    "model": "parakeet-ultra",
                }, 409)

            try:
                audio = base64.b64decode(body["audio_b64"])
            except (ValueError, TypeError):
                return self.send_json({"ok": False, "reason": "bad_request",
                                       "detail": "audio_b64 no es base64"}, 400)

            dur = wav_duration_s(audio)
            lang = (body.get("lang") or "es")[:2]

            t0 = time.monotonic()
            # retardo artificial ~250 ms (en la laptop real: ms ≈ dur/7 * 1000)
            time.sleep(0.24 + random.uniform(0.0, 0.09) + dur * 0.01)
            text, words = synth_words(dur, lang)
            ms = int((time.monotonic() - t0) * 1000)
            rtf = round(ms / max(1.0, dur * 1000), 4)
            return self.send_json({
                "ok": True,
                "text": text,
                "words": words,
                "ms": ms,
                "rtf": rtf,
                "lang": lang,
            })

        return self.send_json({"ok": False, "reason": "not_found"}, 404)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("stub ASR en http://127.0.0.1:%d  (web: %s)" % (PORT, WEB))
    print("parakeet-ultra arranca descargado: el primer window devuelve 409 a propósito")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
