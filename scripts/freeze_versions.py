#!/usr/bin/env python3
"""Escribe hub/versions.json con las versiones reales del stack de esta máquina.

Lo usa GET /api/stack para mostrar el stack "del metal a la app" con datos ciertos.
Volvé a correrlo después de actualizar algo:  python3 scripts/freeze_versions.py
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "hub", "versions.json")


def sh(cmd, timeout=20):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=timeout).decode()
    except Exception as exc:                       # noqa: BLE001
        return f"__error: {exc}"


def llama_version() -> dict:
    out = sh([os.path.join(ROOT, "vendor", "llama.cpp", "build", "bin", "llama-server"), "--version"])
    ver = re.search(r"version:\s*(\S+)\s*\(build (\d+), commit ([0-9a-f]+)\)", out)
    built = re.search(r"built with (\S+) for (\S+)", out)
    return {
        "version": ver.group(1) if ver else "desconocida",
        "build": ver.group(2) if ver else "?",
        "commit": ver.group(3) if ver else "?",
        "toolchain": built.group(1) if built else "?",
        "target": built.group(2) if built else "?",
    }


def git_commit(path: str) -> dict:
    out = sh(["git", "-C", path, "log", "-1", "--format=%h|%ad|%s", "--date=short"])
    parts = out.strip().split("|")
    return {"commit": parts[0] if parts else "?", "date": parts[1] if len(parts) > 1 else "?",
            "subject": parts[2] if len(parts) > 2 else "?"}


def cpu() -> dict:
    flags = set()
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("flags"):
                    flags = set(line.split(":", 1)[1].split())
                    break
    except OSError:
        pass
    name = "?"
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    name = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    mem_mb = 0
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    mem_mb = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    gpus = sh(["lspci"], timeout=15)
    gpu = next((ln.split(": ", 1)[-1].strip() for ln in gpus.splitlines()
                if re.search(r"vga|3d|display", ln, re.I)), "sin GPU discreta")
    return {"name": name, "cores": os.cpu_count() or 0, "mem_total_mb": mem_mb,
            "gpu": gpu, "avx2": "avx2" in flags, "avx512": "avx512f" in flags,
            "os": f"{platform.system()} {platform.release()}", "arch": platform.machine()}


def python_pkgs() -> dict:
    out = {}
    for mod, name in (("torch", "torch"), ("ultralytics", "ultralytics"), ("fastapi", "fastapi"),
                      ("uvicorn", "uvicorn"), ("numpy", "numpy"), ("pyarrow", "pyarrow")):
        try:
            m = __import__(mod)
            out[name] = getattr(m, "__version__", "?")
        except Exception:                          # noqa: BLE001
            out[name] = None
    return out


def main() -> int:
    data = {
        "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds"),
        "cpu": cpu(),
        "llama_cpp": llama_version(),
        "transcribe_cpp": git_commit(os.path.join(ROOT, "vendor", "transcribe.cpp")),
        "python": sys.version.split()[0],
        "packages": python_pkgs(),
        "python_venv": os.path.relpath(os.path.join(ROOT, ".venv"), ROOT),
        "weights_dir": "models/",
    }
    with open(OUT, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    print(f"→ {OUT}")
    print(json.dumps({k: data[k] for k in ("cpu", "llama_cpp", "transcribe_cpp", "python")},
                     indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
