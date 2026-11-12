#!/usr/bin/env python3
"""Deja a `pi` listo para usar los modelos locales del hub SURUS.

Mergea un provider `surus-local` (OpenAI-compatible) en ~/.pi/agent/models.json
sin tocar los providers que ya tengas. Hace un .bak la primera vez.

Uso:
  python3 scripts/pi_setup.py                 # agrega/actualiza surus-local
  python3 scripts/pi_setup.py --model lfm3-1.7b   # apunta al modelo grande
  python3 scripts/pi_setup.py --remove
Después:
  pi --provider surus-local --model mnn-pocket
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

HUB = os.environ.get("SURUS_HUB", "http://127.0.0.1:8180")
PATH = os.path.expanduser(os.environ.get("PI_MODELS_JSON", "~/.pi/agent/models.json"))


def provider(model: str) -> dict:
    # El hub hace dispatch por el campo "model" del request: un solo provider
    # sirve para los dos modelos (pi elige con --model).
    return {
        "baseUrl": f"{HUB}/v1",
        "api": "openai-completions",
        "apiKey": "local",
        "models": [
            {"id": "mnn-pocket", "input": ["text"]},
            {"id": "lfm3-1.7b", "input": ["text"]},
        ],
        "note": "el hub carga el modelo que pidas, si entra en el presupuesto de RAM",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="mnn-pocket",
                    help="modelo al que apunta baseUrl por defecto (mnn-pocket | lfm3-1.7b)")
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(os.path.dirname(PATH)):
        print(f"no existe {os.path.dirname(PATH)}: parece que pi no está configurado.", file=sys.stderr)
        return 1

    data = {}
    if os.path.exists(PATH):
        with open(PATH) as fh:
            data = json.load(fh)
        if not args.remove and not os.path.exists(PATH + ".surus.bak"):
            shutil.copy2(PATH, PATH + ".surus.bak")
    provs = data.setdefault("providers", {})

    if args.remove:
        if provs.pop("surus-local", None) is None:
            print("no había un provider surus-local.")
            return 0
    else:
        provs["surus-local"] = provider(args.model)

    tmp = PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, PATH)
    print(f"listo: {PATH}")
    if not args.remove:
        print(f"  baseUrl → {HUB}/v1   (dispatch por modelo)")
        print("  probalo con:  pi --provider surus-local --model lfm3-1.7b --no-tools "
              "--no-session -p \"¿qué es el prefill?\"\n"
              "              pi --provider surus-local --model mnn-pocket --no-tools "
              "--no-session -p \"hola\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
