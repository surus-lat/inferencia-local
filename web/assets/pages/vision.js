/* ============================================================================
   vision.js — lógica compartida de cámara para /d/dedos y /d/yolo
   ----------------------------------------------------------------------------
   sin dependencias: sólo tokens.css + fonts.css. Se carga como script clásico
   y publica window.VisionKit. Las páginas llaman:
     VisionKit.startCam(video)            getUserMedia + play
     VisionKit.captureFrame(video, maxW)  jpeg base64 SIN el prefijo data:
     VisionKit.frameSize(video, maxW)     {w,h} reales del frame que se enviaría
     VisionKit.createLoop(tick, opts)     loop secuencial: 1 request en vuelo,
                                          el resto de los frames se descarta
     VisionKit.drawBoxes(canvas, boxes, w, h, opts)
   Colores: siempre de tokens.css (--surus-*), nunca hardcodeados acá.
   ========================================================================== */
(function (global) {
  'use strict';

  var JPEG_QUALITY = 0.72;
  var MAX_DPR = 2;

  function now() {
    return (global.performance && global.performance.now) ? global.performance.now() : Date.now();
  }

  function token(name, fallback) {
    try {
      var v = global.getComputedStyle(global.document.documentElement).getPropertyValue(name);
      v = (v || '').trim();
      return v || fallback;
    } catch (e) {
      return fallback;
    }
  }

  function mean(list) {
    if (!list.length) return 0;
    var s = 0;
    for (var i = 0; i < list.length; i++) s += list[i];
    return s / list.length;
  }

  /* ------------------------------------------------------------ cámara */

  function hasCamera() {
    return !!(global.navigator && global.navigator.mediaDevices &&
              global.navigator.mediaDevices.getUserMedia);
  }

  // Devuelve {stream, settings}. Tira Error con mensaje legible si no hay cámara.
  function startCam(video, opts) {
    opts = opts || {};
    if (!hasCamera()) {
      return Promise.reject(new Error(
        'Este navegador no expone getUserMedia (necesitás http://127.0.0.1 o https).'));
    }
    var want = {
      audio: false,
      video: {
        width: { ideal: opts.width || 640 },
        height: { ideal: opts.height || 480 },
        facingMode: opts.facingMode || 'user'
      }
    };
    return global.navigator.mediaDevices.getUserMedia(want).then(function (stream) {
      video.srcObject = stream;
      video.muted = true;
      video.setAttribute('playsinline', '');
      var p = video.play();
      if (p && p.catch) p.catch(function () { /* autoplay bloqueado: da igual, el loop igual toma frames */ });
      var track = stream.getVideoTracks()[0];
      return { stream: stream, settings: (track && track.getSettings) ? track.getSettings() : {} };
    });
  }

  function stopCam(video) {
    var stream = video && video.srcObject;
    if (stream && stream.getTracks) {
      stream.getTracks().forEach(function (t) { t.stop(); });
    }
    if (video) video.srcObject = null;
  }

  // Dimensiones del frame que captureFrame produciría (el espacio de coords de los boxes).
  function frameSize(video, maxW) {
    var vw = video.videoWidth || 0;
    var vh = video.videoHeight || 0;
    if (!vw || !vh) return { w: 0, h: 0 };
    var s = maxW && vw > maxW ? maxW / vw : 1;
    return { w: Math.round(vw * s), h: Math.round(vh * s) };
  }

  // jpeg base64 sin el prefijo "data:image/jpeg;base64,". null si el video todavía no pinta.
  function captureFrame(video, maxW) {
    var size = frameSize(video, maxW || 640);
    if (!size.w || !size.h) return null;
    var canvas = captureFrame._canvas || (captureFrame._canvas = global.document.createElement('canvas'));
    canvas.width = size.w;
    canvas.height = size.h;
    var ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0, size.w, size.h);
    var url = canvas.toDataURL('image/jpeg', JPEG_QUALITY);
    var comma = url.indexOf(',');
    return comma >= 0 ? url.slice(comma + 1) : null;
  }

  /* ------------------------------------------------------- boxes en canvas */

  // boxes en el espacio w x h (el tamaño del frame enviado) -> canvas mostrado.
  // opts: {mirror, labels, fontSize, maxBoxes}
  function drawBoxes(canvas, boxes, w, h, opts) {
    opts = opts || {};
    var dpr = Math.min(global.devicePixelRatio || 1, MAX_DPR);
    var cssW = canvas.clientWidth;
    var cssH = canvas.clientHeight;
    if (!cssW || !cssH) return 0;
    var pw = Math.round(cssW * dpr);
    var ph = Math.round(cssH * dpr);
    if (canvas.width !== pw || canvas.height !== ph) {
      canvas.width = pw;
      canvas.height = ph;
    }
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    if (!w || !h || !boxes || !boxes.length) return 0;

    var sx = cssW / w;
    var sy = cssH / h;
    var aqua = token('--surus-aqua', '#7FE3CD');
    var deep = token('--surus-deep', '#182128');
    var line = token('--surus-line-2', '#434D55');
    var body = token('--surus-body', '#B7BDC1');
    var mirror = !!opts.mirror;
    var labels = opts.labels !== false;
    var fs = opts.fontSize || 11;
    var shown = opts.maxBoxes ? Math.min(boxes.length, opts.maxBoxes) : boxes.length;

    ctx.font = fs + 'px "JetBrains Mono", ui-monospace, monospace';
    ctx.lineJoin = 'miter';

    for (var i = 0; i < shown; i++) {
      var b = boxes[i] || {};
      var xy = b.xyxy || [];
      if (xy.length < 4) continue;
      var x1 = Math.max(0, Math.min(cssW, xy[0] * sx));
      var y1 = Math.max(0, Math.min(cssH, xy[1] * sy));
      var x2 = Math.max(0, Math.min(cssW, xy[2] * sx));
      var y2 = Math.max(0, Math.min(cssH, xy[3] * sy));
      if (mirror) {
        var mx1 = cssW - x2;
        var mx2 = cssW - x1;
        x1 = mx1;
        x2 = mx2;
      }
      var bw = x2 - x1;
      var bh = y2 - y1;
      if (bw <= 1 || bh <= 1) continue;

      // relleno tenue del acento, sin sombras ni colores nuevos
      ctx.globalAlpha = 0.06;
      ctx.fillStyle = aqua;
      ctx.fillRect(x1, y1, bw, bh);
      ctx.globalAlpha = 1;

      ctx.strokeStyle = aqua;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x1, y1, bw, bh);

      if (!labels) continue;
      var name = b.name || ('cls ' + (b.cls == null ? '?' : b.cls));
      var conf = (b.conf == null) ? '' : ' ' + Number(b.conf).toFixed(2);
      var label = name + conf;
      var tw = ctx.measureText(label).width;
      var lh = fs + 5;
      var lx = x1;
      var ly = (y1 - lh >= 0) ? y1 - lh : y1;
      if (lx + tw + 8 > cssW) lx = Math.max(0, cssW - tw - 8);

      ctx.fillStyle = deep;
      ctx.fillRect(lx, ly, tw + 8, lh);
      ctx.strokeStyle = line;
      ctx.lineWidth = 1;
      ctx.strokeRect(lx + 0.5, ly + 0.5, tw + 7, lh - 1);
      ctx.fillStyle = body;
      ctx.fillText(label, lx + 4, ly + lh - 6);
    }
    return shown;
  }

  /* -------------------------------------------------- loop secuencial */

  // tick: función async que captura, manda y pinta.
  // opts: {intervalMs} — cadencia objetivo (el cap de frecuencia).
  // Nunca hay más de 1 request en vuelo: si el tick sigue ocupado, el tick
  // del intervalo se descarta (y se cuenta en state.dropped).
  function createLoop(tick, opts) {
    opts = opts || {};
    var state = {
      running: false,
      inFlight: false,
      intervalMs: opts.intervalMs || 1000,
      ticks: 0,
      dropped: 0,
      errors: 0,
      lastMs: 0,
      avgMs: 0,
      fps: 0,
      lastError: null,
      lastAt: 0
    };
    var timer = null;
    var cycles = [];
    var durations = [];
    var lastStart = 0;

    function pump() {
      if (!state.running) return;
      if (state.inFlight) {
        state.dropped++;
        return;
      }
      state.inFlight = true;
      var t0 = now();
      if (lastStart) {
        cycles.push(t0 - lastStart);
        if (cycles.length > 24) cycles.shift();
      }
      lastStart = t0;
      Promise.resolve()
        .then(function () { return tick(); })
        .then(
          function () { state.ticks++; },
          function (err) { state.errors++; state.lastError = err; }
        )
        .then(function () {
          var t1 = now();
          state.inFlight = false;
          state.lastMs = t1 - t0;
          durations.push(t1 - t0);
          if (durations.length > 24) durations.shift();
          state.avgMs = mean(durations);
          state.fps = cycles.length >= 2 ? 1000 / mean(cycles) : 0;
          state.lastAt = t1;
        });
    }

    function restart() {
      if (timer) global.clearInterval(timer);
      timer = state.running ? global.setInterval(pump, Math.max(16, state.intervalMs)) : null;
    }

    return {
      state: state,
      start: function () {
        if (state.running) return;
        state.running = true;
        cycles = [];
        durations = [];
        lastStart = 0;
        restart();
        pump();
      },
      stop: function () {
        state.running = false;
        if (timer) global.clearInterval(timer);
        timer = null;
      },
      toggle: function () {
        if (state.running) this.stop();
        else this.start();
      },
      setIntervalMs: function (ms) {
        state.intervalMs = ms;
        restart();
      },
      kick: function () { pump(); },
      reset: function () {
        cycles = [];
        durations = [];
        lastStart = 0;
        state.fps = 0;
        state.avgMs = 0;
        state.dropped = 0;
        state.ticks = 0;
        state.errors = 0;
      }
    };
  }

  /* ------------------------------------------------------------- red */

  // {status, data, networkError}. status 0 = no llegó a haber respuesta.
  function request(path, method, body) {
    var init = { method: method, headers: {} };
    if (body != null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    return global.fetch(path, init).then(function (res) {
      return res.text().then(function (text) {
        var data;
        try {
          data = text ? JSON.parse(text) : {};
        } catch (e) {
          data = { ok: false, reason: 'bad_json', raw: String(text).slice(0, 240) };
        }
        return { status: res.status, data: data };
      });
    }, function () {
      return { status: 0, networkError: true, data: { ok: false, reason: 'network' } };
    });
  }

  function api(path, body) { return request(path, 'POST', body == null ? {} : body); }
  function getJSON(path) { return request(path, 'GET'); }

  function loadModel(id) {
    return api('/api/models/' + encodeURIComponent(id) + '/load', {});
  }
  function unloadModel(id) {
    return api('/api/models/' + encodeURIComponent(id) + '/unload', {});
  }

  function reasonText(reason) {
    var map = {
      model_not_loaded: 'el modelo no está cargado',
      over_ram: 'no entra en el presupuesto de RAM',
      bad_request: 'pedido inválido',
      backend_down: 'el backend no responde',
      timeout: 'tiempo de espera agotado',
      network: 'no se pudo conectar al hub',
      bad_json: 'el hub devolvió algo que no es JSON'
    };
    return map[reason] || ('error: ' + (reason || 'desconocido'));
  }

  /* ---------------------------------------------------------- formato */

  function num(n, digits) {
    if (n == null || isNaN(n)) return '—';
    var d = digits == null ? 0 : digits;
    var fixed = Number(n).toFixed(d);
    var parts = fixed.split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    return parts.join(',');
  }

  function clock(d) {
    function p(v) { return (v < 10 ? '0' : '') + v; }
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  global.VisionKit = {
    hasCamera: hasCamera,
    startCam: startCam,
    stopCam: stopCam,
    frameSize: frameSize,
    captureFrame: captureFrame,
    drawBoxes: drawBoxes,
    createLoop: createLoop,
    api: api,
    getJSON: getJSON,
    loadModel: loadModel,
    unloadModel: unloadModel,
    reasonText: reasonText,
    num: num,
    clock: clock,
    token: token
  };
})(typeof window !== 'undefined' ? window : this);
