#!/usr/bin/env python3
"""
search_index.py — motor de búsqueda semántica para la Demo 4 (RI CONICET).

Lo importa el hub (`hub/server.py`):

    from search_index import SearchIndex
    idx = SearchIndex(ROOT / "data" / "index_full")
    idx.load(2946)                                  # scope 1% (mmap, no carga todo)
    res = idx.search(query_vec, k=10, scan_rows=2946)   # query_vec: 768 floats

Formato esperado en `root_dir` (lo genera `scripts/build_index.py`):
  vectors.f32.bin  float32 C-contiguous (n x 768), L2-normalizado -> cosine == dot
  meta.jsonl.gz    una línea por fila indexada: {item_id,title,authors,year,type,
                   language,pdf}
  index.json       {n, dims, built_at, source, rows_total, skipped_no_embedding,
                    scopes:{p1,p5,p25,full}, ...}

Claves del diseño (RAM de esta máquina: ~4,8 GB disponibles):
  * Los vectores NUNCA se cargan a RAM: np.memmap sobre vectors.f32.bin. El kernel
    pasa páginas a page cache bajo demanda y se pueden reaprovechar entre consultas.
    El índice completo son 904 MB de vectores: residentes sólo mientras se escanean.
  * El escaneo va por LOTES (default 16.384 filas = 50 MB) con BLAS sgemm vía
    `np.dot(lote, q, out=scores[i:j])`. El buffer de scores es float32 de
    `scan_rows` (1,2 MB en el peor caso). No se materializa (n x n) ni se copia
    la matriz completa.
  * La metadata se guarda como UN buffer de bytes descomprimido + offsets de línea
    (numpy int64), y sólo se hace json.loads de las k filas devueltas.

MEDICIÓN DE REFERENCIA (CPU x86_64 4C/8T, DDR4, sin GPU; numpy 2.5.4 sobre
scipy-openblas64; índice completo = 271.980 filas x 768 float32 = 836 MB mmap;
batch default 32.768 filas; top-k con argpartition; `scan_ms` = sólo el lazo de
dot, sin embed de la query; mediana de 7 pasadas con page cache caliente):

    filas escaneadas            OpenBLAS default   OPENBLAS_NUM_THREADS=4   =2
    ---------------------------------------------------------------------------
      2.946   (1%)                 7,2 ms  (min 2,8)      0,8 ms          0,7 ms
     14.730   (5%)                 9,3 ms  (min 4,2)      4,0 ms          4,3 ms
     73.649   (25%)               35,1 ms  (min 27,0)    22 ms           20 ms
    271.980   (100% real, full)  104,4 ms  (min 82,1)    88 ms           74 ms
    294.595   (100% del dataset)  = lo mismo: se recorta a 271.980 filas

  (el default de numpy acá son 8 threads = las 8 hyperthreads; por eso la columna
  "default" es la más lenta y la más ruidosa: la laptop tenía ~12 GB usados por
  otras apps durante la medición).

  Primera pasada con las páginas FRÍAS (tras `posix_fadvise(fd, ..., DONTNEED)`):
  177 ms del completo, y 96 ms la segunda. O sea traer los 836 MB desde disco
  cuesta ~80 ms extra; se puede pagar una sola vez con `prefault()`.

  REGLA PRÁCTICA: para este scan (gemv 768-dim, cuello de botella de memoria y no
  de cómputo) 8 threads es LO PEOR: el overhead domina. Arrancar el hub con
  `OPENBLAS_NUM_THREADS=2` (o 4) baja el scan del 1% de ~7 ms a ~0,8 ms y el
  completo a ~74-88 ms, y deja cores para llama-server. Este build de numpy no
  exporta `openblas_set_num_threads` (símbolos strippeados, probado con ctypes),
  así que la variable hay que ponerla ANTES de importar numpy: no sirve setearla
  desde `search_index.py`.

  Costos de proceso (medidos en /proc/self/status, índice completo):
    load(): 0,86 s (gunzip de 27,9 MB -> 90,9 MB de metadata + 2,2 MB de offsets),
            RSS 119 MB (102 MB anónimos).
    tras escanear el 100%: RSS 1.011 MB, pero 817 MB son RssFile = páginas del mmap
            (page cache recuperable); lo anónimo propio queda en ~193 MB.
  Con eso, y el presupuesto de ~4 GB de modelos, el buscador no compite por RAM.
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path

import numpy as np

DEFAULT_BATCH_ROWS = 32768  # 32.768 x 768 x 4B = 100 MB de páginas del mmap por lote


class IndexNotFound(FileNotFoundError):
    pass


class SearchIndex:
    """Índice denso en mmap + metadata decodificada sólo para las k filas.

    Thread-safe para leer: `load()` una vez y luego `search()` desde los hilos
    del threadpool de FastAPI (el mmap es de sólo lectura y `scores` es local).
    """

    def __init__(self, root_dir, batch_rows: int = DEFAULT_BATCH_ROWS):
        self.root = Path(root_dir)
        self.batch_rows = int(batch_rows)
        self.info: dict = {}
        self.n = 0
        self.dims = 0
        self._mm: np.memmap | None = None
        self._fh = None
        self._meta_buf: bytes | None = None
        self._meta_off: np.ndarray | None = None
        self.scope_rows: int = 0
        self.last_scan_ms = 0.0

    # ------------------------------------------------------------------ lifecycle
    def load(self, scope_rows: int | None = None) -> "SearchIndex":
        """Mapea vectors.f32.bin y lee los offsets de meta.jsonl.gz.

        `scope_rows` fija cuántas filas quedan disponibles por defecto para
        `search()` (se recorta a `n`). No copia los vectores a RAM: sólo mmap.
        Idempotente: recargar con otro scope no vuelve a mapear.
        """
        idx_path = self.root / "index.json"
        bin_path = self.root / "vectors.f32.bin"
        meta_path = self.root / "meta.jsonl.gz"
        for p in (idx_path, bin_path, meta_path):
            if not p.exists():
                raise IndexNotFound(f"falta {p} (corré scripts/build_index.py)")

        self.info = json.loads(idx_path.read_text(encoding="utf-8"))
        self.dims = int(self.info["dims"])
        self.n = int(self.info["n"])
        expect_bytes = self.n * self.dims * 4
        if bin_path.stat().st_size < expect_bytes:
            raise IndexNotFound(f"{bin_path} truncado: "
                                f"{bin_path.stat().st_size} < {expect_bytes} bytes")

        if self._mm is None:
            self._fh = open(bin_path, "rb")
            self._mm = np.memmap(bin_path, dtype=np.float32, mode="r",
                                 shape=(self.n, self.dims))
        if self._meta_off is None:
            with gzip.open(meta_path, "rb") as fh:
                self._meta_buf = fh.read()
            nl = np.frombuffer(self._meta_buf, dtype=np.uint8)
            starts = np.flatnonzero(nl == 0x0A).astype(np.int64) + 1
            self._meta_off = np.concatenate((np.zeros(1, dtype=np.int64), starts))

        want = self.n if scope_rows is None else int(scope_rows)
        self.scope_rows = int(max(0, min(want, self.n)))
        return self

    def close(self) -> None:
        self._mm = None
        self._meta_buf = None
        self._meta_off = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def prefault(self, scan_rows: int | None = None) -> float:
        """Trae las páginas del rango al page cache (primera consulta más rápida)."""
        self._ensure()
        rows = self.scope_rows if scan_rows is None else self._clamp(scan_rows)
        t0 = time.perf_counter()
        acc = np.zeros(self.dims, dtype=np.float32)
        for a in range(0, rows, self.batch_rows):
            b = min(a + self.batch_rows, rows)
            acc += np.asarray(self._mm[a:b]).sum(axis=0)
        return (time.perf_counter() - t0) * 1000.0

    # ------------------------------------------------------------------ search
    def _clamp(self, scan_rows) -> int:
        if scan_rows is None:
            return self.scope_rows
        return int(max(0, min(int(scan_rows), self.n)))

    def _ensure(self) -> None:
        if self._mm is None:
            raise RuntimeError("SearchIndex.load() no fue llamado")

    def search(self, query_vec, k: int = 10, scan_rows: int | None = None) -> list[dict]:
        """Top-k por similitud coseno sobre las primeras `scan_rows` filas.

        Los vectores del índice ya están L2-normalizados (build_index.py), así que
        cosine == producto punto; igualmente normalizamos la query por seguridad.
        Devuelve dicts con item_id, title, authors, year, type, language, pdf,
        score (y rank 1-based). `self.last_scan_ms` trae el tiempo del escaneo.
        """
        self._ensure()
        q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        if q.size != self.dims:
            raise ValueError(f"query_vec tiene {q.size} dims, se esperan {self.dims}")
        nrm = float(np.linalg.norm(q))
        if not np.isfinite(nrm) or nrm < 1e-9:
            raise ValueError("query_vec es cero o no finita")
        q = q / nrm

        rows = self._clamp(scan_rows)
        k = max(1, min(int(k), rows)) if rows else 1
        scores = np.empty(rows, dtype=np.float32)
        t0 = time.perf_counter()
        for a in range(0, rows, self.batch_rows):
            b = min(a + self.batch_rows, rows)
            # BLAS sgemm sobre el lote del mmap, escribiendo directo en scores
            np.dot(np.asarray(self._mm[a:b]), q, out=scores[a:b])
        self.last_scan_ms = (time.perf_counter() - t0) * 1000.0
        if not rows:
            return []

        top = np.argpartition(scores, -k)[-k:] if k < rows else np.arange(rows)
        top = top[np.argsort(-scores[top], kind="stable")]
        out = []
        for rank, i in enumerate(top, 1):
            row = self._meta(int(i))
            row["score"] = round(float(scores[int(i)]), 6)
            row["rank"] = rank
            out.append(row)
        return out

    def row_vector(self, i: int) -> np.ndarray:
        """Copia float32 del vector de la fila i (útil para self-tests)."""
        self._ensure()
        return np.array(self._mm[int(i)], dtype=np.float32)

    # alias histórico
    nearest_row_vector = row_vector

    # ------------------------------------------------------------------ metadata
    def _meta(self, i: int) -> dict:
        if self._meta_off is None or self._meta_buf is None:
            return {}
        if not (0 <= i < len(self._meta_off)):
            return {"item_id": None, "title": "", "authors": [], "year": None,
                    "type": "", "language": "", "pdf": None}
        a = int(self._meta_off[i])
        b = int(self._meta_off[i + 1]) if i + 1 < len(self._meta_off) else len(self._meta_buf)
        try:
            return json.loads(self._meta_buf[a:b])
        except Exception:
            return {"item_id": None, "title": "", "authors": [], "year": None,
                    "type": "", "language": "", "pdf": None}

    def row_meta(self, i: int) -> dict:
        """Metadata de la fila i sin buscar (para armar handle/pdf_url en el hub)."""
        return self._meta(int(i))

    # ------------------------------------------------------------------ config
    def config(self) -> dict:
        """Payload para `GET /api/search/config` (ver docs/SPEC.md, Demo 4)."""
        scopes = dict(self.info.get("scopes", {}))
        return {
            "rows_total": int(self.info.get("rows_total", 0)),
            "indexed_rows": self.n,
            "dims": self.dims,
            "model": self.info.get("model", "embeddinggemma-2-Q8_0"),
            "scopes": scopes,
            "corpus": self.info.get("source", "UNRN/ri-conicet"),
            # extras informativos para la UI (no rompen el contrato)
            "loaded_scope": self.scope_rows,
            "normalized": bool(self.info.get("normalized", True)),
            "built_at": self.info.get("built_at"),
            "vectors_MB": round(int(self.info.get("vectors_bytes", 0)) / 1e6, 1),
            "index_dir": str(self.root),
        }

    # ------------------------------------------------------------------ bench
    def scan_bench(self, rows_list, reps: int = 3, k: int = 10) -> list[dict]:
        """Mide el scan puro (sin embed de query) con una query sintética."""
        self._ensure()
        rng = np.random.default_rng(7)
        q = rng.standard_normal(self.dims).astype(np.float32)
        q /= np.linalg.norm(q)
        out = []
        for rows in rows_list:
            rows = self._clamp(rows)
            best = float("inf")
            self.search(q, k=k, scan_rows=rows)  # warm
            for _ in range(reps):
                self.search(q, k=k, scan_rows=rows)
                best = min(best, self.last_scan_ms)
            out.append({"rows": rows, "ms_best": round(best, 2),
                        "ms_per_1k": round(best / max(rows, 1) * 1000, 3)})
        return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Self-test / bench de SearchIndex.")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1] / "data" / "index"))
    ap.add_argument("--query-row", type=int, default=0,
                    help="fila del índice usada como query (self-test score ~1)")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--bench", default="",
                    help="lista separada por comas de scopes a benchmarkear")
    args = ap.parse_args(argv)

    idx = SearchIndex(args.root).load()
    print(json.dumps(idx.config(), ensure_ascii=False, indent=2))
    q = idx.nearest_row_vector(args.query_row)
    res = idx.search(q, k=args.k, scan_rows=idx.n)
    print(f"\nquery = fila {args.query_row}  scan_ms={idx.last_scan_ms:.2f}")
    for r in res:
        print(f"  #{r['rank']} {r['score']:.4f}  {r['item_id']}  {r['title'][:70]}  ({r['year']})")
    if args.bench:
        rows = [int(x) for x in args.bench.split(",") if x.strip()]
        print("\nscan bench (ms del escaneo puro, mejor de 3):")
        for b in idx.scan_bench(rows):
            print(f"  {b['rows']:>7} filas  {b['ms_best']:>8.2f} ms  ({b['ms_per_1k']:.3f} ms/1k)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
