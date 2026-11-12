/* ============================================================================
   stack.js — panel visual del stack (4 capas) + monitor de recursos
   Autocontenido, sin dependencias. Expone window.SurusStack = { mount(demoId) }
   y se auto-monta en páginas con <body data-stack="<id>">.
   app.js detecta window.SurusStack y NO monta su panel viejo de .stk-*.
   Datos reales: GET /api/stack (una vez) y GET /api/state (poll mientras abierto).
   ========================================================================== */
(function (global) {
  'use strict';

  /* Se declara antes que nada: es la señal que lee app.js para no montar el
     panel viejo. Aunque el resto del archivo fallara, el guard igual aplica. */
  global.SurusStack = { mount: mount };

  var STORE_OPEN = 'surus.stack.open';
  var STORE_JOURNEY = 'surus.stack.journey';
  var POLL_MS = 2000;
  var COPIED_MS = 1200;
  var TRAY_MS = 4000;
  var DASH = '—';

  /* Nada de /home/marian en la UI: se recorta la raíz del proyecto. */
  var ROOT_RE = /[^\s"',;]*?inferencia-local-demos\//g;
  var HOME_RE = /\/(?:home|Users|mnt|srv|opt|var)\/[^\s"',;]{0,220}?(?=(?:models|vendor|data|web|hub|scripts)\/)/g;

  /* Orden didáctico del deck: de lo que se ve a lo que se ejecuta.
     fallback por si /api/stack no trajera layers_order. */
  var FALLBACK_ORDER = ['app', 'engine', 'kernels', 'metal'];

  var LAYER = {
    app: {
      label: 'app',
      desc: 'Lo que tocás: la página y el hub. Acá se mueve JSON; todavía no se multiplica nada.',
      versions: ['fastapi', 'uvicorn', 'python']
    },
    engine: {
      label: 'engine',
      desc: 'El motor arma el prompt, decide qué token sigue y mantiene la KV cache en RAM.',
      versions: ['llama_cpp', 'transcribe_cpp', 'ultralytics']
    },
    kernels: {
      label: 'kernels / runtime',
      desc: 'Las cuentas: ggml o libtorch multiplican matrices sobre el CPU con AVX2, sin GPU.',
      versions: ['torch', 'numpy']
    },
    metal: {
      label: 'metal',
      desc: 'El piso del sistema: lo que mide el sistema operativo. Si la RAM no alcanza, el motor ni arranca.',
      versions: []
    }
  };

  /* Un color del brand kit por capa: borde izquierdo, etiqueta y chips.
     Sin rellenos grandes en pastel, sin sombras, radio 3 px. */
  var SEG_PALETTE = [
    'var(--surus-aqua)',
    'var(--surus-nabla-cyan)',
    'var(--surus-sol)',
    'var(--surus-area-compras)',
    'var(--surus-area-seguros)',
    'var(--surus-area-legales)',
    'var(--surus-nabla-yellow)'
  ];

  /* ------------------------------------------------------------ utilidades */

  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /** 4202 -> "4.202" (agrupación es-AR, sin Intl, determinístico). */
  function group(n) {
    var s = String(Math.round(Math.abs(Number(n))));
    return (Number(n) < 0 ? '-' : '') + s.replace(/\B(?=(\d{3})+(?!\d))/g, '.');
  }

  function mb(v) {
    if (v === null || v === undefined || v === '' || isNaN(Number(v))) return DASH;
    return group(v) + ' MB';
  }

  /** Los .py y .h vienen como 0.0 MB: "0 MB" desorienta, se muestra "< 1 MB". */
  function fileMb(v) {
    if (v === null || v === undefined || isNaN(Number(v))) return DASH;
    var n = Number(v);
    if (n < 0.05) return '< 1 MB';
    return group(n) + ' MB';
  }

  /** 1.51 -> "1,51" (decimal con coma, formato es-AR). */
  function num(v, dec) {
    if (v === null || v === undefined || v === '' || isNaN(Number(v))) return DASH;
    var s = Number(v).toFixed(dec === undefined ? 2 : dec);
    return s.indexOf('.') >= 0 ? s.replace('.', ',') : s;
  }

  function relativize(s) {
    if (!s) return '';
    return String(s).replace(ROOT_RE, '').replace(HOME_RE, '').trim();
  }

  function getJSON(url) {
    return fetch(url, { headers: { Accept: 'application/json' } }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function store(key, val) {
    try {
      if (val === null) global.localStorage.removeItem(key);
      else global.localStorage.setItem(key, val);
    } catch (e) { /* sin localStorage (modo privado): no rompe nada */ }
  }

  function read(key) {
    try { return global.localStorage.getItem(key); } catch (e) { return null; }
  }

  function reducedMotion() {
    return !!(global.matchMedia &&
      global.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function copyText(txt) {
    if (!txt) return Promise.resolve(false);
    if (global.navigator && navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(txt).then(
        function () { return true; },
        function () { return legacyCopy(txt); }
      );
    }
    return Promise.resolve(legacyCopy(txt));
  }

  function legacyCopy(txt) {
    try {
      var ta = document.createElement('textarea');
      ta.value = txt;
      ta.setAttribute('readonly', '');
      ta.className = 'sx-ta';
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return ok;
    } catch (e) { return false; }
  }

  /** Qué motor usa esta demo, deducido del comando real que reporta el hub. */
  function engineFamily(cmd) {
    var s = String(cmd || '').toLowerCase();
    if (s.indexOf('llama-server') >= 0 || s.indexOf('llama.cpp') >= 0) return 'llama_cpp';
    if (s.indexOf('transcribe') >= 0 || s.indexOf('asrtool') >= 0) return 'transcribe_cpp';
    if (s.indexOf('ultralytics') >= 0 || s.indexOf('yolo') >= 0) return 'torch';
    return null;
  }

  /** El chip de versión real que corresponde a cada capa (de /api/stack.versions). */
  function versionChips(key, versions, family) {
    if (key === 'app') {
      var out = [];
      if (versions.fastapi) out.push({ name: 'FastAPI', ver: shortVer(versions.fastapi), full: versions.fastapi });
      if (versions.uvicorn) out.push({ name: 'uvicorn', ver: shortVer(versions.uvicorn), full: versions.uvicorn });
      return out;
    }
    if (key === 'engine') {
      var v = family === 'llama_cpp' ? versions.llama_cpp
        : family === 'transcribe_cpp' ? versions.transcribe_cpp
        : family === 'torch' ? (versions.ultralytics || versions.torch) : null;
      return v ? [{ name: 'versión', ver: shortVer(v), full: v }] : [];
    }
    if (key === 'kernels') {
      var out2 = [];
      if (family === 'torch' && versions.torch) out2.push({ name: 'torch', ver: shortVer(versions.torch), full: versions.torch });
      if (versions.python) out2.push({ name: 'Python', ver: shortVer(versions.python), full: versions.python });
      return out2;
    }
    return [];
  }

  /** "0.9.0-dev (build 1, commit 827466b)" -> "0.9.0-dev" (el resto va en el title). */
  function shortVer(v) {
    var s = String(v == null ? '' : v).trim();
    var i = s.indexOf(' (');
    return i > 0 ? s.slice(0, i) : s;
  }

  /** Chips de la capa metal: el CPU, la RAM y si hay AVX2 / GPU. */
  function metalChips(lines) {
    var cpu = lines[0] || '';
    var out = [];
    if (cpu.indexOf('AVX2 sí') >= 0) out.push({ ver: 'AVX2' });
    else if (cpu.indexOf('AVX2 no') >= 0) out.push({ ver: 'sin AVX2' });
    if (/sin GPU/i.test(lines.join(' '))) out.push({ ver: 'sin GPU' });
    return out;
  }

  /** "Nombre · detalle" -> chip con nombre destacado; si no hay separador, todo texto. */
  function normChip(raw) {
    if (raw && typeof raw === 'object') {
      return { name: raw.name || '', ver: raw.ver || '', full: raw.full || '', text: '' };
    }
    var s = String(raw == null ? '' : raw).trim();
    if (!s) return null;
    var i = s.indexOf(' · ');
    return i > 0
      ? { name: s.slice(0, i), ver: '', full: '', text: s.slice(i + 3) }
      : { name: '', ver: '', full: '', text: s };
  }

  function chipHTML(raw, cls) {
    var c = normChip(raw);
    if (!c) return '';
    var inner = '';
    if (c.name) inner += '<b>' + esc(c.name) + '</b>';
    if (c.ver) {
      inner += '<i' + (c.full ? ' title="' + esc(c.full) + '"' : '') + '>' + esc(c.ver) + '</i>';
    }
    if (c.text) inner += (c.name ? '<span class="sx-dot-sep"> · </span>' : '') + '<span>' + esc(c.text) + '</span>';
    return '<span class="sx-chip ' + (cls || '') + '">' + inner + '</span>';
  }

  /* --------------------------------------------------------- estado vivo */

  var S = {
    demo: null, stack: null, state: null,
    open: false, loaded: false, timer: null,
    els: {}, copiedTimers: {}, trayTimer: null
  };

  /* -------------------------------------------------------------- renderers */

  function layerHTML(key, s) {
    var meta = LAYER[key];
    var demo = (s.demos || {})[S.demo] || {};
    /* metal y app vienen en common (son iguales para las 5 demos);
       kernels y engine son específicos de cada demo. */
    var raw = Object.prototype.hasOwnProperty.call(demo, key) ? demo[key] : ((s.common || {})[key] || []);
    if (!Array.isArray(raw)) raw = [];
    var versions = s.versions || {};
    var family = engineFamily(demo.serve_cmd);

    /* En la cabecera de la banda van las herramientas con su versión real;
       abajo, hasta 3 chips con las piezas concretas de esta demo. */
    var head = [];
    if (key === 'metal') metalChips(raw).forEach(function (c) { head.push(chipHTML(c, 'sx-chip-ver')); });
    else versionChips(key, versions, family).forEach(function (c) { head.push(chipHTML(c, 'sx-chip-ver')); });

    var chips = [];
    for (var j = 0; j < raw.length && chips.length < 3; j++) chips.push(chipHTML(raw[j]));

    return '<div class="sx-layer sx-l-' + key + '">' +
      '<div class="sx-lh">' +
        '<span class="sx-tag">' + esc(meta.label) + '</span>' +
        '<span class="sx-lh-chips">' + head.join('') + '</span>' +
        '<span class="sx-arrow" aria-hidden="true">↓</span>' +
      '</div>' +
      '<p class="sx-desc">' + esc(meta.desc) + '</p>' +
      '<div class="sx-chips">' + (chips.join('') || '<span class="sx-chip sx-miss">' + DASH + '</span>') + '</div>' +
      '</div>';
  }

  function renderStack() {
    var s = S.stack;
    var layers = S.els.layers;
    if (!s) {
      layers.innerHTML = '<div class="sx-note">el stack no cargó: el hub no responde ' + DASH + '</div>';
      S.els.stack.classList.remove('sx-ready');
      return;
    }
    var order = Array.isArray(s.layers_order) ? s.layers_order.slice().reverse() : FALLBACK_ORDER.slice();
    var html = '';
    for (var i = 0; i < order.length; i++) if (LAYER[order[i]]) html += layerHTML(order[i], s);
    layers.innerHTML = html;
    S.els.stack.classList.add('sx-ready');

    var demo = (s.demos || {})[S.demo] || {};
    S.els.cmd.textContent = relativize(demo.serve_cmd) || DASH;

    var files = Array.isArray(demo.files) ? demo.files : [];
    S.els.files.innerHTML = files.length
      ? files.map(function (f) {
          var path = relativize(f && f.path);
          var exists = !f || f.exists !== false;
          var openable = exists && /^(web|hub|scripts)\//.test(path);
          return '<button type="button" class="sx-file' + (openable ? ' sx-openable' : '') +
            (exists ? '' : ' sx-missing') + '" data-path="' + esc(path) + '"' +
            (openable ? ' data-open="' + esc(path) + '"' : '') + '>' +
            '<span class="sx-path">' + esc(path) + '</span>' +
            '<span class="sx-size">' + esc(fileMb(f && f.mb)) + '</span>' +
            (exists ? '' : '<span class="sx-flag">falta</span>') +
            '</button>';
        }).join('')
      : '<span class="sx-note">el hub no declara archivos para esta demo ' + DASH + '</span>';

    var miss = [];
    if (demo.serve_cmd === undefined) miss.push('serve_cmd');
    if (!Array.isArray(demo.files)) miss.push('files');
    if (!Array.isArray(s.layers_order)) miss.push('layers_order');
    if (!s.versions || !Object.keys(s.versions).length) miss.push('versions');
    var nModels = Array.isArray(demo.models) ? demo.models.length : 0;
    S.els.hint.textContent = miss.length
      ? 'le faltan campos al hub: ' + miss.join(', ') + ' ' + DASH
      : 'todas las rutas son relativas al ROOT del proyecto · ' +
        nModels + (nModels === 1 ? ' modelo' : ' modelos') + ' declarado(s) para esta demo';
  }

  function renderMonitor(st) {
    var ram = (st && st.ram) || {};
    var cpu = (st && st.cpu) || {};
    var list = (st && st.models) || [];

    var live = [];
    for (var i = 0; i < list.length; i++) {
      var m = list[i];
      var rss = (m.metrics && m.metrics.rss_mb) || m.rss_mb || 0;
      if (m.status === 'off' && !rss) continue;
      live.push({
        id: m.id,
        label: m.label || m.id,
        rss: rss,
        est: m.est_ram_mb || 0,
        status: m.status,
        port: m.port || (m.metrics && m.metrics.port) || null
      });
    }

    var used = live.reduce(function (a, x) { return a + x.rss; }, 0);
    var budget = ram.budget_mb || 0;

    S.els.n1.textContent = mb(ram.available_mb);
    S.els.n1sub.textContent = 'de ' + mb(ram.total_mb) + ' totales del equipo';
    S.els.n2.textContent = (ram.used_models_mb === undefined && !used)
      ? DASH
      : group(ram.used_models_mb === undefined ? used : ram.used_models_mb) + ' / ' + group(budget);
    S.els.n2sub.textContent = 'RSS real de los procesos vs presupuesto';
    S.els.n3.textContent = num(cpu.load1);
    S.els.n3sub.textContent = '1 min · ' + (cpu.cores ? group(cpu.cores) + ' hilos lógicos' : DASH) +
      ' · ' + (cpu.threads ? group(cpu.threads) + ' de inferencia' : DASH);
    S.els.load5.textContent = '5 min ' + num(cpu.load5);

    /* Barra segmentada: el eje es el presupuesto; cada modelo cargado es un
       segmento de un color; lo que sobra hasta el presupuesto queda en gris. */
    var scale = Math.max(budget, used, 1);
    var segs = '';
    for (var j = 0; j < live.length; j++) {
      var pct = Math.max(0, Math.min(100, (live[j].rss / scale) * 100));
      segs += '<span class="sx-seg" style="width:' + pct.toFixed(2) + '%;--sx-seg:' +
        SEG_PALETTE[j % SEG_PALETTE.length] + '" title="' +
        esc(live[j].label + ' · RSS ' + mb(live[j].rss) + (live[j].est ? ' · estimado ' + mb(live[j].est) : '')) +
        '"></span>';
    }
    var free = Math.max(0, scale - used) / scale * 100;
    segs += '<span class="sx-seg sx-free" style="width:' + free.toFixed(2) + '%" title="' +
      'margen hasta el presupuesto: ' + mb(Math.max(0, budget - used)) + '"></span>';
    S.els.bar.innerHTML = segs;

    S.els.legend.innerHTML = live.length
      ? live.map(function (x, idx) {
          return '<span class="sx-leg" style="--sx-seg:' + SEG_PALETTE[idx % SEG_PALETTE.length] + '">' +
            '<i></i><b>' + esc(x.label) + '</b><u>' + mb(x.rss || x.est) + '</u>' +
            '<s>' + esc(x.status) + (x.port ? ' :' + x.port : '') + '</s></span>';
        }).join('')
      : '<span class="sx-note">ningún modelo cargado ahora: la barra muestra solo el presupuesto de ' +
        group(budget) + ' MB ' + DASH + '</span>';

    S.els.foot.textContent = 'el hub ' + mb(cpu.hub_rss_mb) + ' · modelos ' + mb(used) +
      ' · quedan ' + mb(Math.max(0, budget - used)) + ' de margen';

    var n = live.length;
    S.els.summary.textContent = n
      ? 'stack · ' + n + (n === 1 ? ' modelo' : ' modelos') + ' · ' + mb(ram.available_mb) + ' libres'
      : 'stack · sin modelos · ' + mb(ram.available_mb) + ' libres';
  }

  /* -------------------------------------------------------- viaje / abrir */

  function setJourneyLabel() {
    var btn = S.els.journey;
    if (!btn) return;
    btn.textContent = reducedMotion()
      ? 'ver el viaje del pedido (sin animación)'
      : 'ver el viaje del pedido';
  }

  function stopJourney() {
    S.els.stack.classList.remove('sx-journey');
    S.els.root.classList.remove('sx-journey');
    S.els.journey.setAttribute('aria-pressed', 'false');
    store(STORE_JOURNEY, null);
  }

  function runJourney() {
    if (!S.els.stack.classList.contains('sx-ready')) return;
    var box = S.els.stack;
    box.classList.remove('sx-journey');
    S.els.root.classList.remove('sx-journey');
    void box.offsetWidth;          /* reflujo: reinicia la animación CSS */
    box.classList.add('sx-journey');
    S.els.root.classList.add('sx-journey');
    S.els.journey.setAttribute('aria-pressed', 'true');
    store(STORE_JOURNEY, '1');
  }

  function tick() {
    return getJSON('/api/state').then(function (st) {
      S.state = st;
      renderMonitor(st);
    }).catch(function (e) {
      if (!S.state) {
        S.els.n1.textContent = DASH;
        S.els.n2.textContent = DASH;
        S.els.n3.textContent = DASH;
        S.els.n1sub.textContent = S.els.n2sub.textContent = S.els.n3sub.textContent = '';
        S.els.legend.innerHTML = '<span class="sx-note">el hub no responde (' + esc(e.message) + ')</span>';
        S.els.summary.textContent = 'stack · el hub no responde ' + DASH;
      }
    });
  }

  function ensureStack() {
    if (S.loaded) return Promise.resolve();
    S.loaded = true;
    return getJSON('/api/stack').then(function (s) {
      S.stack = s;
      renderStack();
      if (read(STORE_JOURNEY) === '1') runJourney();
    }).catch(function (e) {
      S.loaded = false;
      S.stack = null;
      renderStack();
      S.els.err.textContent = 'no se pudo leer /api/stack (' + e.message + ')';
    });
  }

  function setOpen(v) {
    S.open = !!v;
    S.els.body.hidden = !S.open;
    S.els.root.classList.toggle('is-open', S.open);
    S.els.toggle.setAttribute('aria-expanded', String(S.open));
    S.els.caret.textContent = S.open ? '–' : '+';
    store(STORE_OPEN, S.open ? '1' : '0');
    if (S.open) {
      ensureStack();
      if (!S.state) tick();
      if (!S.timer) S.timer = setInterval(tick, POLL_MS);
      /* Al reabrir, si el viaje estaba pedido se vuelve a mostrar. */
      if (read(STORE_JOURNEY) === '1') runJourney();
    } else if (S.timer) {
      clearInterval(S.timer);
      S.timer = null;
    }
  }

  /* ----------------------------------------------------- feedback visual */

  function flash(btn, promise) {
    var isFile = btn.classList.contains('sx-file');
    var label = isFile ? btn.querySelector('.sx-size') : btn;
    if (!label) return;
    var key = btn.dataset.path || btn.dataset.act || 'cmd';
    var old = label.textContent;
    var live = S.copiedTimers[key];
    if (live) { clearTimeout(live.id); if (live.restored) { label.textContent = old; btn.classList.remove('sx-copied'); } }
    Promise.resolve(promise).then(function () {
      btn.classList.add('sx-copied');
      label.textContent = 'copiada';
      S.copiedTimers[key] = {
        restored: false,
        id: setTimeout(function () {
          btn.classList.remove('sx-copied');
          label.textContent = old;
          S.copiedTimers[key] = { restored: true, id: 0 };
        }, COPIED_MS)
      };
    });
  }

  function tray(msg) {
    var el = S.els.tray;
    clearTimeout(S.trayTimer);
    el.hidden = false;
    if (el.firstChild) el.removeChild(el.firstChild);
    el.appendChild(document.createTextNode(msg));
    S.trayTimer = setTimeout(function () { el.hidden = true; }, TRAY_MS);
  }

  /* -------------------------------------------------------------- montaje */

  function mount(demoId) {
    var id = demoId || (document.body.dataset && document.body.dataset.stack);
    if (!id) return;
    S.demo = id;

    var root = document.createElement('section');
    root.className = 'sx';
    root.setAttribute('aria-label', 'stack y recursos de esta demo');
    root.innerHTML =
      '<div class="sx-body" hidden>' +
        '<div class="sx-head">' +
          '<div class="sx-head-l">' +
            '<div class="sx-kicker">∇ el stack de esta demo</div>' +
            '<div class="sx-title">' + esc(id) + '</div>' +
          '</div>' +
          '<div class="sx-tools">' +
            '<button class="sx-btn" type="button" data-act="journey" aria-pressed="false">ver el viaje del pedido</button>' +
            '<span class="sx-legend">el prefill baja · los tokens suben</span>' +
          '</div>' +
        '</div>' +

        '<div class="sx-stack">' +
          '<span class="sx-rail" aria-hidden="true"><i></i></span>' +
          '<div class="sx-layers"><div class="sx-note">cargando el stack…</div></div>' +
        '</div>' +
        '<div class="sx-err"></div>' +

        '<div class="sx-sec">' +
          '<div class="sx-h">monitor de recursos</div>' +
          '<div class="sx-nums">' +
            '<div><b data-n="1">' + DASH + '</b><i>RAM disponible</i><u data-n="1s"></u></div>' +
            '<div><b data-n="2">' + DASH + '</b><i>modelos / presupuesto</i><u data-n="2s"></u></div>' +
            '<div class="sx-n-load"><b data-n="3">' + DASH + '</b><i>load average</i>' +
              '<u data-n="3s"></u><u data-n="5s"></u></div>' +
          '</div>' +
          '<div class="sx-bar"></div>' +
          '<div class="sx-legend-ram"></div>' +
          '<div class="sx-foot"></div>' +
        '</div>' +

        '<div class="sx-sec">' +
          '<div class="sx-h">el comando que corre en tu máquina</div>' +
          '<div class="sx-cmd"><code></code>' +
            '<button class="sx-btn" type="button" data-act="copy-cmd">copiar</button></div>' +
        '</div>' +

        '<div class="sx-sec">' +
          '<div class="sx-h">archivos de esta demo · clic para copiar la ruta</div>' +
          '<div class="sx-files"></div>' +
          '<div class="sx-hint"></div>' +
        '</div>' +
      '</div>' +
      '<button class="sx-toggle" type="button" aria-expanded="false">' +
        '<span class="sx-mark">∇</span> <span class="sx-sum">stack</span> <span class="sx-caret">+</span>' +
      '</button>' +
      '<div class="sx-tray" hidden></div>';

    (document.getElementById('stack') || document.body).appendChild(root);

    function q(sel) { return root.querySelector(sel); }
    S.els = {
      root: root,
      body: q('.sx-body'),
      toggle: q('.sx-toggle'),
      caret: q('.sx-caret'),
      summary: q('.sx-sum'),
      tray: q('.sx-tray'),
      stack: q('.sx-stack'),
      layers: q('.sx-layers'),
      err: q('.sx-err'),
      rail: q('.sx-rail'),
      journey: q('[data-act="journey"]'),
      cmd: q('.sx-cmd code'),
      files: q('.sx-files'),
      hint: q('.sx-hint'),
      bar: q('.sx-bar'),
      legend: q('.sx-legend-ram'),
      foot: q('.sx-foot'),
      n1: q('[data-n="1"]'), n1sub: q('[data-n="1s"]'),
      n2: q('[data-n="2"]'), n2sub: q('[data-n="2s"]'),
      n3: q('[data-n="3"]'), n3sub: q('[data-n="3s"]'),
      load5: q('[data-n="5s"]')
    };

    S.els.toggle.addEventListener('click', function () { setOpen(!S.open); });
    S.els.journey.addEventListener('click', function () {
      if (S.els.stack.classList.contains('sx-journey')) stopJourney();
      else runJourney();
    });

    root.addEventListener('click', function (ev) {
      var btn = ev.target && ev.target.closest
        ? ev.target.closest('[data-act="copy-cmd"], .sx-file') : null;
      if (!btn) return;
      if (btn.dataset.act === 'copy-cmd') {
        flash(btn, copyText(S.els.cmd.textContent || ''));
        return;
      }
      var openTo = btn.dataset.open;
      if (openTo) {
        getJSON('/__open?path=' + encodeURIComponent(openTo)).then(function (r) {
          tray((r && r.opened ? 'abierto en el editor · ' : 'abierto · ') + openTo);
        }).catch(function () {
          tray('se copió la ruta, pero el hub no la pudo abrir · ' + openTo);
        });
      }
      flash(btn, copyText(btn.dataset.path || ''));
    });

    /* Con el dot terminando el ciclo se apaga la animación (una sola pasada). */
    S.els.rail.addEventListener('animationend', function (ev) {
      if (ev.animationName && ev.animationName.indexOf('sx-travel') === 0) stopJourney();
    });

    setJourneyLabel();
    if (global.matchMedia) {
      var mq = global.matchMedia('(prefers-reduced-motion: reduce)');
      if (mq.addEventListener) mq.addEventListener('change', setJourneyLabel);
    }

    setOpen(read(STORE_OPEN) === '1');
    /* Cerrado no hay timers, pero una lectura alcanza para el resumen del botón. */
    if (!S.open && !S.state) tick();
  }

  /* Auto-montaje: el launcher no tiene data-stack y queda sin panel. */
  function boot() {
    var id = document.body.dataset && document.body.dataset.stack;
    if (id && !S.els.root) mount(id);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})(typeof window !== 'undefined' ? window : globalThis);
