#!/usr/bin/env python3
"""_stub_text.py · scratch (prefijo `_`), NO forma parte del hub real.

Simula en 127.0.0.1:8195 lo que necesitan /d/llm (Demo 2) y /d/search
(Demo 4) para desarrollarse sin tener los gguf cargados:

  GET  /d/llm · /d/search      → web/pages/llm.html · search.html
  GET  /                       → launcher mínimo con links a ambas demos
  GET  /assets/* · /pages/*    → estáticos desde web/
  GET  /api/llm/config         → modelos + bloque pi
  POST /api/llm/chat           → JSON streameado de a poco (chunked), con
                                 timings plausibles (LFM3 ~11 tok/s y
                                 reasoning primero; MNN ~4x más rápido)
  GET  /api/search/config      → rows_total 294595 · dims 768 · scopes
  POST /api/search             → resultados simulados del RI CONICET;
                                 acepta query o image_b64 (gana la imagen,
                                 devuelve mode:"imagen" y scores cross-modal
                                 más bajos); sin ambos → 400 bad_request
  POST /api/models/{id}/load   → {ok:true} (y /unload, para probar el 409)

Corre con la stdlib. Uso:  python3 scripts/_stub_text.py [puerto]
"""

import hashlib
import json
import math
import random
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
PAGES = WEB / "pages"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8195
HOST = "127.0.0.1"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".map": "application/json",
}

# ------------------------------------------------------------------ estado

LOCK = threading.Lock()
MODELS = {
    "lfm3-1.7b": {
        "label": "LFM3-1.7B", "kind": "llm-chat", "params": "1.7B",
        "quant": "Q4_K_M", "size_mb": 1180, "est_ram_mb": 1800, "port": 8182,
        "rate": 11.0, "prefill_rate": 42.0, "thinking": True, "loaded": True,
        "metrics": {"tok_per_s": 11.0},
    },
    "mnn-pocket": {
        "label": "MNN-Pocket-0.3B", "kind": "llm-chat", "params": "0.3B",
        "quant": "Q4_1", "size_mb": 210, "est_ram_mb": 450, "port": 8183,
        "rate": 45.0, "prefill_rate": 120.0, "thinking": False, "loaded": True,
        "metrics": {"tok_per_s": 45.0},
    },
    "embeddinggemma": {
        "label": "EmbeddingGemma-2", "kind": "embeddings", "params": "300M",
        "quant": "Q8_0", "size_mb": 330, "est_ram_mb": 450, "port": 8185,
        "rate": None, "thinking": False, "loaded": True, "metrics": {},
    },
}

ROWS_TOTAL = 294595
SCOPES = {"p1": 2946, "p5": 14730, "p25": 73649, "full": 294595}

# ------------------------------------------------------------- chat simulado

REASON_POOL = [
    "Primero identifico qué pregunta el usuario: quiere una explicación concreta, no una encuesta. "
    "Mencionó inferencia local, así que convienen números de esta máquina. Ordeno la idea: qué ocurre, "
    "por qué cuesta y qué lo mitigate. Reviso que la respuesta quede corta y sin promesas exageradas: "
    "el prefill es paralelo pero igual tarda en CPU; el decode es secuencial y manda en la latencia percibida.",
    "Interpreto la consulta y busco el concepto central. Verifico no mezclar términos: prompt eval vs "
    "prompt cache, TTFT vs TPOT. Elijo dos ejemplos concretos y cierro con una regla práctica para la clase.",
    "Descompongo: entrada tokens, atención sobre el prefijo, generación secuencial. Chequeo coherencia "
    "numérica (prefill ~40 tok/s, decode ~11 tok/s en LFM3 Q4). Reduzco la respuesta a lo esencial.",
]

ANSWER_BY_KEY = [
    (("prefill", "ttft", "latencia", "prompt"),
     "Durante el prefill el modelo procesa todo tu prompt de una sola vez y llena el KV cache; "
     "recién después empieza el decode, token a token. Por eso el TTFT (el tiempo hasta el primer "
     "token) lo domina el prefill: en esta laptop, LFM3 Q4 prefillea a ~40 tok/s, así que un prompt "
     "de 800 tokens tarda unos 20 s antes de que aparezca la primera palabra. Con prompt caching o "
     "contextos cortos el TTFT baja muchísimo; el decode, en cambio, queda en ~11 tok/s siempre."),
    (("thinking", "razon", "lfm"),
     "LFM3-1.7B es un modelo thinking: primero emite reasoning_content y después la respuesta final. "
     "En la práctica eso significa que ves salir texto antes de la respuesta y que los tokens totales "
     "del turno crecen: el razonamiento consume decode. Si apagás el modo thinking, la respuesta es "
     "más corta y llega antes, a costa de respuestas más simples."),
    (("quant", "cuant", "gguf", "modelo", "ram"),
     "La cuantización achica los pesos para que el modelo entre en la RAM disponible: Q4_K_M baja los "
     "pesos a ~4 bits y LFM3 1.7B queda en ~1,8 GB estimados. Se pierde algo de precisión, pero en CPU "
     "el cuello de botella es el ancho de banda de memoria: con menos bytes por peso, el decode va más "
     "rápido. Por eso MNN-Pocket Q4_1, además de ser más chico, corre ~4× más rápido."),
    (("pi", "proxy", "openai", "agente"),
     "El hub expone un proxy OpenAI-compatible en /v1/proxy/<model_id>, así que pi puede hablar con tu "
     "laptop como si fuera la nube: misma API, pero todo corre en 127.0.0.1. Cargá el modelo en el "
     "launcher y corré el comando que figura más abajo; si el modelo no está cargado, el hub responde "
     "409 y la página ofrece cargarlo."),
]

GENERIC_ANSWER = (
    "Buena pregunta para la clase de inferencia local: en esta laptop (i5-10310U, 14 GB, sin GPU) todo "
    "pasa por CPU y RAM, así que las dos fases del generativo se notan separadas. El prefill procesa el "
    "prompt por bloques y el decode genera un token a la vez; las métricas de este turno muestran ambas "
    "en crudo. Probá cambiar de modelo o apagar el thinking para ver la diferencia de tiempos."
)

SEARCH_POOL = [
    {"item_id": "2021/061234", "title": "El bioma Patagónico frente al cambio climático: temperatura y precipitaciones (1960-2020)",
     "authors": ["A. Fernández", "R. Quiroga"], "year": 2021, "pdf": True},
    {"item_id": "2019/057812", "title": "Cambio climático y dinámicas del bioma Patagónico: una revisión",
     "authors": ["L. Arroyo"], "year": 2019, "pdf": False},
    {"item_id": "2022/068901", "title": "Aprendizaje automático aplicado a series temporales climáticas del sur argentino",
     "authors": ["M. Ledesma", "C. Bravo"], "year": 2022, "pdf": True},
    {"item_id": "2018/049553", "title": "Arqueología del NOA: ocupación humana temprana en la Puna",
     "authors": ["S. Götz", "P. Lynch"], "year": 2018, "pdf": True},
    {"item_id": "2020/063110", "title": "Lingüística quechua: variación y contacto en el noroeste argentino",
     "authors": ["N. Zapet", "F. Vergara"], "year": 2020, "pdf": False},
    {"item_id": "2017/045021", "title": "Materiales compuestos con fibras vegetales: caracterización mecánica",
     "authors": ["J. Arrieta", "D. Sosa"], "year": 2017, "pdf": True},
    {"item_id": "2023/071344", "title": "Modelos de aprendizaje automático para detección de incendios forestales",
     "authors": ["V. Palomeque"], "year": 2023, "pdf": True},
    {"item_id": "2016/041789", "title": "Vientos patagónicos y erosión eólica en ambientes semiáridos",
     "authors": ["G. Varas", "H. Outes"], "year": 2016, "pdf": False},
    {"item_id": "2021/060277", "title": "Compuestos poliméricos reforzados con nanopartículas: síntesis y propiedades",
     "authors": ["E. Aldabe", "R. Molfino"], "year": 2021, "pdf": True},
    {"item_id": "2015/038112", "title": "Clima patagónico: tendencias de temperatura en los últimos 60 años",
     "authors": ["B. Bianucci"], "year": 2015, "pdf": True},
    {"item_id": "2019/056098", "title": "Relevamiento lingüístico del quechua santiagueño",
     "authors": ["T. Rossi", "M. Ferreyra"], "year": 2019, "pdf": False},
    {"item_id": "2022/069531", "title": "Zooarqueología del NOA: fauna y prácticas de pastoreo en la puna seca",
     "authors": ["I. Cruz", "O. Yacobaccio"], "year": 2022, "pdf": True},
    {"item_id": "2024/074820", "title": "Redes convolucionales para clasificación de imágenes satelitales de cultivos",
     "authors": ["K. Duarte", "A. Fernández"], "year": 2024, "pdf": True},
]

STOPWORDS = {"el", "la", "los", "las", "de", "del", "en", "y", "un", "una", "que", "para", "sobre", "con", "al"}


def _jitter(seed: str, lo: float, hi: float) -> float:
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    return lo + (hi - lo) * (h / 0xFFFFFFFF)


def est_prompt_tokens(messages) -> int:
    n = 0
    for m in messages:
        n += len(str(m.get("content", ""))) // 4 + 4
    return max(8, n)


def sim_chat(body):
    """Devuelve (objeto_json, n_tokens_generados, rate) para simular el chat."""
    mid = str(body.get("model") or "lfm3-1.7b")
    m = MODELS.get(mid)
    thinking_on = bool(body.get("thinking")) and m.get("thinking")
    q = " ".join(str(x.get("content", "")) for x in body.get("messages", [])
                 if isinstance(x, dict)).lower()

    answer = None
    for keys, text in ANSWER_BY_KEY:
        if any(k in q for k in keys):
            answer = text
            break
    if not answer:
        answer = GENERIC_ANSWER

    rng = random.Random(q or "sin-query")
    reasoning = ""
    if thinking_on:
        reasoning = rng.choice(REASON_POOL)
    elif m.get("thinking"):
        # thinking apagado en un modelo thinking: respuesta corta explícita
        answer = answer.split(". ")[0] + "."

    words = len(reasoning.split()) + len(answer.split())
    rate = m["rate"] * _jitter(q + "rate", 0.92, 1.08)
    prefill_rate = m["prefill_rate"] * _jitter(q + "pre", 0.9, 1.1)
    pt = est_prompt_tokens(body.get("messages", []))

    obj = {
        "ok": True,
        "model": mid,
        "content": answer,
        "reasoning": reasoning,
        "usage": {"prompt_tokens": pt, "completion_tokens": words,
                  "total_tokens": pt + words},
        "timings": {
            "prompt_ms": round(pt / prefill_rate * 1000),
            "predicted_ms": round(words / rate * 1000),
            "prompt_per_second": round(prefill_rate, 1),
            "predicted_per_second": round(rate, 1),
        },
    }
    return obj, words, rate


def sim_search(body):
    query = str(body.get("query") or "").strip()
    img = body.get("image_b64")
    use_image = bool(img) and str(img).strip() != ""
    k = int(body.get("k") or 10)
    scope = int(body.get("scope") or SCOPES["p1"])
    scanned = min(max(0, scope), ROWS_TOTAL)

    if use_image:
        seed = str(img)[:96]          # determinístico por frame capturado
        qwords = []
    else:
        seed = query
        qwords = [w for w in query.lower().replace(",", " ").split() if w not in STOPWORDS]

    scored = []
    for it in SEARCH_POOL:
        text = (it["title"] + " " + " ".join(it["authors"])).lower()
        overlap = sum(1 for w in qwords if w in text)
        jit = _jitter(seed + it["item_id"], 0.02, 0.18)
        if use_image:
            # cross-modal imagen↔texto vía mmproj: scores típicos ~0,2–0,5
            score = min(0.52, 0.16 + 0.10 * overlap + jit * 1.4)
        else:
            score = min(0.95, 0.30 + 0.13 * overlap + jit)
        scored.append((score, it))
    scored.sort(key=lambda t: -t[0])
    results = []
    for score, it in scored[:max(1, min(k, 10))]:
        handle = "11336/" + it["item_id"].replace("/", "-")
        results.append({
            "item_id": it["item_id"],
            "title": it["title"],
            "authors": it["authors"],
            "year": it["year"],
            "type": "Artículo de publicación periódica",
            "language": "Español",
            "score": round(score, 4),
            "handle": handle,
            "pdf_url": ("https://ri.conicet.gov.ar/bitstream/handle/%s/"
                        "CONICET_Digital_Nro.%s.pdf" % (handle, it["item_id"].split("/")[1]))
                       if it["pdf"] else None,
        })

    if use_image:
        embed_ms = round(_jitter(seed + "iemb", 60, 150))  # forward del proyector de visión
    else:
        embed_ms = round(_jitter(seed + "emb", 14, 38))
    scan_ms = round(scanned / 1000 * 9 + _jitter(seed + "scan", 5, 30))
    obj = {
        "ok": True,
        "mode": "imagen" if use_image else "texto",
        "took_ms": embed_ms + scan_ms + 6,
        "embed_ms": embed_ms,
        "scan_ms": scan_ms,
        "scanned_rows": scanned,
        "rows_total": ROWS_TOTAL,
        "dims": 768,
        "k": k,
        "results": results,
    }
    return obj


# ------------------------------------------------------------------- servidor

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SurusStubText/1.0"

    def log_message(self, fmt, *args):  # stdout amigable
        sys.stderr.write("[stub %s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ------------------------------------------------------------ escritura

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path):
        try:
            data = path.read_bytes()
        except OSError:
            return self._send_json({"ok": False, "reason": "bad_request"}, 404)
        mime = MIME.get(path.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, html: str):
        data = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------- utilidades

    def _safe_static(self, url_path: str):
        """Resuelve /assets/... y /pages/... dentro de web/, sin escapar de la raíz."""
        rel = url_path.lstrip("/")
        if rel.startswith("pages/"):
            rel = "pages/" + rel[len("pages/"):]
        target = (WEB / rel).resolve()
        base = WEB.resolve()
        if not str(target).startswith(str(base) + "/") or not target.is_file():
            return None
        return target

    # ------------------------------------------------------------------ GET

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            return self._send_html(
                "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
                "<title>stub · SURUS demos de texto</title>"
                "<link rel='stylesheet' href='/assets/surus/tokens.css'>"
                "<link rel='stylesheet' href='/assets/surus/fonts.css'>"
                "<link rel='stylesheet' href='/assets/app.css'></head>"
                "<body><main class='page'><span class='eyebrow'>stub _stub_text.py · %d</span>"
                "<h1 class='h1'>Demos de <span class='hl'>texto</span> (stub)</h1>"
                "<div class='btn-row'><a class='btn btn--primary' href='/d/llm'>/d/llm · LLM en vivo</a>"
                "<a class='btn btn--primary' href='/d/search'>/d/search · RI CONICET</a></div>"
                "</main></body></html>" % PORT)
        if path in ("/d/llm", "/d/llm/"):
            return self._send_file(PAGES / "llm.html")
        if path in ("/d/search", "/d/search/"):
            return self._send_file(PAGES / "search.html")

        st = self._safe_static(path)
        if st:
            return self._send_file(st)

        if path == "/api/health":
            return self._send_json({"ok": True, "ts": round(time.time(), 3), "stub": True})

        if path == "/api/llm/config":
            models = []
            with LOCK:
                for mid, m in MODELS.items():
                    if mid == "embeddinggemma":
                        continue
                    models.append({
                        "id": mid, "label": m["label"], "kind": m["kind"],
                        "params": m["params"], "quant": m["quant"],
                        "size_mb": m["size_mb"], "est_ram_mb": m["est_ram_mb"],
                        "port": m["port"], "status": "ready" if m["loaded"] else "off",
                        "metrics": dict(m["metrics"]),
                    })
            return self._send_json({
                "ok": True,
                "models": models,
                "pi": {
                    "cmd": "pi --provider openai --model lfm3-1.7b "
                           "--api-base-url http://127.0.0.1:8180/v1/proxy/lfm3-1.7b",
                    "base_url": "http://127.0.0.1:8180/v1/proxy/lfm3-1.7b",
                    "notes": "El modelo queda servido local en 127.0.0.1: el hub actúa de proxy "
                             "OpenAI-compatible en /v1/proxy/<model_id>.",
                },
            })

        if path == "/api/search/config":
            return self._send_json({
                "ok": True,
                "rows_total": ROWS_TOTAL,
                "indexed_rows": ROWS_TOTAL,
                "dims": 768,
                "model": "embeddinggemma-2-Q8_0",
                "scopes": SCOPES,
                "corpus": "UNRN/ri-conicet",
            })

        return self._send_json({"ok": False, "reason": "bad_request"}, 404)

    # ----------------------------------------------------------------- POST

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = self._read_body()

        if path == "/api/llm/chat":
            mid = str(body.get("model") or "")
            m = MODELS.get(mid)
            if not m or mid == "embeddinggemma":
                return self._send_json({"ok": False, "reason": "bad_request",
                                        "model": mid or "?"}, 400)
            with LOCK:
                loaded = m["loaded"]
            if not loaded:
                return self._send_json({"ok": False, "reason": "model_not_loaded",
                                        "model": mid}, 409)
            obj, words, rate = sim_chat(body)
            # Streaming honesto del stub: JSON chunked, "tokens de a poco".
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                time.sleep(obj["timings"]["prompt_ms"] / 1000.0)  # TTFT simulado
                step = max(0.015, min(0.09, 1.0 / max(rate, 1.0)))
                size = max(24, math.ceil(len(data) / max(words, 1)))
                for i in range(0, len(data), size):
                    chunk = data[i:i + size]
                    self.wfile.write(b"%X\r\n" % len(chunk))
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    time.sleep(step)
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if path == "/api/search":
            with LOCK:
                loaded = MODELS["embeddinggemma"]["loaded"]
            if not loaded:
                return self._send_json({"ok": False, "reason": "model_not_loaded",
                                        "model": "embeddinggemma"}, 409)
            query = str(body.get("query") or "").strip()
            img = body.get("image_b64")
            if not query and not (img and str(img).strip()):
                return self._send_json({"ok": False, "reason": "bad_request"}, 400)
            # si vienen query e imagen, gana la imagen (cross-modal): lo resuelve sim_search
            return self._send_json(sim_search(body))

        if path.startswith("/api/models/") and path.endswith("/load"):
            mid = path[len("/api/models/"):-len("/load")].strip("/")
            with LOCK:
                if mid in MODELS:
                    MODELS[mid]["loaded"] = True
                    return self._send_json({"ok": True, "model": mid})
            return self._send_json({"ok": False, "reason": "bad_request",
                                    "model": mid}, 404)

        if path.startswith("/api/models/") and path.endswith("/unload"):
            mid = path[len("/api/models/"):-len("/unload")].strip("/")
            with LOCK:
                if mid in MODELS:
                    MODELS[mid]["loaded"] = False
                    return self._send_json({"ok": True, "model": mid})
            return self._send_json({"ok": False, "reason": "bad_request",
                                    "model": mid}, 404)

        return self._send_json({"ok": False, "reason": "bad_request"}, 404)


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("stub _stub_text.py en http://%s:%d  (Ctrl+C para salir)" % (HOST, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
