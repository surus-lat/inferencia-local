#!/usr/bin/env python3
"""
build_index.py — Demo 4 · embeddings + búsqueda semántica (UNRN/ri-conicet).

Lee `data/metadata.parquet` en STREAMING por row groups con pyarrow (nunca carga
las 294.595 filas completas: pide solo las columnas necesarias y se corta de leer
en cuanto alcanza el scope pedido) y deja en `--out`:

  vectors.f32.bin   matriz C-contiguous float32 (N x 768) pensada para np.memmap
  meta.jsonl.gz     por fila indexada: {item_id,title,authors,year,type,language,pdf}
  index.json        {n, dims, built_at, source, rows_total, skipped_no_embedding,
                     scopes:{p1,p5,p25,full}} + extras de trazabilidad
  skipped.jsonl.gz  filas DESCARTADAS (embedding None / largo != 768 / norma ~0),
                    una por línea: {row,item_id,reason}

Decisiones medidas sobre este parquet (verificado, no supuesto):
  * 294.595 filas, 59 row groups (5.000; el último 4.595). 0 filas deleted=True.
  * 22.615 filas (7,68%) tienen `embedding` NULL → van a skipped.jsonl.gz.
  * Los vectores del parquet NO vienen todos L2-normalizados: norma mediana 0.243,
    p25 0.224 (= 1/sqrt(20)), p75 0.577 y solo ~16,6% con norma exacta 1.0. El
    promedio de chunks normalizados no renormaliza el resultado. Por eso este build
    L2-normaliza cada fila al escribirla (`normalized: true` en index.json): así el
    hub puede hacer cosine == dot product sin dividir por normas por fila.

Uso (venv del proyecto, python3.12):
  ROOT/.venv/bin/python ROOT/scripts/build_index.py --fraction 0.01 --out ROOT/data/index
  ROOT/.venv/bin/python ROOT/scripts/build_index.py --fraction 1.0  --out ROOT/data/index_full

Memoria: el build no acumula la matriz en RAM; cada row group se convierte, se
escribe al .bin y se libera. Pico medido ~200 MB en el build completo (ver README
de la demo / mensaje del informe).
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]

DIMS = 768
SOURCE = "UNRN/ri-conicet"
MODEL = "embeddinggemma-2-Q8_0"
TITLE_MAX_CHARS = 300
AUTHORS_MAX = 8
# Columnas que realmente se leen: description/subjects/publisher/rights/formats/
# fetched_at se descartan en la lectura parquet (es lo que ahorra RAM).
COLS = ["item_id", "title", "creators", "date", "type", "language", "pdf", "embedding"]
SCOPE_PCT = {"p1": 0.01, "p5": 0.05, "p25": 0.25, "full": 1.0}

_NUM_RE = re.compile(r"\d+")


def pick_year(date_val) -> int | None:
    """Año = primer token numérico de `date` que sea un año plausible.

    Ejemplos reales del dataset: '2016-12' -> 2016, '2019-03-27' -> 2019,
    '2011' -> 2011, '12-2016' -> 2016, 's.f.' -> None.
    """
    if not date_val:
        return None
    for tok in _NUM_RE.findall(str(date_val)):
        year = int(tok[:4]) if len(tok) >= 4 else int(tok)
        if 800 <= year <= 2100:
            return year
    return None


def clean_title(val) -> str:
    if not val:
        return ""
    txt = " ".join(str(val).split())
    return txt[:TITLE_MAX_CHARS]


def clean_authors(val) -> list[str]:
    if not val:
        return []
    out = []
    for a in list(val)[:AUTHORS_MAX]:
        if a:
            s = " ".join(str(a).split())
            if s:
                out.append(s)
    return out


def list_col_to_flat(arr: pa.ChunkedArray | pa.Array):
    """ChunkedArray<float> de listas -> vista numpy del buffer plano + offsets.

    Cero copias para el plano (np.frombuffer sobre el buffer float de Arrow);
    la única copia es el gather de las filas válidas.
    """
    la = arr.combine_chunks() if isinstance(arr, pa.ChunkedArray) else arr
    vals = la.values
    voff = vals.offset
    buf = vals.buffers()[1]
    flat = np.frombuffer(buf, dtype=np.float32) if buf is not None else np.zeros(0, np.float32)
    offs = la.offsets.to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
    nrows = len(la)
    starts = offs[:nrows] - voff
    ends = offs[1:1 + nrows] - voff
    if la.null_count:
        valid = np.asarray(la.is_valid().to_numpy(zero_copy_only=False), dtype=bool)
    else:
        valid = np.ones(nrows, dtype=bool)
    return flat, starts, ends, valid


def build(parquet_path: Path, out_dir: Path, scan_rows: int, max_rows: int | None) -> dict:
    pf = pq.ParquetFile(parquet_path)
    rows_total = pf.metadata.num_rows
    n_rgs = pf.metadata.num_row_groups

    limit = scan_rows if max_rows is None else min(scan_rows, max_rows)
    limit = min(limit, rows_total)

    out_dir.mkdir(parents=True, exist_ok=True)
    bin_path = out_dir / "vectors.f32.bin"
    meta_path = out_dir / "meta.jsonl.gz"
    idx_path = out_dir / "index.json"
    skip_path = out_dir / "skipped.jsonl.gz"

    n = 0
    scanned = 0
    skipped_no_embedding = 0
    skipped_bad = 0
    renormalized = 0
    norm_min_before = 1.0
    norm_max_before = 0.0

    t0 = time.perf_counter()
    bin_fh = bin_path.open("wb")
    meta_fh = gzip.open(meta_path, "wt", encoding="utf-8", compresslevel=6)
    skip_fh = gzip.open(skip_path, "wt", encoding="utf-8", compresslevel=6)
    try:
        for rg in range(n_rgs):
            table = pf.read_row_group(rg, columns=COLS)
            if scanned + table.num_rows > limit:
                table = table.slice(0, limit - scanned)
            scanned += table.num_rows

            emb = table.column("embedding")
            flat, starts, ends, valid = list_col_to_flat(emb)
            lengths = ends - starts
            ok = valid & (lengths == DIMS)

            item_ids = table.column("item_id").to_pylist()
            # --- filas descartadas (orden parquet, estable) ---
            for i in range(table.num_rows):
                if not ok[i]:
                    reason = "no_embedding" if (not valid[i] or lengths[i] == 0) else "bad_length"
                    if reason == "no_embedding":
                        skipped_no_embedding += 1
                    else:
                        skipped_bad += 1
                    skip_fh.write(
                        json.dumps({"row": scanned - table.num_rows + i,
                                    "item_id": item_ids[i], "reason": reason},
                                   ensure_ascii=False) + "\n"
                    )
                    ok[i] = False

            take = np.flatnonzero(ok)
            if take.size:
                starts_ok = starts[take]
                # gather de la submatriz (copia única, luego se libera)
                mat = flat[starts_ok[:, None] + np.arange(DIMS, dtype=np.int64)[None, :]]
                mat = np.ascontiguousarray(mat, dtype=np.float32)
                norms = np.sqrt(np.einsum("ij,ij->i", mat, mat))
                good = np.isfinite(norms) & (norms > 1e-6)
                if not good.all():
                    for i in take[~good]:
                        skipped_bad += 1
                        skip_fh.write(json.dumps(
                            {"row": scanned - table.num_rows + int(i),
                             "item_id": item_ids[int(i)], "reason": "zero_or_nan"},
                            ensure_ascii=False) + "\n")
                    mat = mat[good]
                    norms = norms[good]
                    take = take[good]
                norm_min_before = min(norm_min_before, float(norms.min()))
                norm_max_before = max(norm_max_before, float(norms.max()))
                renormalized += int(np.count_nonzero(np.abs(norms - 1.0) > 1e-3))
                # L2-normalizar: cosine == dot product en el buscador
                mat /= norms[:, None]
                bin_fh.write(mat.tobytes(order="C"))
                n += mat.shape[0]
                del mat, norms

            titles = table.column("title").to_pylist()
            creators = table.column("creators").to_pylist()
            dates = table.column("date").to_pylist()
            types = table.column("type").to_pylist()
            langs = table.column("language").to_pylist()
            pdfs = table.column("pdf").to_pylist()
            w = meta_fh.write
            for i in take:
                j = int(i)
                w(json.dumps(
                    {"item_id": item_ids[j],
                     "title": clean_title(titles[j]),
                     "authors": clean_authors(creators[j]),
                     "year": pick_year(dates[j]),
                     "type": (types[j] or ""),
                     "language": (langs[j] or ""),
                     "pdf": (pdfs[j] or None)},
                    ensure_ascii=False) + "\n")
            del table, emb, flat, starts, ends, valid, lengths, take
            if scanned >= limit:
                break
    finally:
        bin_fh.close()
        meta_fh.close()
        skip_fh.close()
    dt = time.perf_counter() - t0

    scopes_dataset = {k: min(rows_total, max(1, int(round(rows_total * v))))
                      for k, v in SCOPE_PCT.items()}
    # scopes como filas del índice (scan_rows se aplica sobre vectors.f32.bin)
    scopes = {k: min(v, n) for k, v in scopes_dataset.items()}

    info = {
        "n": n,
        "dims": DIMS,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": SOURCE,
        "rows_total": rows_total,
        "skipped_no_embedding": skipped_no_embedding,
        "scopes": scopes,
        # --- extras (informativos, el hub puede ignorarlos) ---
        "scopes_dataset": scopes_dataset,
        "dtype": "float32",
        "normalized": True,
        "model": MODEL,
        "rows_scanned": scanned,
        "fraction_requested": round(scan_rows / rows_total, 6),
        "skipped_bad_shape": skipped_bad,
        "norm_before_min": round(norm_min_before, 4),
        "norm_before_max": round(norm_max_before, 4),
        "rows_renormalized": renormalized,
        "vectors_bytes": bin_path.stat().st_size,
        "title_max_chars": TITLE_MAX_CHARS,
        "authors_max": AUTHORS_MAX,
        "parquet": parquet_path.name,
        "elapsed_s": round(dt, 2),
    }
    idx_path.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return info


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build dense index for Demo 4 (RI CONICET).")
    ap.add_argument("--parquet", default=str(ROOT / "data" / "metadata.parquet"))
    ap.add_argument("--fraction", type=float, default=0.01,
                    help="fracción de filas fuente a escanear (default 0.01)")
    ap.add_argument("--out", default=str(ROOT / "data" / "index"))
    ap.add_argument("--max-rows", type=int, default=None,
                    help="tope duro de filas fuente (pisa a --fraction)")
    args = ap.parse_args(argv)

    if not (0 < args.fraction <= 1):
        print("fraction debe estar en (0, 1]", file=sys.stderr)
        return 2

    pq_path = Path(args.parquet)
    if not pq_path.exists():
        print(f"no existe {pq_path}", file=sys.stderr)
        return 2
    out_dir = Path(args.out)

    t0 = time.perf_counter()
    info = build(pq_path, out_dir, int(round(args.fraction * pq.ParquetFile(pq_path).metadata.num_rows)),
                 args.max_rows)
    info["wall_s"] = round(time.perf_counter() - t0, 2)
    idx = out_dir / "index.json"
    idx.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    try:
        import resource
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        rss_mb = float("nan")
    print(json.dumps({
        "out": str(out_dir), "n": info["n"], "rows_scanned": info["rows_scanned"],
        "skipped_no_embedding": info["skipped_no_embedding"],
        "skipped_bad_shape": info["skipped_bad_shape"],
        "vectors_MB": round(info["vectors_bytes"] / 1e6, 1),
        "meta_gz_MB": round((out_dir / "meta.jsonl.gz").stat().st_size / 1e6, 2),
        "wall_s": info["wall_s"], "max_rss_MB": round(rss_mb, 1),
        "scopes": info["scopes"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
