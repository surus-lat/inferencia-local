/* ============================================================================
   asr.js — Demo 3 · ASR en vivo con parakeet-ultra (CPU) · SURUS
   ----------------------------------------------------------------------------
   El navegador captura el micro a 16 kHz mono. Un VAD por energía con
   histéresis arma ventanas de 2–8 s (corte a ~1,2 s de silencio o duro a 8 s
   de habla), cada ventana se codifica a WAV 16-bit PCM16 (header RIFF escrito
   a mano) y se manda como base64 a POST /api/asr/window.
   Cola estricta: solo un request en vuelo; si ya hay uno pendiente, la nueva
   ventana espera (y si se acumulan, se fusionan para no desordenar).
   parakeet-ultra es offline por chunks: el texto tentativo (la ventana en
   curso) se muestra en cursiva con tinte aqua hasta que llega la respuesta y
   pasa a confirmado.
   Sin dependencias. Si window.Surus (app.js) no existe, usa un helper propio.
   ========================================================================== */
(function () {
  'use strict';

  /* ------------------------------------------------------------------ dom */

  function $(id) { return document.getElementById(id); }

  var elMic = $('btn-mic');
  var elClear = $('btn-clear');
  var elCopy = $('btn-copy');
  var elDl = $('btn-dl');
  var elThr = $('vad-thr');
  var elThrVal = $('vad-thr-val');
  var elLang = $('lang');
  var elVu = $('vu');
  var elTx = $('tx');
  var elTxEmpty = $('tx-empty');
  var elTry = $('tx-try');
  var elTip = $('wordtip');
  var elNotice = $('notice');
  var elVoiceDot = $('dot-voice');
  var elVoiceState = $('voice-state');
  var elModelDot = $('dot-model');
  var elModelState = $('model-state');
  var elChipModel = $('chip-model');
  var elChipRef = $('chip-ref');
  var elKMs = $('k-ms');
  var elKRtf = $('k-rtf');
  var elKWin = $('k-win');
  var elKTotal = $('k-total');
  var elKChunk = $('k-chunk');
  var elKWords = $('k-words');
  var elNoteMetrics = $('metrics-note');

  /* --------------------------------------------------------------- formato */

  function n(x, dec) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return Number(x).toFixed(dec === undefined ? 1 : dec).replace('.', ',');
  }

  function kb(bytes) {
    if (bytes === null || bytes === undefined || isNaN(bytes)) return '—';
    return n(bytes / 1024, 1) + ' KB';
  }

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
  }

  /* ------------------------------------------------------------------- api */
  /* Usa Surus.api si app.js está cargado; si no, un equivalente propio.
     En ambos casos el Error trae .reason y .status (409 model_not_loaded). */

  function localApi(path, opts) {
    var o = opts || {};
    var init = { method: o.method || (o.body !== undefined ? 'POST' : 'GET'), headers: {} };
    if (o.body !== undefined && o.body !== null) {
      init.body = JSON.stringify(o.body);
      init.headers['Content-Type'] = 'application/json';
    }
    return fetch(path, init).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = { ok: false, reason: 'bad_json' }; }
        }
        if (!res.ok || !data || data.ok === false) {
          var err = new Error((data && data.reason) || ('http_' + res.status));
          err.reason = (data && data.reason) || ('http_' + res.status);
          err.status = res.status;
          err.data = data || {};
          throw err;
        }
        return data;
      });
    });
  }

  var api = (window.Surus && window.Surus.api) ? window.Surus.api : localApi;

  /* ---------------------------------------------------------------- aviso */

  function notice(text, kind, mono) {
    if (!text) { elNotice.hidden = true; return; }
    elNotice.hidden = false;
    elNotice.className = 'notice' + (kind === 'ok' ? ' notice--ok' : kind === 'warn' ? ' notice--warn' : '');
    elNotice.innerHTML = esc(text) + (mono ? '<span class="notice-mono">' + esc(mono) + '</span>' : '');
  }

  /* ------------------------------------------------- config / estado modelo */

  function setModelState(label, on) {
    elModelState.textContent = label;
    elModelDot.classList.toggle('is-on', !!on);
  }

  function loadConfig() {
    api('/api/asr/config').then(function (cfg) {
      if (cfg.model) elChipModel.textContent = cfg.model;
      if (cfg.rtf_ref) elChipRef.textContent = 'esperado en esta laptop: ~' + n(cfg.rtf_ref, 0) + '× tiempo real';
      setModelState('hub disponible', true);
    }).catch(function (err) {
      setModelState('hub sin responder', false);
      notice(
        'No se pudo leer /api/asr/config. Con el hub en 127.0.0.1:8180 (o el stub en 8194) esto desaparece.',
        'warn',
        (err && err.message) || String(err)
      );
    });
  }

  function loadModel() {
    setModelState('cargando parakeet-ultra…', false);
    notice('Cargando parakeet-ultra en CPU…');
    api('/api/models/parakeet-ultra/load', { method: 'POST' }).then(function () {
      setModelState('parakeet-ultra listo', true);
      notice('parakeet-ultra cargado. Reintento las ventanas que quedaron en cola.', 'ok');
      blocked = false;
      flush();
    }).catch(function (err) {
      setModelState('fallo la carga', false);
      notice('No se pudo cargar el modelo.', 'warn', (err && err.message) || String(err));
    });
  }

  /* --------------------------------------------------------------- estado */

  var running = false;          // micro activo
  var ctx = null;               // AudioContext
  var mediaSource = null;       // MediaStreamAudioSourceNode
  var captureNode = null;       // AudioWorkletNode o ScriptProcessorNode
  var stream = null;

  var SAMPLE_RATE = 16000;      // objetivo del WAV (el hub espera 16 kHz)
  var FRAME_S = 512 / 16000;    // tamaño de cuadro lógicos (512 muestras)

  var thrDb = -40;              // umbral calibrable con el slider
  var HYST_DB = 6;              // histéresis: la escucha se mantiene 6 dB abajo del umbral
  var SILENCE_CUT_MS = 1200;    // corte de frase
  var MAX_SPEECH_S = 8.0;       // corte duro
  var MIN_SEND_S = 0.8;         // por debajo, es un blip de ruido: se descarta
  var PRE_ROLL_S = 0.24;        // audio previo a la detección (no pierde el ataque)
  var TAIL_KEEP_S = 0.2;        // colas de silencio que se conservan al cortar

  var voiced = false;           // estado actual del gate con histéresis
  var voiceRunFrames = 0;       // cuadros seguidos sobre el umbral (anti-clic)
  var silenceMs = 0;            // silencio acumulado dentro de la escucha
  var chunks = [];              // Float32Array del window en curso
  var preRoll = [];             // cuadros recientes cuando está en silencio
  var speechStartS = 0;         // ctx.currentTime del inicio (incluye pre-roll)
  var speechDurS = 0;           // duración acumulada de la ventana
  var lastVoiceSample = 0;      // índice de muestra de la última frame voiced
  var windowSeq = 0;

  var vuHist = [];              // histórico de dB para el canvas
  var VU_N = 96;

  var inFlight = false;         // request en vuelo (cola de a 1)
  var blocked = false;          // 409 / error: no mandar hasta recuperar
  var pending = [];             // ventanas esperando [{startS, durS, samples:[Float32Array]}]

  var totalAudioS = 0;          // audio total transcrito
  var wordCount = 0;
  var lowConfCount = 0;
  var windows = [];             // resultados confirmados, para copiar/descargar

  /* -------------------------------------------------------- VU en canvas */

  var vctx = elVu.getContext('2d');

  function css(name, fallback) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  }

  var C = {
    aqua: css('--surus-aqua', '#7FE3CD'),
    line: css('--surus-line', '#2C3740'),
    line2: css('--surus-line-2', '#434D55'),
    muted: css('--surus-muted', '#7E878E'),
    ink: css('--surus-ink', '#121A20')
  };

  function drawVu() {
    var w = elVu.width, h = elVu.height;
    vctx.clearRect(0, 0, w, h);

    // rejilla de fondo
    vctx.strokeStyle = C.line;
    vctx.lineWidth = 1;
    vctx.beginPath();
    vctx.moveTo(0, h - 0.5);
    vctx.lineTo(w, h - 0.5);
    vctx.stroke();

    var lo = -60, hi = 0;
    var bw = w / VU_N;

    // línea de umbral
    var yThr = h - ((thrDb - lo) / (hi - lo)) * h;
    vctx.save();
    vctx.strokeStyle = C.line2;
    vctx.setLineDash([3, 3]);
    vctx.beginPath();
    vctx.moveTo(0, yThr);
    vctx.lineTo(w, yThr);
    vctx.stroke();
    vctx.restore();

    // barras de nivel
    for (var i = 0; i < vuHist.length; i++) {
      var db = vuHist[i];
      var t = Math.max(0, Math.min(1, (db - lo) / (hi - lo)));
      var bh = Math.max(1, t * (h - 4));
      var on = vuHist.on[i];
      vctx.fillStyle = on ? C.aqua : C.line2;
      vctx.globalAlpha = on ? 0.95 : 0.55;
      vctx.fillRect(i * bw, h - bh - 2, Math.max(1, bw - 1), bh);
    }
    vctx.globalAlpha = 1;

    // etiqueta de nivel actual
    vctx.font = '10px ' + (css('--surus-font-mono', 'monospace'));
    vctx.fillStyle = C.muted;
    var last = vuHist.length ? vuHist[vuHist.length - 1] : -60;
    vctx.fillText('nivel ' + Math.round(last) + ' dBFS · umbral ' + thrDb + ' dBFS', 4, 12);
  }

  function pushVu(db, on) {
    vuHist.push(db);
    vuHist.on.push(on);
    if (vuHist.length > VU_N) { vuHist.shift(); vuHist.on.shift(); }
    drawVu();
  }
  vuHist.on = [];

  function setVoiceUi(on) {
    elVoiceDot.classList.toggle('is-on', on);
    elVoiceState.textContent = on ? 'voz detectada' : (running ? 'en silencio' : 'micro detenido');
  }

  /* --------------------------------------------------------- VAD por frame */

  function rmsDb(buf) {
    var s = 0;
    for (var i = 0; i < buf.length; i++) s += buf[i] * buf[i];
    var rms = Math.sqrt(s / buf.length);
    return 20 * Math.log10(rms + 1e-9);
  }

  function resetWindow() {
    chunks = [];
    speechDurS = 0;
    silenceMs = 0;
    voiceRunFrames = 0;
    lastVoiceSample = 0;
  }

  function handleFrame(buf) {
    // La captura puede venir a la tasa real del contexto (48 kHz típico);
    // el VAD iguala el tamaño de cuadro lógico a esa tasa.
    var frameMs = (buf.length / (ctx ? ctx.sampleRate : SAMPLE_RATE)) * 1000;
    var db = rmsDb(buf);

    // gate con histéresis: abre en thrDb, se mantiene hasta thrDb - HYST_DB
    var gateOn;
    if (voiced) gateOn = db > (thrDb - HYST_DB);
    else gateOn = db >= thrDb;

    if (!voiced) {
      if (gateOn) {
        voiceRunFrames++;
        if (voiceRunFrames >= 2) {
          voiced = true;
          speechStartS = nowS() - (chunksTotalS() + PRE_ROLL_S);
          chunks = preRoll.slice();
          speechDurS = chunks.reduce(function (a, c) { return a + c.length; }, 0) / ctx.sampleRate;
          lastVoiceSample = chunksTotalS() * ctx.sampleRate;
          silenceMs = 0;
          setVoiceUi(true);
          updateTentative();
        }
      } else {
        voiceRunFrames = 0;
        preRoll.push(buf);
        var maxPre = Math.ceil((PRE_ROLL_S * 1000) / frameMs);
        while (preRoll.length > maxPre) preRoll.shift();
      }
    }

    if (voiced) {
      chunks.push(buf);
      speechDurS += frameMs / 1000;
      if (gateOn) {
        lastVoiceSample = chunksTotalS() * ctx.sampleRate;
        silenceMs = 0;
      } else {
        silenceMs += frameMs;
      }

      updateTentative();

      var cutReason = null;
      if (speechDurS >= MAX_SPEECH_S) cutReason = 'max';
      else if (silenceMs >= SILENCE_CUT_MS) cutReason = 'silencio';

      if (cutReason) {
        var cutIdx = (cutReason === 'max')
          ? chunksTotalS() * ctx.sampleRate
          : Math.min(chunksTotalS() * ctx.sampleRate, lastVoiceSample + TAIL_KEEP_S * ctx.sampleRate);
        var dur = cutIdx / ctx.sampleRate;
        if (dur < MIN_SEND_S) {
          // blip corto de ruido: descartar y volver a escuchar
          voiced = false;
          resetWindow();
          preRoll = [];
          setVoiceUi(false);
          updateTentative();
        } else {
          windowSeq++;
          pending.push({
            seq: windowSeq,
            startS: Math.max(0, speechStartS),
            durS: dur,
            samples: chunks,
            cutIdx: cutIdx
          });
          if (pending.length > 2) mergeLastTwo();
          voiced = false;
          resetWindow();
          preRoll = [];
          setVoiceUi(false);
          updateTentative();
          flush();
        }
      }
    }

    pushVu(db, voiced && gateOn);
  }

  function chunksTotalS() {
    var tot = 0;
    for (var i = 0; i < chunks.length; i++) tot += chunks[i].length;
    return tot / (ctx ? ctx.sampleRate : SAMPLE_RATE);
  }

  /* Fusionar dos ventanas pendientes consecutivas para no desordenar el
     transcripto cuando el hub está más lento que el habla. */
  function mergeLastTwo() {
    var a = pending[pending.length - 2];
    var b = pending[pending.length - 1];
    a.samples = a.samples.concat(b.samples);
    a.cutIdx = (a.cutIdx / ctx.sampleRate + b.durS) * ctx.sampleRate;
    a.durS = a.cutIdx / ctx.sampleRate;
    a.merged = true;
    pending.pop();
  }

  /* --------------------------------------------------------------- audio */

  function nowS() {
    return ctx ? ctx.currentTime : 0;
  }

  /* Remuestreo lineal simple si el contexto no corrió a 16 kHz. */
  function resampleTo16k(samples, srcRate) {
    if (srcRate === SAMPLE_RATE) return samples;
    var ratio = srcRate / SAMPLE_RATE;
    var out = new Float32Array(Math.floor(samples.length / ratio));
    for (var i = 0; i < out.length; i++) {
      var p = i * ratio;
      var i0 = Math.floor(p);
      var i1 = Math.min(samples.length - 1, i0 + 1);
      var f = p - i0;
      out[i] = samples[i0] * (1 - f) + samples[i1] * f;
    }
    return out;
  }

  /* WAV 16-bit PCM16 mono con el header RIFF escrito a mano. */
  function encodeWav(samples16k) {
    var n = samples16k.length;
    var dataLen = n * 2;
    var buf = new ArrayBuffer(44 + dataLen);
    var view = new DataView(buf);

    function ascii(offset, str) {
      for (var i = 0; i < str.length; i++) view.setUint8(offset + i, str.charCodeAt(i));
    }

    ascii(0, 'RIFF');
    view.setUint32(4, 36 + dataLen, true);
    ascii(8, 'WAVE');
    ascii(12, 'fmt ');
    view.setUint32(16, 16, true);          // tamaño del chunk fmt
    view.setUint16(20, 1, true);           // PCM
    view.setUint16(22, 1, true);           // 1 canal
    view.setUint32(24, SAMPLE_RATE, true); // 16 kHz
    view.setUint32(28, SAMPLE_RATE * 2, true); // byte rate
    view.setUint16(32, 2, true);           // block align
    view.setUint16(34, 16, true);          // bits
    ascii(36, 'data');
    view.setUint32(40, dataLen, true);

    var off = 44;
    for (var i = 0; i < n; i++) {
      var s = Math.max(-1, Math.min(1, samples16k[i]));
      view.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      off += 2;
    }
    return new Uint8Array(buf);
  }

  function b64FromBytes(bytes) {
    var CH = 0x8000;
    var parts = [];
    for (var i = 0; i < bytes.length; i += CH) {
      parts.push(String.fromCharCode.apply(null, bytes.subarray(i, i + CH)));
    }
    return btoa(parts.join(''));
  }

  /* Recorta la ventana al cutIdx, remuestrea a 16k y arma el body. */
  function windowToBody(w) {
    // concatenar muestras hasta cutIdx
    var total = 0;
    for (var i = 0; i < w.samples.length; i++) total += w.samples[i].length;
    var limit = Math.min(w.cutIdx || total, total);
    var merged = new Float32Array(limit);
    var off = 0;
    for (var j = 0; j < w.samples.length && off < limit; j++) {
      var c = w.samples[j];
      var take = Math.min(c.length, limit - off);
      merged.set(c.subarray(0, take), off);
      off += take;
    }
    var srcRate = ctx ? ctx.sampleRate : SAMPLE_RATE;
    var pcm = resampleTo16k(merged, srcRate);
    var wav = encodeWav(pcm);
    return {
      body: { audio_b64: b64FromBytes(wav), window_start_s: w.startS, lang: elLang.value || 'es' },
      wavBytes: wav.length,
      durS: pcm.length / SAMPLE_RATE
    };
  }

  /* ---------------------------------------------------------------- cola */

  function flush() {
    if (inFlight || blocked || !pending.length) return;
    var w = pending.shift();
    var enc = windowToBody(w);

    elKChunk.textContent = kb(enc.wavBytes);
    elKWin.textContent = n(enc.durS, 1) + ' s';
    updateTentative(w);

    inFlight = true;
    api('/api/asr/window', { method: 'POST', body: enc.body }).then(function (res) {
      inFlight = false;
      onWindowResult(w, res, enc);
      flush();
    }).catch(function (err) {
      inFlight = false;
      pending.unshift(w);
      if (err && (err.status === 409 || err.reason === 'model_not_loaded')) {
        blocked = true;
        setModelState('parakeet-ultra no está cargado', false);
        showLoadNotice();
      } else {
        blocked = true;
        notice('El request de la ventana falló. Queda en cola para reintentar.', 'warn',
          (err && (err.message || err.reason)) || String(err));
      }
      renderTryError();
    });
  }

  function showLoadNotice() {
    elNotice.hidden = false;
    elNotice.className = 'notice notice--warn';
    elNotice.innerHTML =
      'El hub respondió <strong>409 model_not_loaded</strong>: parakeet-ultra no está en RAM.' +
      '<span class="notice-mono">POST /api/models/parakeet-ultra/load</span>';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn--sm';
    btn.style.marginTop = '8px';
    btn.textContent = 'Cargar modelo';
    btn.addEventListener('click', loadModel);
    elNotice.appendChild(btn);
  }

  /* --------------------------------------------------------- transcripto */

  function onWindowResult(w, res, enc) {
    totalAudioS += enc.durS;
    elKTotal.textContent = n(totalAudioS, 1) + ' s';

    var ms = res.ms !== undefined ? res.ms : null;
    elKMs.textContent = ms === null ? '—' : (ms < 1000 ? Math.round(ms) + ' ms' : n(ms / 1000, 2) + ' s');

    if (res.rtf !== undefined && res.rtf !== null && res.rtf > 0) {
      elKRtf.textContent = n(1 / res.rtf, 1) + '×  (rtf ' + n(res.rtf, 3) + ')';
    } else if (ms) {
      var est = (enc.durS * 1000) / ms;
      elKRtf.textContent = n(est, 1) + '×';
    }

    var words = Array.isArray(res.words) ? res.words : [];
    var low = 0;
    for (var i = 0; i < words.length; i++) {
      var p = Number(words[i].p);
      if (!isNaN(p) && p < 0.6) low++;
    }
    wordCount += words.length;
    lowConfCount += low;
    elKWords.textContent = wordCount + (lowConfCount ? ' (' + lowConfCount + ' baja)' : ' (—)');

    appendWindow(res.window_start_s !== undefined ? res.window_start_s : w.startS, res.text, words);
    windows.push({ startS: w.startS, text: res.text || '' });
    updateTentative();
  }

  function appendWindow(startS, text, words) {
    elTxEmpty.hidden = true;

    var row = document.createElement('div');
    row.className = 'tx-win';

    var t = document.createElement('span');
    t.className = 'tx-t';
    var t0 = Number(startS) || 0;
    var t1 = words.length ? t0 + (Number(words[words.length - 1].t1) || 0) : t0;
    t.textContent = fmtClock(t0) + '–' + fmtClock(t1);
    row.appendChild(t);

    var body = document.createElement('span');
    body.className = 'tx-text';
    if (words.length) {
      words.forEach(function (wd, i) {
        var span = document.createElement('span');
        span.className = 'w';
        var p = Number(wd.p);
        if (!isNaN(p) && p < 0.6) span.className += ' w--low';
        span.textContent = (i ? ' ' : '') + String(wd.word || '');
        var abs0 = t0 + (Number(wd.t0) || 0);
        var abs1 = t0 + (Number(wd.t1) || 0);
        span.dataset.tip = wd.word + '  ·  ' + fmtClock(abs0) + ' → ' + fmtClock(abs1) +
          '  ·  p ' + (isNaN(p) ? '—' : n(p, 2)) +
          (p < 0.6 ? '  ·  baja confianza' : '');
        body.appendChild(span);
      });
    } else {
      body.textContent = text || '(sin texto)';
    }
    row.appendChild(body);
    elTx.appendChild(row);
    elTx.scrollTop = elTx.scrollHeight;
  }

  function fmtClock(s) {
    s = Math.max(0, Number(s) || 0);
    var m = Math.floor(s / 60);
    var rest = s - m * 60;
    return (m ? m + ':' : '') + rest.toFixed(1).replace('.', ',') + 's';
  }

  /* Texto tentativo: la ventana en curso, en cursiva con tinte aqua. */
  function updateTentative(sending) {
    if (sending) {
      elTry.hidden = false;
      elTry.innerHTML = '<span class="tx-t">ventana #' + sending.seq + '</span> ' +
        'transcribiendo ' + n(sending.durS, 1) + ' s de audio en el hub…';
      return;
    }
    if (voiced) {
      elTry.hidden = false;
      elTry.innerHTML = '<span class="tx-t">ventana #' + (windowSeq + 1) + '</span> ' +
        'hablando… ' + n(speechDurS, 1) + ' s · se cierra con ~1,2 s de silencio';
      return;
    }
    if (pending.length) {
      elTry.hidden = false;
      elTry.innerHTML = '<span class="tx-t">cola</span> ' + pending.length +
        ' ventana(s) esperando; un solo request en vuelo.';
      return;
    }
    elTry.hidden = true;
  }

  function renderTryError() {
    if (!blocked) return;
    elTry.hidden = false;
    if (elTry.textContent.indexOf('pausado') === -1) {
      elTry.innerHTML = '<span class="tx-t">pausado</span> la cola espera a que el hub vuelva a responder.';
    }
  }

  /* --------------------------------------------------------- tooltip palabra */

  elTx.addEventListener('mouseover', function (ev) {
    var w = ev.target && ev.target.closest ? ev.target.closest('.w') : null;
    if (!w || !w.dataset.tip) return;
    elTip.textContent = w.dataset.tip;
    elTip.hidden = false;
    var r = w.getBoundingClientRect();
    elTip.style.left = Math.max(4, Math.min(window.innerWidth - elTip.offsetWidth - 8, r.left)) + 'px';
    elTip.style.top = (r.top - elTip.offsetHeight - 6) + 'px';
  });
  elTx.addEventListener('mouseout', function () { elTip.hidden = true; });

  /* --------------------------------------------------------- capturar micro */

  function getCapture(rate) {
    return navigator.mediaDevices && navigator.mediaDevices.getUserMedia
      ? navigator.mediaDevices.getUserMedia({
          audio: {
            channelCount: 1,
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true
          },
          video: false
        })
      : Promise.reject(new Error('getUserMedia no está disponible'));
  }

  var WORKLET_SRC =
    'class AsrCapturer extends AudioWorkletProcessor {' +
    '  process(inputs) {' +
    '    var inp = inputs[0];' +
    '    if (inp && inp[0]) this.port.postMessage(inp[0].slice(0));' +
    '    return true;' +
    '  }' +
    '}' +
    "registerProcessor('asr-capturer', AsrCapturer);";

  function attachCapture(source) {
    if (ctx.audioWorklet) {
      var url = URL.createObjectURL(new Blob([WORKLET_SRC], { type: 'application/javascript' }));
      return ctx.audioWorklet.addModule(url).then(function () {
        URL.revokeObjectURL(url);
        var node = new AudioWorkletNode(ctx, 'asr-capturer');
        node.port.onmessage = function (ev) { handleFrame(ev.data); };
        source.connect(node);
        node.connect(ctx.destination); // keepalive para que el grafo corra
        captureNode = node;
      });
    }
    // fallback: ScriptProcessor (deprecated pero universal)
    return new Promise(function (resolve) {
      var node = ctx.createScriptProcessor(512, 1, 1);
      node.onaudioprocess = function (ev) {
        handleFrame(new Float32Array(ev.inputBuffer.getChannelData(0)));
      };
      source.connect(node);
      node.connect(ctx.destination);
      captureNode = node;
      resolve();
    });
  }

  function startMic() {
    if (running) return;
    notice('');
    getCapture().then(function (st) {
      stream = st;
      var Ctor = window.AudioContext || window.webkitAudioContext;
      try {
        ctx = new Ctor({ sampleRate: SAMPLE_RATE });
      } catch (e) {
        ctx = new Ctor(); // algunos navegadores no aceptan sampleRate: 16000
      }
      return ctx.resume().then(function () {
        mediaSource = ctx.createMediaStreamSource(stream);
        return attachCapture(mediaSource);
      });
    }).then(function () {
      running = true;
      elMic.textContent = 'Detener micro';
      elMic.classList.remove('btn--primary');
      setVoiceUi(false);
      updateTentative();
      flush(); // por si quedaron ventanas de antes
    }).catch(function (err) {
      running = false;
      notice('No se pudo abrir el micrófono.', 'warn', (err && err.message) || String(err));
    });
  }

  function stopMic() {
    running = false;
    if (captureNode && mediaSource) {
      try { mediaSource.disconnect(captureNode); } catch (e) { /* nada */ }
      try { captureNode.disconnect(); } catch (e) { /* nada */ }
    }
    if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    if (ctx && ctx.suspend) ctx.suspend();
    voiced = false;
    resetWindow();
    preRoll = [];
    elMic.textContent = 'Iniciar micro';
    elMic.classList.add('btn--primary');
    setVoiceUi(false);
    updateTentative();
  }

  /* ------------------------------------------------------- copiar / bajar */

  function transcriptText() {
    var head = 'INFERENCIA LOCAL · SURUS — transcripto ASR en vivo (parakeet-ultra, CPU)\n' +
      'Generado: ' + new Date().toLocaleString('es-AR') + '\n' +
      'Audio transcrito: ' + n(totalAudioS, 1) + ' s en ' + windows.length + ' ventana(s)\n\n';
    return head + windows.map(function (w) {
      return '[' + fmtClock(w.startS) + '] ' + w.text;
    }).join('\n') + '\n';
  }

  function copyTranscript() {
    var text = windows.length ? transcriptText() : '';
    if (!text) { notice('Todavía no hay transcripto para copiar.', 'warn'); return; }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        notice('Transcripto copiado al portapapeles.', 'ok');
      }).catch(function () { fallbackCopy(text); });
    } else {
      fallbackCopy(text);
    }
  }

  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      notice('Transcripto copiado al portapapeles.', 'ok');
    } catch (e) {
      notice('El navegador no permitió copiar; descargá el .txt.', 'warn');
    }
    document.body.removeChild(ta);
  }

  function downloadTranscript() {
    if (!windows.length) { notice('Todavía no hay transcripto para descargar.', 'warn'); return; }
    var blob = new Blob([transcriptText()], { type: 'text/plain;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'transcripto-asr-surus.txt';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 500);
  }

  function clearTranscript() {
    windows = [];
    totalAudioS = 0;
    wordCount = 0;
    lowConfCount = 0;
    windowSeq = 0;
    pending = [];
    elTx.querySelectorAll('.tx-win').forEach(function (el) { el.remove(); });
    elTxEmpty.hidden = false;
    elKTotal.textContent = '0,0 s';
    elKWords.textContent = '0 (—)';
    elKMs.textContent = '—';
    elKRtf.textContent = '—';
    elKWin.textContent = '—';
    elKChunk.textContent = '—';
    updateTentative();
  }

  /* -------------------------------------------------------------- eventos */

  elMic.addEventListener('click', function () { running ? stopMic() : startMic(); });
  elClear.addEventListener('click', clearTranscript);
  elCopy.addEventListener('click', copyTranscript);
  elDl.addEventListener('click', downloadTranscript);

  elThr.addEventListener('input', function () {
    thrDb = Number(elThr.value);
    elThrVal.textContent = (thrDb === 0 ? '0' : '−' + Math.abs(thrDb)) + ' dBFS';
    drawVu();
  });

  /* Reintentos suaves: si quedó algo en cola y el hub volvió, mandar solo. */
  setInterval(function () {
    if (running && pending.length && !blocked && !inFlight) flush();
  }, 2000);

  /* ---------------------------------------------------------------- init */

  elThrVal.textContent = '−' + Math.abs(thrDb) + ' dBFS';
  drawVu();
  loadConfig();
})();
