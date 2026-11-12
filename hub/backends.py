"""Gerenciador de backends de inferencia local (CPU) para el hub SURUS.

Un solo proceso hub controla:
  - N instancias de llama-server (VLM, LLM x2, embeddings)
  - un worker ASR persistente (transcribe.cpp / parakeet-ultra)
  - YOLO in-process (ultralytics + torch CPU)

Regla de oro en esta laptop (14 GB, ~4.8 GB disponibles al arrancar):
no se carga un modelo si empuja el uso por encima del presupuesto.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "vendor", "llama.cpp", "build", "bin", "llama-server")
LOGS = os.path.join(ROOT, "logs")

# Presupuesto de RAM para modelos a la vez. Ajustable con DSH_RAM_BUDGET_MB.
RAM_BUDGET_MB = int(os.environ.get("DSH_RAM_BUDGET_MB", "4096"))
LD_LIBRARY_PATH = os.path.join(ROOT, "vendor", "llama.cpp", "build", "bin")


def meminfo() -> Dict[str, float]:
    """MemTotal / MemAvailable desde /proc/meminfo, en MB."""
    out = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                if k in ("MemTotal", "MemAvailable"):
                    out[k] = int(v.strip().split()[0]) / 1024.0
    except OSError:
        pass
    return {"total_mb": out.get("MemTotal", 0.0), "available_mb": out.get("MemAvailable", 0.0)}


def cpu_info() -> Dict[str, Any]:
    """Carga del sistema: load average, núcleos y RSS del propio hub."""
    try:
        with open("/proc/loadavg") as fh:
            l1, l5, l15 = (float(x) for x in fh.read().split()[:3])
    except OSError:
        l1 = l5 = l15 = 0.0
    return {"load1": l1, "load5": l5, "load15": l15, "cores": os.cpu_count() or 0,
            "threads": int(os.environ.get("DSH_THREADS", "4")),
            "hub_rss_mb": round(proc_rss_mb(os.getpid()), 1)}


def proc_rss_mb(pid: Optional[int]) -> float:
    if not pid:
        return 0.0
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        return 0.0
    return 0.0


@dataclass
class ModelSpec:
    id: str
    label: str
    kind: str          # vlm | llm | asr | embed | detect
    src: str           # repo o origen del peso
    params: str
    quant: str
    size_mb: int
    est_ram_mb: int
    engine: str
    port: Optional[int] = None
    path: Optional[str] = None      # peso relativo a ROOT (puede ser "a + b")
    args: List[str] = field(default_factory=list)
    alias: Optional[str] = None     # nombre limpio que expone la API OpenAI
    note: str = ""

    def to_public(self) -> Dict[str, Any]:
        return {
            "id": self.id, "label": self.label, "kind": self.kind, "src": self.src,
            "params": self.params, "quant": self.quant, "size_mb": self.size_mb,
            "est_ram_mb": self.est_ram_mb, "engine": self.engine, "port": self.port,
            "note": self.note,
        }


def _p(*parts: str) -> str:
    return os.path.join(ROOT, *parts)


def planned_cmd(spec: ModelSpec) -> str:
    """Comando exacto con el que el hub sirve este modelo (aunque esté descargado)."""
    n_threads = os.environ.get("DSH_THREADS", "4")
    ctx = os.environ.get("DSH_CTX_LLM", "8192") if spec.kind == "llm" else "4096"
    parts = ["llama-server", "-m", spec.path or "", "--host", "127.0.0.1",
             "--port", str(spec.port or 0), "-t", n_threads, "-ngl", "0",
             "-a", spec.alias or spec.id, "-c", ctx] + list(spec.args)
    return " ".join(x if " " not in x else f'"{x}"' for x in parts).replace(ROOT + "/", "")


SERVE_CMD = {
    "parakeet-ultra": (
        'vendor/asrtool/build/live-asr   # worker persistente; por stdin: '
        '{"cmd":"load","model":"models/parakeet-ultra-Q4_K_M.gguf","threads":4} '
        'y luego {"cmd":"window","wav_b64":"…"}'),
    "yolo": (
        "python -c \"from ultralytics import YOLO; YOLO('yolo26s.pt')\"  "
        "# in-process en el hub; torch.set_num_threads(4); "
        "predict(img, device='cpu', imgsz=640, conf=0.35, iou=0.5)"),
}


REGISTRY: Dict[str, ModelSpec] = {s.id: s for s in [
    ModelSpec(
        id="onejev-vlm", label="OneJev 1.6 · 0.8B", kind="vlm",
        src="OmniJev/OneJev1.6-0.8B (GGUF de mradermacher)", params="0.8B", quant="Q4_K_M",
        size_mb=529 + 116, est_ram_mb=1150, engine="llama-server", port=8181,
        path="models/OneJev1.6-0.8B.Q4_K_M.gguf",
        args=["--mmproj", _p("models/OneJev1.6-0.8B.mmproj-Q8_0.gguf"),
              "--image-max-tokens", "96"],
        note="Modelo de decisión (System 1) multimodal; responde el conteo de dedos.",
    ),
    ModelSpec(
        id="lfm3-1.7b", label="LFM3 · 1.7B", kind="llm",
        src="LiquidAI/LFM3-1.7B-GGUF", params="1.7B", quant="Q4_K_M",
        size_mb=1100, est_ram_mb=1800, engine="llama-server", port=8182,
        path="models/LFM3-1.7B-Q4_K_M.gguf", args=[],
        note="Modelo thinking: primero razona y después responde (~11 tok/s en esta CPU).",
    ),
    ModelSpec(
        id="mnn-pocket", label="MNN-Pocket · 0.3B", kind="llm",
        src="taobao-mnn/MNN-Pocket-0.3B-GGUF", params="0.3B", quant="Q4_1",
        size_mb=192, est_ram_mb=450, engine="llama-server", port=8183,
        path="models/MNN-Pocket-0.3B-Q4_1.gguf", args=[],
        note="El más chico y rápido: ideal para alternar con pi en vivo.",
    ),
    ModelSpec(
        id="parakeet-ultra", label="Parakeet Ultra · 0.6B", kind="asr",
        src="moondream/parakeet-ultra (GGUF de handy-computer)", params="627M", quant="Q4_K_M",
        size_mb=486, est_ram_mb=900, engine="transcribe.cpp",
        path="models/parakeet-ultra-Q4_K_M.gguf",
        note="25 idiomas. ~7× tiempo real en CPU; el live se arma con VAD + ventanas.",
    ),
    ModelSpec(
        id="embeddinggemma", label="EmbeddingGemma-2", kind="embed",
        src="google/embeddinggemma-2 (GGUF de ggml-org)", params="2B", quant="Q8_0",
        size_mb=310 + 555, est_ram_mb=800, engine="llama-server", port=8185,
        path="models/embeddinggemma-2-Q8_0.gguf",
        args=["--embedding", "--pooling", "mean",
              "--mmproj", _p("models/mmproj-embeddinggemma-2-Q8_0.gguf"),
              # 256 tokens de imagen → ~12 s por foto. Con 128 baja a ~5 s pero la
              # recuperación empeora (aparece "probióticos" por un colectivo).
              "--image-max-tokens", "256"],
        note="768 dims, L2-normalizado. Es la misma familia que los vectores del corpus.",
    ),
    ModelSpec(
        id="yolo", label="YOLO26 · s", kind="detect",
        src="docs.ultralytics.com/models/yolo26", params="10M (s)", quant="fp32 CPU",
        size_mb=20, est_ram_mb=900, engine="ultralytics",
        path="yolo26s.pt",
        note="Detecta 80 clases COCO en la webcam. ~160 ms/frame a 640 en esta CPU.",
    ),
]}

DEMOS = [
    {"id": "dedos", "path": "/d/dedos", "title": "Dedos en vivo", "models": ["onejev-vlm"],
     "tagline": "El modelo de decisión cuenta los dedos que levantás, frame a frame."},
    {"id": "llm", "path": "/d/llm", "title": "LLM servido local", "models": ["lfm3-1.7b", "mnn-pocket"],
     "tagline": "Chat con dos modelos abiertos, y el mismo endpoint listo para pi."},
    {"id": "asr", "path": "/d/asr", "title": "Transcripción en vivo", "models": ["parakeet-ultra"],
     "tagline": "Parakeet Ultra transcribiendo tu voz por ventanas, en CPU."},
    {"id": "search", "path": "/d/search", "title": "Búsqueda semántica",
     "models": ["embeddinggemma"],
     "tagline": "EmbeddingGemma-2 sobre el corpus del CONICET, que ya trae sus vectores."},
    {"id": "yolo", "path": "/d/yolo", "title": "Webcam con YOLO26", "models": ["yolo"],
     "tagline": "Detección de 80 clases en tiempo real, con los modelos n / s / m."},
]


class LlamaBackend:
    """Una instancia de llama-server administrada como subprocesso."""

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.proc: Optional[subprocess.Popen] = None
        self.status = "off"
        self.ready_ms: Optional[int] = None
        self.last_error: Optional[str] = None
        self.cmd: Optional[str] = None
        self.log_path = os.path.join(LOGS, f"{spec.id}.log")
        self._lock = threading.Lock()

    def _env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = LD_LIBRARY_PATH + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        env.setdefault("TMPDIR", os.path.join(ROOT, ".tmp"))
        return env

    def start(self) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            self.status = "loading"
            self.last_error = None
            path = _p(self.spec.path)
            if not os.path.exists(path):
                self.status, self.last_error = "error", f"no existe {self.spec.path}"
                raise FileNotFoundError(path)
            n_threads = os.environ.get("DSH_THREADS", "4")
            ctx = os.environ.get("DSH_CTX_LLM", "8192") if self.spec.kind == "llm" else "4096"
            cmd = [BIN, "-m", path, "--host", "127.0.0.1", "--port", str(self.spec.port),
                   "-t", n_threads, "-ngl", "0",
                   "-a", self.spec.alias or self.spec.id, "-c", ctx] + list(self.spec.args)
            os.makedirs(LOGS, exist_ok=True)
            log = open(self.log_path, "w")
            t0 = time.time()
            self.cmd = " ".join(cmd)
            self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=self._env())
            try:
                self._wait_ready(timeout=180)
            except Exception as exc:
                self.stop()
                self.status, self.last_error = "error", str(exc)
                raise
            self.ready_ms = int((time.time() - t0) * 1000)
            self.status = "ready"

    def _wait_ready(self, timeout: int) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc is None or self.proc.poll() is not None:
                raise RuntimeError(f"llama-server murió al cargar ({self.spec.id})")
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.spec.port}/health", timeout=2) as r:
                    if json.loads(r.read().decode() or "{}").get("status") == "ok":
                        return
            except Exception:
                pass
            time.sleep(0.5)
        raise RuntimeError(f"timeout cargando {self.spec.id}")

    def stop(self) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None
            self.status = "off"

    def rss_mb(self) -> float:
        return proc_rss_mb(self.proc.pid if self.proc else None)

    def post(self, path: str, payload: Dict[str, Any], timeout: float = 180.0) -> Dict[str, Any]:
        url = f"http://127.0.0.1:{self.spec.port}{path}"
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    def metrics(self) -> Dict[str, Any]:
        return {"rss_mb": round(self.rss_mb(), 1), "port": self.spec.port,
                "ready_ms": self.ready_ms, "cmd": self.cmd}


class BackendManager:
    def __init__(self) -> None:
        self.llama: Dict[str, LlamaBackend] = {
            s.id: LlamaBackend(s) for s in REGISTRY.values() if s.engine == "llama-server"
        }
        self.asr = None          # lazy: hub.asr_worker.AsrWorker
        self.yolo = None         # lazy: ultralytics YOLO
        self.yolo_name = "yolo26s"
        self.asr_loaded = False
        self.yolo_loaded = False

    # ---- estado ------------------------------------------------------------
    def used_models_mb(self) -> float:
        used = 0.0
        for b in self.llama.values():
            if b.status == "ready":
                used += b.rss_mb() or b.spec.est_ram_mb
        if self.asr_loaded:
            used += REGISTRY["parakeet-ultra"].est_ram_mb
        if self.yolo_loaded:
            used += REGISTRY["yolo"].est_ram_mb + (
                700 if self.yolo_name != "yolo26s" else 0)
        return used

    def state(self) -> Dict[str, Any]:
        mi = meminfo()
        models = []
        for sid, spec in REGISTRY.items():
            entry = spec.to_public()
            if sid in self.llama:
                b = self.llama[sid]
                m = b.metrics()
                m["cmd"] = m.get("cmd") or planned_cmd(b.spec)
                entry.update({"status": b.status, "ready_ms": b.ready_ms,
                              "last_error": b.last_error, "metrics": m})
            else:
                loaded = (sid == "parakeet-ultra" and self.asr_loaded) or \
                         (sid == "yolo" and self.yolo_loaded)
                rss = 0.0
                if sid == "parakeet-ultra" and self.asr:
                    try:
                        rss = round(self.asr.rss_mb(), 1)
                    except Exception:                              # noqa: BLE001
                        rss = spec.est_ram_mb
                elif sid == "yolo" and self.yolo_loaded:
                    rss = float(spec.est_ram_mb)
                entry.update({"status": "ready" if loaded else "off",
                              "ready_ms": None, "last_error": None,
                              "metrics": {"rss_mb": rss, "ready_ms": None,
                                          "cmd": SERVE_CMD.get(sid, "")}})
            models.append(entry)
        return {
            "ram": {"total_mb": round(mi["total_mb"]), "available_mb": round(mi["available_mb"]),
                    "used_models_mb": round(self.used_models_mb()), "budget_mb": RAM_BUDGET_MB},
            "cpu": cpu_info(),
            "models": models,
            "demos": DEMOS,
        }

    def is_ready(self, sid: str) -> bool:
        if sid in self.llama:
            return self.llama[sid].status == "ready"
        if sid == "parakeet-ultra":
            return self.asr_loaded
        if sid == "yolo":
            return self.yolo_loaded
        return False

    def free_for(self, sid: str) -> float:
        """MB disponibles dentro del presupuesto si se carga sid."""
        spec = REGISTRY[sid]
        need = spec.est_ram_mb
        if sid == "yolo" and self.yolo_name != "yolo26s":
            need += 700
        return RAM_BUDGET_MB - self.used_models_mb() - need

    def over_ram(self, sid: str) -> Dict[str, Any]:
        mi = meminfo()
        return {"ok": False, "reason": "over_ram", "need_mb": REGISTRY[sid].est_ram_mb,
                "free_mb": round(self.free_for(sid)), "budget_mb": RAM_BUDGET_MB,
                "available_mb": round(mi["available_mb"]),
                "used_models_mb": round(self.used_models_mb())}

    # ---- ciclos de vida ----------------------------------------------------
    def load(self, sid: str) -> Dict[str, Any]:
        if self.is_ready(sid):
            return {"ok": True, "already": True}
        if self.free_for(sid) < 0:
            return self.over_ram(sid)
        if sid in self.llama:
            self.llama[sid].start()
            return {"ok": True, "ready_ms": self.llama[sid].ready_ms}
        if sid == "parakeet-ultra":
            from asr_worker import AsrWorker  # scope del compañero asr-worker
            self.asr = AsrWorker()
            self.asr.start(_p(REGISTRY[sid].path), threads=int(os.environ.get("DSH_THREADS", "4")))
            self.asr_loaded = True
            return {"ok": True}
        if sid == "yolo":
            self._yolo_load(self.yolo_name)
            return {"ok": True}
        return {"ok": False, "reason": "bad_request"}

    def unload(self, sid: str) -> Dict[str, Any]:
        if sid in self.llama:
            self.llama[sid].stop()
        elif sid == "parakeet-ultra":
            if self.asr:
                try:
                    self.asr.stop()
                except Exception:
                    pass
            self.asr, self.asr_loaded = None, False
            import gc; gc.collect()
        elif sid == "yolo":
            self.yolo, self.yolo_loaded = None, False
            import gc; gc.collect()
        return {"ok": True}

    def tail_log(self, sid: str, n: int = 200) -> List[str]:
        if sid in self.llama:
            p = self.llama[sid].log_path
        else:
            p = os.path.join(LOGS, f"{sid}.log")
        try:
            with open(p, "r", errors="replace") as fh:
                return fh.read().splitlines()[-n:]
        except OSError:
            return []

    # ---- YOLO --------------------------------------------------------------
    def _yolo_load(self, name: str) -> None:
        import gc
        from ultralytics import YOLO
        import torch
        torch.set_num_threads(int(os.environ.get("DSH_THREADS", "4")))
        self.yolo = YOLO(f"{name}.pt")
        self.yolo_name = name
        self.yolo_loaded = True
        gc.collect()

    def yolo_frame(self, img_path: str, conf: float, iou: float, imgsz: int, model: str):
        if self.yolo is None or self.yolo_name != model:
            if self.yolo_loaded:
                if self.free_for("yolo") + (self.yolo and REGISTRY["yolo"].est_ram_mb or 0) < 0:
                    return None
            self._yolo_load(model)
        t0 = time.time()
        r = self.yolo.predict(img_path, device="cpu", verbose=False, imgsz=imgsz,
                              conf=conf, iou=iou)
        ms = int((time.time() - t0) * 1000)
        boxes = r[0].boxes
        out = [{"cls": int(c), "name": r[0].names[int(c)], "conf": round(float(cf), 3),
                "xyxy": [round(float(x), 1) for x in xy]}
               for c, cf, xy in zip(boxes.cls, boxes.conf, boxes.xyxy.tolist())]
        return ms, out
