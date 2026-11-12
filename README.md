# Inferencia local · 5 demos en tu laptop

Demos de la clase **"Inferencia local: tu hardware, tus reglas"** (SURUS). Todo corre
en **CPU**, en tu máquina, sin nube: una sola webapp raíz (`127.0.0.1:8180`) lanza
cinco demos y deja **cargar o descargar cada modelo a pedido**, con un presupuesto de
RAM que evita que la laptop se ahogue.

Laptop de referencia: CPU x86_64 de 4 núcleos / 8 hilos, ~14 GB de RAM, **sin GPU**
(sólo gráficos integrados). Con ~10 GB ya usados por otras aplicaciones quedan
~4,5 GB libres: por eso hay un presupuesto de RAM.

## Prender y apagar

```bash
cd inferencia-local-demos
python3 scripts/freeze_versions.py        # congela las versiones reales de TU máquina
scripts/run_hub.sh                        # hub + 5 webapps en http://127.0.0.1:8180
python3 .venv/bin/python scripts/smoke.py # prueba las 5 demos con pesos reales
```

Ctrl-C apaga el hub y **descarga todos los modelos** (los subprocessos de
`llama-server` y el worker ASR mueren con él). No queda nada corriendo.

Variables útiles (ya tienen default sensato en `scripts/run_hub.sh`):

| variable | default | qué hace |
|---|---|---|
| `DSH_RAM_BUDGET_MB` | `4096` | techo de RAM para modelos a la vez |
| `DSH_THREADS` | `4` | hilos de llama.cpp, parakeet y torch |
| `DSH_CTX_LLM` | `16384` | contexto de los LLM (bajalo a `8192` si no usás agentes con tools) |
| `OPENBLAS_NUM_THREADS` | `2` | gana al default de 8 hilos en el escaneo de coseno |

## Las cinco demos

| demo | ruta | modelo | engine | qué muestra |
|---|---|---|---|---|
| Dedos en vivo | `/d/dedos` | OneJev 1.6 · 0.8B `Q4_K_M` + mmproj `Q8_0` | llama.cpp (mtmd) | la webcam y el conteo de dedos (0-5) del modelo de decisión |
| LLM servido local | `/d/llm` | LFM3 · 1.7B `Q4_K_M` y MNN-Pocket · 0.3B `Q4_1` | llama.cpp | chat con los dos y el mismo endpoint listo para `pi` |
| Transcripción en vivo | `/d/asr` | Parakeet Ultra · 0.6B `Q4_K_M` | transcribe.cpp | VAD en el navegador + ventanas → transcripto acumulado |
| Búsqueda semántica | `/d/search` | EmbeddingGemma-2 `Q8_0` | llama.cpp `--embedding` | consulta embebida contra el corpus del CONICET |
| Webcam con YOLO26 | `/d/yolo` | YOLO26 `n` / `s` / `m` | ultralytics + torch CPU | 80 clases COCO dibujadas sobre la webcam |

La raíz `/` es el **launcher**: switch de carga por modelo, RAM usada vs presupuesto,
log de cada backend y acceso a las demos. Cada demo trae un botón **stack y recursos**:
un diagrama de las 4 capas (app → engine → kernels/runtime → metal) con las versiones
reales, el comando exacto con que se sirve ese modelo, las rutas relativas de sus
archivos y un monitor de RAM/carga que sólo sondea cuando está abierto. Si una carga no entra en el presupuesto,
avisa cuánto falta en vez de mandar todo a swap.

## Mediciones reales (CPU, sin GPU)

Números de referencia (con la laptop tranquila; si hay otras apps, empeoran):

- **Prefill de una imagen** (OneJev, 96 tokens de imagen): ~2,1-2,6 s · con la misma
  imagen repetida ~0,15 s gracias al KV cache. El costo está
  en el prefill, no en generar el dígito.
- **LFM3 1.7B Q4**: ~11 tok/s de decode, ~40 tok/s de prefill. Es modelo *thinking*:
  razona en `reasoning_content` y después responde.
- **MNN-Pocket 0.3B Q4**: ~44-56 tok/s. La diferencia de tamaño → velocidad, en vivo.
- **Parakeet Ultra Q4**: 5,7-7× tiempo real (0,9-1,0 s para 5,9 s de audio), con
  timestamps y probabilidad por token. El modelo es offline por chunks, así que el
  efecto "en vivo" lo arma el navegador con VAD.
- **EmbeddingGemma-2 Q8**: 768 dims, L2-normalizado. Escaneo coseno sobre
  **271.980** publicaciones: ~15 ms en el 1%, ~70-200 ms en el 100% (2 s la primera
  pasada fría por el page cache). Buscar **con una foto** pasa por el proyector de
  visión (`mmproj`) y cuesta **~12 s** por imagen en esta CPU: a 128 tokens de imagen
  baja a ~5 s pero la recuperación empeora (por un colectivo devuelve "probióticos").
- **YOLO26s a 640**: ~160 ms/frame (≈6 FPS). El primer frame con un peso nuevo paga la carga.

## Búsqueda semántica: RI CONICET

`UNRN/ri-conicet` (snapshot de CONICET Digital, 294.595 registros) trae una columna
`embedding` de 768 dims ya calculada con EmbeddingGemma-2. Se bajó el parquet completo
(1,03 GB) y se construyeron índices en `data/`:

- `data/index/` — 1% (2.913 vectores, 8,9 MB)
- `data/index_full/` — 100% (271.980 vectores con embedding, 824 MB, leídos con `mmap`)

`data/index` es **prefijo exacto** de `index_full`: pasar de 1% a 100% en la UI no
cambia los resultados ni reconstruye nada. Ojo: los vectores del parquet **no** venían
normalizados (normas de 0,149 a 1,0), así que `scripts/build_index.py` los L2-normaliza
al escribir el `.bin` y por eso coseno = producto punto.

```bash
.venv/bin/python scripts/build_index.py --fraction 1.0 --out data/index_full
```

## Usarlo desde `pi`

Un solo endpoint compatible OpenAI, que **elige el backend según el campo `model`** y
**carga el modelo si entra en el presupuesto de RAM**:

```
POST http://127.0.0.1:8180/v1/chat/completions
GET  http://127.0.0.1:8180/v1/models          # lista los modelos y si están cargados
```

Dejalo registrado en `pi` una vez:

```bash
python3 scripts/pi_setup.py            # agrega el provider "surus-local" a ~/.pi/agent/models.json
pi --provider surus-local --model lfm3-1.7b --no-session -p "¿qué es el prefill?"
pi --provider surus-local --model mnn-pocket --no-session -p "¿por qué cuantizar acelera?"
```

Con tools y sin `--no-tools`: el system prompt de un agente con todas sus tools ya
ocupa ~10.100 tokens, por eso el default de contexto es `16384` (paga ~460 MB extra
de KV cache). Si no usás tools, `DSH_CTX_LLM=8192` alcanza y gasta menos RAM.

Si el modelo no entra en el presupuesto, el proxy devuelve `409` con `reason:"over_ram"`
y cuántos MB faltan, en vez de mandar la laptop a swap.

## Dónde está cada cosa

```
models/            pesos GGUF (.gguf) + yolo26s.pt          2,7 GB
data/              metadata.parquet (1,03 GB) + índices
vendor/llama.cpp   engine de LLM/VLM/embeddings (build nativo, Release)
vendor/transcribe.cpp + vendor/asrtool/live_asr.cpp   worker ASR persistente
hub/               server.py (FastAPI) · backends.py (carga/RAM) · asr_worker.py · search_index.py
                   versions.json (lo genera scripts/freeze_versions.py; es de TU máquina)
web/               launcher + 5 páginas + assets (tokens, fuentes, logos)
scripts/           run_hub.sh · smoke.py · pull_models.sh · build_index.py · pi_setup.py
docs/SPEC.md       contrato de API que siguen las páginas
```

Todo vive dentro del workspace: ninguna caché (`uv`, `TMPDIR`, `YOLO_CONFIG_DIR`,
`HF_HOME`) escribe en tu HOME.
