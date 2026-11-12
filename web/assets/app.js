/* ============================================================================
   app.js — helper compartido · SURUS / inferencia-local-demos
   Sin dependencias. Se carga con <script src="/assets/app.js"></script> y
   expone un único global: window.Surus.
   ========================================================================== */
(function (global) {
  'use strict';

  /* ------------------------------------------------------------- utilidades */

  /** Escapa texto plano para insertarlo con innerHTML. */
  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  /** 1150 -> "1.150" (agrupación es-AR, sin Intl para que sea determinístico). */
  function group(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, '.');
  }

  /** 1150 -> "1.150 MB" · 8192 -> "8.192 MB". Siempre en MB, para que los
   *  números de la UI se comparren directo con la tabla del SPEC. */
  function fmtMb(mb) {
    if (mb === null || mb === undefined || isNaN(mb)) return '—';
    return group(Math.round(Number(mb))) + ' MB';
  }

  /** 876 -> "876 ms" · 2600 -> "2,60 s". */
  function fmtMs(ms) {
    if (ms === null || ms === undefined || isNaN(ms)) return '—';
    var n = Number(ms);
    if (n < 1000) return Math.round(n) + ' ms';
    var s = n / 1000;
    return (s < 100 ? s.toFixed(2) : String(Math.round(s))).replace('.', ',') + ' s';
  }

  /** 11,2 -> "11,2" con un máximo de decimales, en formato es-AR. */
  function fmtNum(n, dec) {
    if (n === null || n === undefined || isNaN(n)) return '—';
    var d = dec === undefined ? 1 : dec;
    return Number(n).toFixed(d).replace('.', ',');
  }

  /* ------------------------------------------------------------------- api */

  /** Traduce los `reason` del hub a frases legibles en español (voseo). */
  function reasonText(reason, data) {
    var d = data || {};
    switch (reason) {
      case 'over_ram':
      case 'ram':
        var need = (d.need_mb !== undefined && d.need_mb !== null) ? fmtMb(d.need_mb) : 'lo que pide';
        var free = (d.free_mb !== undefined && d.free_mb !== null) ? fmtMb(d.free_mb) : 'la que hay';
        return 'No entra en el presupuesto de RAM: pide ' + need +
          ' y hay disponibles ' + free + '. Descargá algún modelo y volvé a intentar.';
      case 'model_not_loaded':
        return 'El modelo ' + (d.model || 'no identificado') + ' no está cargado. Cargalo desde el launcher.';
      case 'bad_request':
        return 'La petición no es válida' + (d.detail ? ': ' + d.detail : '.');
      case 'backend_down':
        return 'El backend no responde. Probá descargar el modelo y volver a cargarlo.';
      case 'timeout':
        return 'El backend tardó demasiado y cortamos la espera.';
      case 'busy':
        return 'El modelo está ocupado con otro request. Esperá a que termine.';
      case 'not_found':
        return 'Eso no existe en el hub.';
      default:
        if (d.detail) return 'Error: ' + d.detail;
        return 'El hub respondió un error' + (reason ? ' (' + reason + ')' : '') + '.';
    }
  }

  /**
   * fetch JSON al mismo origen. Acepta path y opts {method, body, signal, headers}.
   * Reenvía {ok:false, reason} como Error con mensaje legible; el Error lleva
   * .reason, .status y .data para que la página decida qué hacer (ej. over_ram).
   */
  function api(path, opts) {
    var o = opts || {};
    var init = { method: o.method || (o.body !== undefined ? 'POST' : 'GET'), headers: {} };
    if (o.body !== undefined && o.body !== null) {
      init.body = (typeof o.body === 'string') ? o.body : JSON.stringify(o.body);
      init.headers['Content-Type'] = 'application/json';
    }
    if (o.headers) {
      for (var k in o.headers) { if (Object.prototype.hasOwnProperty.call(o.headers, k)) init.headers[k] = o.headers[k]; }
    }
    if (o.signal) init.signal = o.signal;

    return fetch(path, init).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) {
          try { data = JSON.parse(text); } catch (e) { data = { ok: false, reason: 'bad_json', detail: text.slice(0, 200) }; }
        }
        if (!res.ok || !data || data.ok === false) {
          var reason = (data && data.reason) || ('http_' + res.status);
          var err = new Error(reasonText(reason, data));
          err.reason = reason;
          err.status = res.status;
          err.data = data || {};
          throw err;
        }
        return data;
      });
    }, function () {
      var err = new Error('No se pudo hablar con el hub: está en 127.0.0.1:8180 y tiene que estar corriendo.');
      err.reason = 'hub_unreachable';
      err.status = 0;
      err.data = {};
      throw err;
    });
  }

  function getState() { return api('/api/state'); }
  function getModelLog(id, n) { return api('/api/log/' + encodeURIComponent(id) + '?n=' + (n || 200)); }
  function loadModel(id) { return api('/api/models/' + encodeURIComponent(id) + '/load', { method: 'POST' }); }
  function unloadModel(id) { return api('/api/models/' + encodeURIComponent(id) + '/unload', { method: 'POST' }); }

  /* ------------------------------------------------------------------ poll */

  /**
   * Llama a fn (puede ser async) cada `ms`, sin solapar ejecuciones ni reintentar
   * antes de que termine la anterior. Si fn devuelve `false`, se corta.
   * Devuelve stop().
   */
  function poll(fn, ms) {
    var period = ms || 1500;
    var stopped = false;
    var timer = null;
    var running = false;

    function tick() {
      if (stopped) return;
      if (running) { timer = setTimeout(tick, period); return; }
      running = true;
      var out;
      try {
        out = fn();
      } catch (e) {
        running = false;
        timer = setTimeout(tick, period);
        return;
      }
      Promise.resolve(out).then(function (res) {
        running = false;
        if (stopped) return;
        if (res === false) { stopped = true; return; }
        timer = setTimeout(tick, period);
      });
    }

    tick();
    return function stop() {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }

  /* --------------------------------------------------------------- RAM bar */

  /**
   * Dibuja el medidor de RAM dentro de `el` (usa las clases de app.css).
   * La barra va a escala del PRESUPUESTO de modelos: el relleno es lo cargado,
   * la marca punteada recuerda cuánto hay realmente libre en la laptop.
   * Acepta el objeto `ram` de /api/state o un state completo.
   */
  function ramBar(el, ram) {
    if (!el) return;
    var data = (ram && ram.ram) ? ram.ram : (ram || {});
    var budget = Number(data.budget_mb) || 0;
    var used = Number(data.used_models_mb) || 0;
    var free = Number(data.available_mb) || 0;
    var total = Number(data.total_mb) || 0;
    var scale = budget > 0 ? budget : (total || 1);

    var pct = Math.max(0, Math.min(100, (used / scale) * 100));
    var over = used > budget && budget > 0;
    var freePct = Math.max(0, Math.min(100, (free / scale) * 100));

    el.classList.add('ram-meter');

    var head = 'Modelos cargados <span class="ram-value' + (over ? ' ram-value--warn' : ' ram-value--aqua') + '">' +
      esc(fmtMb(used)) + '</span> de <span class="ram-value">' + esc(fmtMb(budget)) + '</span> de presupuesto';

    var legend = '<span>Presupuesto <b>' + esc(fmtMb(budget)) + '</b></span>' +
      '<span>Disponible real <b>' + esc(fmtMb(free)) + '</b></span>' +
      '<span>Total de la laptop <b>' + esc(fmtMb(total)) + '</b></span>';

    var note = over
      ? '<span class="notice notice--warn">Pasaste el presupuesto: descargá algo antes de cargar otro modelo.</span>'
      : '<span class="ram-fill-hint">Quedan ' + esc(fmtMb(Math.max(0, budget - used))) + ' dentro del presupuesto.</span>';

    el.innerHTML =
      '<div class="ram-head mono">' + head + '</div>' +
      '<div class="ram-track" role="img" aria-label="' +
        esc('RAM de modelos: ' + fmtMb(used) + ' de ' + fmtMb(budget) + ' presupuestados; disponible real ' + fmtMb(free)) + '">' +
        '<div class="ram-fill' + (over ? ' is-over' : '') + '" style="width:' + pct.toFixed(1) + '%"></div>' +
        '<div class="ram-marker" style="left:' + freePct.toFixed(1) + '%"></div>' +
      '</div>' +
      '<div class="ram-legend">' + legend + '</div>' +
      '<div class="ram-legend">' + note + '</div>';
  }

  /* ------------------------------------------------------------- badestado */

  var STATUS = {
    off: { label: 'apagado', cls: 'badge--off' },
    loading: { label: 'cargando', cls: 'badge--loading' },
    ready: { label: 'listo', cls: 'badge--ready' },
    busy: { label: 'ocupado', cls: 'badge--busy' },
    error: { label: 'error', cls: 'badge--error' },
    unknown: { label: 'sin datos', cls: 'badge--off' }
  };

  function statusInfo(status) {
    return STATUS[status] || { label: status || '—', cls: 'badge--off' };
  }

  /** HTML de un chip de estado. `note` (ej. last_error) va en el title. */
  function badgeHTML(status, note) {
    var info = statusInfo(status);
    var title = note ? esc(String(note)) : '';
    return '<span class="badge ' + info.cls + '" role="status"' +
      (title ? ' title="' + title + '"' : '') + '>' +
      '<span class="badge-dot" aria-hidden="true"></span>' + esc(info.label) + '</span>';
  }

  function renderBadge(el, status, note) {
    if (!el) return;
    el.innerHTML = badgeHTML(status, note);
  }

  /* ---------------------------------------------------------- marca SURUS */

  var ASSET_BASE = '/assets/surus';

  /** Lockup ∇ + SURUS (o sólo el símbolo con {mark:true}). */
  function lockupHTML(opts) {
    var o = opts || {};
    var src = o.mark ? ASSET_BASE + '/surus-symbol.svg' : ASSET_BASE + '/surus-lockup-on-dark.svg';
    var cls = o.mark ? 'lockup-mark' : 'lockup';
    return '<img class="' + cls + '" src="' + src + '" width="' + (o.mark ? 26 : 105) +
      '" height="' + (o.mark ? 26 : 26) + '" alt="SURUS">';
  }

  /** Header compartido: lockup + título + acciones a la derecha. */
  function headerHTML(opts) {
    var o = opts || {};
    return '<header class="site-header">' +
      lockupHTML({ mark: !!o.mark }) +
      '<span class="header-sep" aria-hidden="true"></span>' +
      '<span class="header-title">' + esc(o.title || 'Inferencia local · tus demos') + '</span>' +
      (o.note ? '<span class="header-note">' + esc(o.note) + '</span>' : '') +
      '<span class="header-right">' + (o.right || '') + '</span>' +
      '</header>';
  }

  /* ------------------------------------------------------------ métricas */

  /** Resumen compacto de metrics del state, en mono, para una celda. */
  function metricsSummary(metrics, max) {
    if (!metrics) return '';
    var keys = Object.keys(metrics).filter(function (k) {
      var v = metrics[k];
      return v !== null && v !== undefined && typeof v !== 'object';
    });
    if (!keys.length) return '';
    var limit = max || 3;
    var parts = keys.slice(0, limit).map(function (k) {
      var v = metrics[k];
      var out;
      if (/ms$|_ms|ms_/.test(k)) out = fmtMs(v);
      else if (typeof v === 'number') {
        var a = Math.abs(v);
        out = fmtNum(v, a < 10 ? 2 : (a < 100 ? 1 : 0));
      } else out = String(v);
      return k + ' ' + out;
    });
    return parts.join(' · ') + (keys.length > limit ? ' …' : '');
  }

  /* ------------------------------------------------------------- exports */

  global.Surus = {
    // red
    api: api,
    getState: getState,
    getModelLog: getModelLog,
    loadModel: loadModel,
    unloadModel: unloadModel,
    poll: poll,
    // formato
    esc: esc,
    fmtMb: fmtMb,
    fmtMs: fmtMs,
    fmtNum: fmtNum,
    group: group,
    metricsSummary: metricsSummary,
    // UI compartida
    ramBar: ramBar,
    badgeHTML: badgeHTML,
    renderBadge: renderBadge,
    statusInfo: statusInfo,
    lockupHTML: lockupHTML,
    headerHTML: headerHTML,
    ASSET_BASE: ASSET_BASE
  };
})(typeof window !== 'undefined' ? window : globalThis);

/* ============================================================================
   Panel de stack + monitor de recursos (auto-montado)
   Una página lo activa con <body data-stack="dedos"> y cargando app.js.
   Muestra, colapsado por default: las 4 capas (metal → kernels → engine → app),
   el comando real con que el hub sirve el modelo, y consumos en vivo.
   ========================================================================== */
(function (global) {
  'use strict';
  const LAYERS = [
    { key: 'metal', label: 'metal' },
    { key: 'kernels', label: 'kernels / runtime' },
    { key: 'engine', label: 'engine' },
    { key: 'app', label: 'app' }
  ];

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  const dash = function (v) { return (v === null || v === undefined || v === '') ? '—' : v; };
  const MB = function (v) { return (v === null || v === undefined) ? '—' : Math.round(v) + ' MB'; };

  async function getJSON(url) {
    const r = await fetch(url, { headers: { 'Accept': 'application/json' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  function mount(demoId) {
    const host = document.getElementById('stack') || document.body;
    const wrap = document.createElement('section');
    wrap.className = 'stk';
    wrap.setAttribute('aria-live', 'polite');
    const saved = global.localStorage && localStorage.getItem('surus.stack.open');
    let open = saved === '1';
    let stack = null;
    let timer = null;

    wrap.innerHTML =
      '<button class="stk-toggle" type="button" aria-expanded="false">' +
      '<span class="stk-mark">∇</span> stack y recursos <span class="stk-caret">+</span></button>' +
      '<div class="stk-body" hidden>' +
      '  <div class="stk-sec"><div class="stk-h">monitor de recursos</div><div class="stk-mon">cargando…</div></div>' +
      '  <div class="stk-sec"><div class="stk-h">cómo se sirve este modelo</div>' +
      '    <div class="stk-cmd"><code></code><button class="stk-copy" type="button">copiar</button></div></div>' +
      '  <div class="stk-sec"><div class="stk-h">el stack, del metal a la app</div><div class="stk-layers"></div></div>' +
      '</div>';

    const btn = wrap.querySelector('.stk-toggle');
    const body = wrap.querySelector('.stk-body');
    const mon = wrap.querySelector('.stk-mon');
    const cmdEl = wrap.querySelector('.stk-cmd code');
    const copyBtn = wrap.querySelector('.stk-copy');
    const layersEl = wrap.querySelector('.stk-layers');

    function setOpen(v) {
      open = v;
      body.hidden = !v;
      btn.setAttribute('aria-expanded', String(v));
      wrap.querySelector('.stk-caret').textContent = v ? '–' : '+';
      if (global.localStorage) localStorage.setItem('surus.stack.open', v ? '1' : '0');
      if (v) { tick(); if (!timer) timer = setInterval(tick, 2000); }
      else if (timer) { clearInterval(timer); timer = null; }
    }

    function renderMonitor(st) {
      const ram = st.ram || {}, cpu = st.cpu || {};
      const pct = ram.budget_mb ? Math.min(100, Math.round(100 * (ram.used_models_mb || 0) / ram.budget_mb)) : 0;
      const rows = (st.models || []).filter(function (m) { return m.status !== 'off'; })
        .map(function (m) {
          const rss = (m.metrics || {}).rss_mb;
          return '<div class="stk-mrow"><span class="stk-mid">' + esc(m.id) + '</span>' +
            '<span class="stk-mst">' + esc(m.status) + '</span>' +
            '<span class="stk-mrss">' + MB(rss || m.est_ram_mb) + '</span>' +
            '<span class="stk-mport">' + (m.port ? ' :' + m.port : '') + '</span></div>';
        }).join('') || '<div class="stk-mrow stk-none">ningún modelo cargado ahora</div>';
      mon.innerHTML =
        '<div class="stk-mrow"><span class="stk-mlbl">RAM disponible</span><span class="stk-mval">' + MB(ram.available_mb) + '</span></div>' +
        '<div class="stk-mrow"><span class="stk-mlbl">modelos / presupuesto</span><span class="stk-mval">' +
        MB(ram.used_models_mb) + ' / ' + MB(ram.budget_mb) + '</span></div>' +
        '<div class="stk-bar"><i style="width:' + pct + '%"></i></div>' +
        '<div class="stk-mrow"><span class="stk-mlbl">load avg (1m) / hilos lógicos</span><span class="stk-mval">' +
        dash(cpu.load1) + ' / ' + dash(cpu.cores) + '</span></div>' +
        '<div class="stk-mrow"><span class="stk-mlbl">hilos de inferencia</span><span class="stk-mval">' +
        dash(cpu.threads) + '</span></div>' +
        '<div class="stk-mrow"><span class="stk-mlbl">RSS del hub</span><span class="stk-mval">' + MB(cpu.hub_rss_mb) + '</span></div>' +
        rows;
    }

    function renderStack(s) {
      const d = (s.demos || {})[demoId] || {};
      layersEl.innerHTML = LAYERS.map(function (L) {
        const own = (s.demos && s.demos[demoId]) || {};
        const items = own[L.key] || ((s.common || {})[L.key] || []);
        return '<div class="stk-layer"><div class="stk-layer-h">' + esc(L.label) + '</div><ul>' +
          (items.length ? items.map(function (i) { return '<li>' + esc(i) + '</li>'; }).join('')
            : '<li class="stk-none">—</li>') + '</ul></div>';
      }).join('');
      const v = s.versions || {};
      layersEl.insertAdjacentHTML('beforeend',
        '<div class="stk-vers">versiones · llama.cpp ' + esc(dash(v.llama_cpp)) +
        ' · transcribe.cpp ' + esc(dash(v.transcribe_cpp)) +
        ' · torch ' + esc(dash(v.torch)) + ' · ultralytics ' + esc(dash(v.ultralytics)) +
        ' · fastapi ' + esc(dash(v.fastapi)) + ' · python ' + esc(dash(v.python)) + '</div>');
      cmdEl.textContent = d.serve_cmd || '—';
    }

    async function tick() {
      try {
        if (!stack) { stack = await getJSON('/api/stack'); renderStack(stack); }
        renderMonitor(stack.runtime_now || await getJSON('/api/state'));
      } catch (e) {
        mon.innerHTML = '<div class="stk-mrow stk-none">el hub no responde (' + esc(e.message) + ')</div>';
      }
    }

    btn.addEventListener('click', function () { setOpen(!open); });
    copyBtn.addEventListener('click', function () {
      const txt = cmdEl.textContent || '';
      if (navigator.clipboard) navigator.clipboard.writeText(txt);
      copyBtn.textContent = 'copiado';
      setTimeout(function () { copyBtn.textContent = 'copiar'; }, 1200);
    });

    if (host === document.body) document.body.appendChild(wrap);
    else host.appendChild(wrap);
    setOpen(open);
  }

  function boot() {
    const id = document.body.dataset && document.body.dataset.stack;
    // Si la página carga stack.js (el panel visual), ese toma el control.
    if (id && !global.SurusStack) mount(id);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
  global.Surus = global.Surus || {};
  global.Surus.stackPanel = mount;
})(typeof window !== 'undefined' ? window : globalThis);
