# Inferencia Local · SURUS — contrato de demos locales

Proyecto: `inferencia-local-demos/` (raíz = `ROOT`).
Laptop: i5-10310U (4C/8T), 14 GB RAM, **sin GPU**, CPU-only. Todo debe correr en CPU.

## Principios

1. **Un solo hub** (FastAPI, `hub/server.py`) en `127.0.0.1:8180`. Sirve las 5 webapps
   **y** todas las APIs. El navegador nunca habla con los backends: siempre mismo origen, sin CORS.
2. **Modelos seleccionables**: cada modelo se carga/descarga a pedido desde el launcher.
   Hay un **presupuesto de RAM**; si una carga lo excede, se rechaza con explicación.
   RAM disponible real al inicio de la sesión: ~4.8 GB (hay ~10 GB usados por otras apps).
   Regla: no más de ~4 GB en modelos a la vez.
3. **Estética SURUS**: usar `web/assets/surus/tokens.css` (tokens `--surus-*`) y `fonts.css`.
   Ink `#121A20`, Deep `#182128`, Line `#2C3740`, Aqua `#7FE3CD` (una sola idea destacada por
   headline), Inter light para títulos, JetBrains Mono para datos/eyebrows, radius 3px, sin sombras,
   sin rojos ni verdes. Español con voseo y tildes. Nada de redbluismo ni glassmorphism.
4. **Honestidad de medición**: cada demo muestra latencia real (prefill / decode / tok-per-s /
   RTF), porque la clase es de inferencia local. Ver `deck-inferencia-local-uba(19).html` para
   el contexto pedagógico (TTFT, TPOT, prefill vs decode, cuantización).

## Backends y puertos (todos 127.0.0.1)

| id | rol | weights | engine | puerto | RAM est. |
|---|---|---|---|---|---|
| `onejev-vlm` | VLM dedos (System 1) | `models/OneJev1.6-0.8B.Q4_K_M.gguf` + `OneJev1.6-0.8B.mmproj-Q8_0.gguf` | llama-server | 8181 | 1150 MB |
| `lfm3-1.7b` | LLM chat/pi | `models/LFM3-1.7B-Q4_K_M.gguf` | llama-server | 8182 | 1800 MB |
| `mnn-pocket` | LLM rápido chat/pi | `models/MNN-Pocket-0.3B-Q4_1.gguf` | llama-server | 8183 | 450 MB |
| `parakeet-ultra` | ASR en vivo | `models/parakeet-ultra-Q4_K_M.gguf` | worker C++ propio (stdin/stdout JSON) | – | 900 MB |
| `embeddinggemma` | embeddings | `models/embeddinggemma-2-Q8_0.gguf` | llama-server `--embedding` | 8185 | 450 MB |
| `yolo` | detección webcam | `yolo26s.pt` (+ `yolo26n.pt`, `yolo26m.pt`) | ultralytics in-process | – | 900 MB |

Medidos en esta máquina (usar como referencia en la UI):
- LFM3 1.7B Q4: ~11 tok/s decode, prefill ~40 tok/s, es modelo *thinking* (respuesta en
  `reasoning_content` y después `content`).
- OneJev VLM: prefill de imagen ~2.6 s con `--image-max-tokens 96` (112 tokens de prompt);
  con imagen repetida el KV cache baja a ~0.15 s. Devuelve el dígito en `reasoning_content`.
- parakeet-ultra Q4_K_M: **7× tiempo real** (876 ms para 5.9 s de audio), con timestamps y
  probabilidad por token. Es offline por chunks → el streaming se simula con VAD + ventanas.
- embeddinggemma-2 Q8: 768 dims, L2-normalizado, 0.41 GB de RSS.
- YOLO26s @640: ~160 ms/frame (≈6 FPS).

## API del hub (mismo origen, JSON)

### Estado y carga de modelos
- `GET /api/state` →
  `{ram:{total_mb,available_mb,used_models_mb,budget_mb},
    models:[{id,label,kind,src,params,quant,size_mb,est_ram_mb,port,status,
             ready_ms,last_error,metrics:{...}}],
    demos:[{id,path,title,models,tagline}]}`
  `status` ∈ `off | loading | ready | error | busy`.
- `POST /api/models/{id}/load` → 200 `{ok:true,model:{...}}` | 409 `{ok:false,reason:"ram",need_mb,free_mb}`
- `POST /api/models/{id}/unload` → `{ok:true}`
- `GET /api/log/{id}?n=200` → `{lines:[...]}` (tail del log del backend)
- `GET /api/health` → `{ok:true, ts}`

### Demo 1 · dedos (VLM en vivo)
- `POST /api/vision/frame`
  req `{image_b64:str(png|jpg base64, sin data:), prompt?:str, image_max_tokens?:int,
        max_tokens?:int, grammar?:str}` (default: prompt de conteo, `grammar:"root ::= [0-5]"`,
        `max_tokens:8`)
  resp `{ok, answer:str(dígito 0-5), raw:str, ms_prefill, ms_decode, ms_total,
         prompt_tokens, completion_tokens, tok_per_s}`
  Necesita `onejev-vlm` ready; si no lo está → 409 `{ok:false,reason:"model_not_loaded",model:"onejev-vlm"}`.
- `GET /api/vision/config` → `{prompt, image_max_tokens, grammar, fps_cap}`

### Demo 2 · LLM en vivo (servido local, usable desde `pi`)
- `POST /api/llm/chat`
  req `{model:"lfm3-1.7b"|"mnn-pocket", messages:[{role,content}], max_tokens?:int,
        temperature?:float, thinking?:bool}`
  resp `{ok, content, reasoning, usage:{prompt_tokens,completion_tokens,total_tokens},
         timings:{prompt_ms,predicted_ms,prompt_per_second,predicted_per_second}, model}`
- `GET /api/llm/config` → `{models:[...], pi:{cmd, base_url, notes}}`
- El hub expone además proxy OpenAI-compatible para `pi`:
  `POST /v1/proxy/{model_id}/chat/completions` (pasa el body tal cual a llama-server) y
  documenta `pi --provider openai --model <id> --api-base-url http://127.0.0.1:8180/v1/proxy/<model_id>`.

### Demo 3 · ASR en vivo (parakeet-ultra)
- `POST /api/asr/window`
  req `{audio_b64:str (WAV 16 kHz mono PCM16), window_start_s:float, lang?:str}`
  resp `{ok, text, words:[{t0,t1,word,p}], ms, rtf, lang}` (tiempos relativos a `window_start_s`)
  Necesita `parakeet-ultra`. El navegador hace VAD por energía y manda ventanas de 2–8 s.
- `GET /api/asr/config` → `{model,rtf_ref,chunk_s}`

### Demo 4 · embeddings + búsqueda semántica (RI CONICET)
- `POST /api/search`  req `{query:str, k?:int(default 10), scope?:int (filas a escanear, default 1% = 2946)}`
  resp `{ok, took_ms, embed_ms, scan_ms, scanned_rows, dims, k,
         results:[{item_id,title,authors:[..],year,type,language,score,handle,pdf_url}]}`
- `GET /api/search/config` → `{rows_total:294595, indexed_rows, dims:768, model:"embeddinggemma-2-Q8_0",
    scopes:{p1:2946,p5:14730,p25:73649,full:294595}, corpus:"UNRN/ri-conicet"}`
- Índices en `data/index/` (ver `scripts/build_index.py`).

### Demo 5 · YOLO con webcam
- `POST /api/yolo/frame`
  req `{image_b64:str, model?:"yolo26n"|"yolo26s"|"yolo26m", conf?:float(0.35), iou?:float(0.5), imgsz?:int(640)}`
  resp `{ok, ms, model, imgsz, boxes:[{cls:int,name:str,conf:float,xyxy:[x1,y1,x2,y2]}], counts:{name:n}}`
- `GET /api/yolo/config` → `{models:[...], default:"yolo26s", classes_coco:80}`

Errores: siempre `{ok:false, reason, ...}` con status 4xx/5xx. `reason` ∈
`model_not_loaded | over_ram | bad_request | backend_down | timeout`.

## Frontend

Rutas estáticas servidas por el hub:
- `/` launcher: 5 tarjetas + switches de carga de modelos + medidor de RAM + links a cada demo.
- `/d/dedos`, `/d/llm`, `/d/asr`, `/d/search`, `/d/yolo`.

Compartido: `web/assets/surus/*` (tokens, fonts, logos) + `web/assets/app.css` (shell) +
`web/assets/app.js` (helper `api()`, `fmt()`, barra de RAM, lockup SURUS).
Los `html` de cada demo viven en `web/pages/`.
