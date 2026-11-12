"""Worker ASR persistente para el hub de SURUS (Demo 3 · parakeet-ultra).

Envuelve el binario C++ ``live-asr`` (``vendor/asrtool/``): un proceso que deja
el modelo GGUF cargado y transcribe ventanas de a una por stdin/stdout, sin
recargar el modelo en cada request.

Protocolo (una línea JSON por request, una línea JSON por respuesta):

    {"cmd":"load","model":"/abs/path.gguf","threads":4}
    {"cmd":"window","id":1,"wav_b64":"<base64 WAV 16k mono PCM16>"}
    {"cmd":"ping"} | {"cmd":"unload"} | {"cmd":"quit"}

Ejemplo de uso desde el hub::

    from hub.asr_worker import AsrWorker
    w = AsrWorker()
    w.start(ROOT + "/models/parakeet-ultra-Q4_K_M.gguf", threads=4)
    r = w.window(open("ventana.wav", "rb").read())   # -> dict con text/words/ms/rtf
    w.stop()

Solo stdlib. Sin dependencias externas.
"""

from __future__ import annotations

import base64
import json
import os
import select
import subprocess
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

# ROOT = .../inferencia-local-demos  (este archivo vive en ROOT/hub/)
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BIN = os.path.join(_ROOT, "vendor", "asrtool", "build", "live-asr")
DEFAULT_MODEL = os.path.join(_ROOT, "models", "parakeet-ultra-Q4_K_M.gguf")

__all__ = ["AsrError", "AsrTimeout", "AsrWorker", "DEFAULT_BIN", "DEFAULT_MODEL"]


class AsrError(RuntimeError):
    """El worker respondió ok:false, murió o no se pudo comunicar."""


class AsrTimeout(AsrError):
    """El worker no respondió dentro del plazo. Se reinicia en el próximo pedido."""


def _child_rss_mb(pid: int) -> Optional[float]:
    """RSS del hijo en MB (Linux), o None si no se puede leer."""
    try:
        with open("/proc/%d/status" % pid, "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return float(parts[1]) / 1024.0
    except OSError:
        return None
    return None


class AsrWorker:
    """Proceso worker de ASR persistente, controlado por líneas JSON."""

    def __init__(
        self,
        bin_path: Optional[str] = None,
        load_timeout: float = 180.0,
        window_timeout: float = 120.0,
        log_lines: int = 300,
    ) -> None:
        self.bin_path = bin_path or DEFAULT_BIN
        self.load_timeout = load_timeout
        self.window_timeout = window_timeout

        self.ready = False  # True sólo después de un "load" exitoso
        self.model: Optional[str] = None
        self.threads: Optional[int] = None
        self.started_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self.load_ms: Optional[float] = None
        self.log: Deque[str] = deque(maxlen=log_lines)

        self._proc: Optional[subprocess.Popen] = None
        self._buf = b""
        self._out_fd: Optional[int] = None
        self._seq = 0
        self._lock = threading.RLock()
        self._stderr_thread: Optional[threading.Thread] = None
        self._info: Dict[str, Any] = {}

    # ------------------------------------------------------------------ spawn

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid if self._proc else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def rss_mb(self) -> Optional[float]:
        return _child_rss_mb(self._proc.pid) if self.is_alive() else None

    def _spawn(self) -> None:
        if not os.path.exists(self.bin_path):
            raise AsrError("no existe el binario del worker: %s" % self.bin_path)
        self._buf = b""
        self._proc = subprocess.Popen(
            [self.bin_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        assert self._proc.stdout is not None
        self._out_fd = self._proc.stdout.fileno()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, name="asr-worker-stderr", daemon=True
        )
        self._stderr_thread.start()

    def _drain_stderr(self) -> None:
        """Consume stderr para que la pipe no se llene y bloquee al hijo."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        fd = proc.stderr.fileno()
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            for ln in chunk.decode("utf-8", "replace").splitlines():
                if ln.strip():
                    self.log.append(ln)

    def _teardown(self) -> None:
        proc = self._proc
        self._proc = None
        self._out_fd = None
        self._buf = b""
        self.ready = False
        if proc is None:
            return
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        self._teardown()

    def _restart(self) -> None:
        """Rearma el proceso y vuelve a cargar el modelo (hijo muerto/timeout)."""
        model = self.model
        threads = self.threads or 4
        self._kill()
        if model is None:
            return
        self._spawn()
        resp = self._request({"cmd": "load", "model": model, "threads": threads}, self.load_timeout)
        self._mark_loaded(model, threads, resp)

    # ------------------------------------------------------------- protocolo

    def _send(self, obj: Dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AsrError("worker no está corriendo")
        data = (json.dumps(obj) + "\n").encode("utf-8")
        try:
            proc.stdin.write(data)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            rc = proc.poll()
            raise AsrError("el worker murió al recibir el comando (rc=%s): %s" % (rc, exc))

    def _read_line(self, timeout: float) -> bytes:
        """Una línea JSON terminada en \\n, con deadline. Levanta AsrTimeout."""
        deadline = time.monotonic() + timeout
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line, self._buf = self._buf[:nl], self._buf[nl + 1:]
                if not line.strip():
                    if time.monotonic() > deadline:
                        raise AsrTimeout("línea vacía y plazo agotado")
                    continue
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AsrTimeout("timeout esperando respuesta del worker (%.1fs)" % timeout)
            assert self._out_fd is not None
            try:
                ready, _, _ = select.select([self._out_fd], [], [], min(remaining, 1.0))
            except OSError as exc:
                raise AsrError("stdout del worker cerrado: %s" % exc)
            if not ready:
                if self._proc is not None and self._proc.poll() is not None:
                    raise AsrError("el worker murió (rc=%s) sin responder" % self._proc.returncode)
                continue
            try:
                chunk = os.read(self._out_fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:  # EOF: el hijo cerró stdout
                rc = self._proc.returncode if self._proc is not None else None
                raise AsrError("stdout cerrado por el worker (rc=%s)" % rc)
            self._buf += chunk

    def _request(self, obj: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        self._send(obj)
        line = self._read_line(timeout)
        try:
            resp = json.loads(line.decode("utf-8", "replace"))
        except ValueError as exc:
            raise AsrError("respuesta no-JSON del worker: %r" % line[:200]) from exc
        if not isinstance(resp, dict):
            raise AsrError("respuesta inesperada del worker: %r" % resp)
        return resp

    def _request_ok(self, obj: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        resp = self._request(obj, timeout)
        if not resp.get("ok"):
            self.last_error = str(resp.get("error", "error desconocido"))
            raise AsrError(self.last_error)
        return resp

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _mark_loaded(self, model: str, threads: int, resp: Dict[str, Any]) -> None:
        self.model = model
        self.threads = threads
        self._info = {
            k: resp.get(k)
            for k in (
                "arch",
                "backend",
                "sample_rate",
                "max_timestamp_kind",
                "supports_streaming",
                "supports_language_detect",
                "max_audio_ms",
                "effective_max_audio_ms",
                "languages",
            )
            if k in resp
        }
        self.load_ms = resp.get("ms")
        self.started_at = time.time()
        self.ready = True
        self.last_error = None

    # ---------------------------------------------------------------- public

    def start(self, model_path: Optional[str] = None, threads: int = 4) -> Dict[str, Any]:
        """Levanta el proceso (si hace falta) y carga el modelo. Idempotente."""
        model_path = os.path.abspath(model_path or DEFAULT_MODEL)
        with self._lock:
            if self.is_alive() and self.ready and self.model == model_path and self.threads == threads:
                return dict(self._info, cached=True, pid=self.pid, rss_mb=self.rss_mb())

            if not self.is_alive():
                self._kill()
                self._spawn()

            resp = self._request(
                {"cmd": "load", "model": model_path, "threads": int(threads)}, self.load_timeout
            )
            if not resp.get("ok"):
                self.last_error = str(resp.get("error", "no se pudo cargar el modelo"))
                self._kill()
                raise AsrError(self.last_error)
            self._mark_loaded(model_path, int(threads), resp)
            return dict(self._info, pid=self.pid, rss_mb=self.rss_mb())

    def window(self, wav_bytes: bytes, lang: Optional[str] = None,
               timestamps: Optional[str] = None) -> Dict[str, Any]:
        """Transcribe una ventana (WAV 16 kHz mono PCM16 ya codificado).

        Devuelve el dict de la respuesta (``text``, ``words``, ``segments``,
        ``ms``, ``run_ms``, ``audio_s``, ``rtf``). Levanta :class:`AsrError` /
        :class:`AsrTimeout` ante errores. Si el hijo se murió, se reinicia y
        reintenta una sola vez.
        """
        if not wav_bytes:
            raise AsrError("wav_bytes vacío")
        with self._lock:
            if self.model is None:
                raise AsrError("modelo no cargado: llamá start(model_path) primero")
            if not self.ready:
                raise AsrError("worker detenido (stop/unload): volvé a llamar start()")

            if not self.is_alive():
                self.last_error = "worker muerto, reiniciando"
                self._restart()

            req: Dict[str, Any] = {
                "cmd": "window",
                "id": self._next_id(),
                "wav_b64": base64.b64encode(wav_bytes).decode("ascii"),
            }
            if lang:
                req["lang"] = lang
            if timestamps:
                req["timestamps"] = timestamps

            try:
                resp = self._request_ok(req, self.window_timeout)
            except AsrTimeout:
                # La pipe quedó posiblemente desincronizada: reinicia para el próximo pedido.
                self.last_error = "timeout en window; worker reiniciado"
                try:
                    self._restart()
                except Exception:  # noqa: BLE001 - el timeout ya es el error primario
                    pass
                raise
            except AsrError:
                if not self.is_alive():  # murió en el medio: rearmar y reintentar una vez
                    self._restart()
                    resp = self._request_ok(dict(req, id=self._next_id()), self.window_timeout)
            return resp

    def ping(self) -> Dict[str, Any]:
        with self._lock:
            if not self.is_alive():
                raise AsrError("worker no está corriendo")
            return self._request_ok({"cmd": "ping"}, 5.0)

    def unload(self) -> Dict[str, Any]:
        with self._lock:
            if not self.is_alive():
                self.ready = False
                return {"ok": True, "ready": False}
            resp = self._request_ok({"cmd": "unload"}, 10.0)
            self.ready = False
            return resp

    def stop(self) -> None:
        """Cierra el worker (envía quit y limpia recursos)."""
        with self._lock:
            if self.is_alive():
                try:
                    self._send({"cmd": "quit"})
                    self._read_line(timeout=5.0)
                except (AsrError, AsrTimeout):
                    pass
            self._teardown()
            self.ready = False

    # Context manager para pruebas rápidas.
    def __enter__(self) -> "AsrWorker":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.stop()


if __name__ == "__main__":  # smoke manual: python3 hub/asr_worker.py [wav]
    import sys

    wav_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(_ROOT, ".tmp", "sample_16k.wav")
    w = AsrWorker()
    t0 = time.time()
    info = w.start(DEFAULT_MODEL, threads=4)
    print("load ms=%s pid=%s rss_mb=%s info=%s" % (info.get("ms"), info.get("pid"), info.get("rss_mb"), info))
    print("spawn+load wall: %.2fs" % (time.time() - t0))
    with open(wav_path, "rb") as fh:
        for i in range(3):
            t = time.time()
            r = w.window(fh.read())
            print("[%d] wall=%.0f ms=%s rtf=%s -> %s" % (i, (time.time() - t) * 1000, r.get("ms"), r.get("rtf"), r.get("text")))
            print("    words[:3]=%s" % (r.get("words") or [])[:3])
    print("rss_mb=%s ready=%s" % (w.rss_mb(), w.ready))
    w.stop()
    print("stopped, ready=%s" % w.ready)
