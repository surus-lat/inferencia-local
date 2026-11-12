/* ============================================================================
   text.js — lógica compartida de las demos /d/llm y /d/search
   SURUS · inferencia-local-demos. Sin dependencias externas.
   Habla sólo con el hub del mismo origen: /api/llm/*, /api/search/*,
   /api/models/{id}/load. Los errores se muestran con avisos honestos
   (409 model_not_loaded → aviso + botón "Cargar modelo").
   ========================================================================== */
(function () {
  'use strict';

  /* ------------------------------------------------------------- helpers */

  var NF = new Intl.NumberFormat('es-AR');

  function $(sel, root) { return (root || document).querySelector(sel); }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = String(text);
    return n;
  }

  function fmtInt(n) {
    n = Number(n);
    return isFinite(n) ? NF.format(n) : '—';
  }

  function fmt1(n) {
    n = Number(n);
    return isFinite(n) ? n.toFixed(1).replace('.', ',') : '—';
  }

  function fmtMs(ms) {
    ms = Number(ms);
    if (!isFinite(ms)) return '—';
    return ms >= 1000 ? (ms / 1000).toFixed(1).replace('.', ',') + ' s' : Math.round(ms) + ' ms';
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* fetch → {status, data}. data = JSON parseado o {} si no se pudo parsear. */
  function api(method, url, body) {
    var opts = { method: method, headers: { 'Accept': 'application/json' } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (res) {
      return res.text().then(function (txt) {
        var data = {};
        try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = {}; }
        return { status: res.status, data: data };
      });
    });
  }

  /* Aviso con acción (botón "Cargar modelo" para 409 model_not_loaded). */
  function showNotice(node, msg, actionLabel, onAction) {
    if (!node) return;
    node.innerHTML = '';
    node.appendChild(el('span', null, msg));
    if (actionLabel && onAction) {
      node.appendChild(document.createElement('br'));
      var b = el('button', 'btn btn--sm', actionLabel);
      b.type = 'button';
      b.style.marginTop = '8px';
      b.addEventListener('click', function () {
        b.disabled = true;
        b.textContent = 'Cargando…';
        onAction(b);
      });
      node.appendChild(b);
    }
    node.hidden = false;
  }

  function hideNotice(node) { if (node) node.hidden = true; }

  function setStatus(idDot, idText, on, label) {
    var d = idDot && $(idDot), t = idText && $(idText);
    if (d) d.className = 'dot' + (on ? ' is-on' : '');
    if (t) t.textContent = label;
  }

  function copyText(text, btn) {
    function done() {
      if (!btn) return;
      var prev = btn.textContent;
      btn.textContent = 'Copiado';
      setTimeout(function () { btn.textContent = prev; }, 1400);
    }
    function legacy() {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'absolute';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); } catch (e) { /* sin-opción: ya avisamos */ }
      document.body.removeChild(ta);
      done();
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, legacy);
    } else {
      legacy();
    }
  }

  /* Barra de progreso simple para estados "esperando". */
  function pendingBubble(text) {
    var b = el('div', 'bubble bubble--pending');
    b.appendChild(document.createTextNode(text + ' '));
    var dots = el('span', 'pending-dots');
    dots.textContent = '';
    b.appendChild(dots);
    return b;
  }

  /* Revelado palabra a palabra, calibrado con los tok/s declarados por el hub.
     El JSON llega completo (el hub no streamea); el revelado es visual y la
     métrica que se muestra sigue siendo la medida real. */
  function revealTokens(node, text, tokensPerSec, onDone) {
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var words = String(text == null ? '' : text).split(/\s+/).filter(Boolean);
    if (reduce || !words.length) {
      node.textContent = text == null ? '' : text;
      if (onDone) onDone();
      return;
    }
    var per = 1000 / Math.max(1, Number(tokensPerSec) || 12);
    per = Math.min(90, Math.max(10, per));
    var i = 0;
    (function step() {
      i += 1;
      node.textContent = words.slice(0, i).join(' ');
      if (i < words.length) setTimeout(step, per);
      else if (onDone) onDone();
    })();
  }

  /* --------------------------------------------------------- estado modelo
     Botón "Cargar modelo" → POST /api/models/{id}/load; resuelve ok. */
  function loadModel(modelId) {
    return api('POST', '/api/models/' + encodeURIComponent(modelId) + '/load', {})
      .then(function (r) { return !!r.data.ok; });
  }

  /* =========================================================== DEMO 2 · llm */

  function initLlm() {
    var sel = $('#model-select');
    var facts = $('#model-facts');
    var thinkingBtn = $('#thinking-switch');
    var chat = $('#chat');
    var chatEmpty = $('#chat-empty');
    var composer = $('#composer');
    var input = $('#input');
    var btnSend = $('#btn-send');
    var btnClear = $('#btn-clear');
    var notice = $('#llm-notice');
    var reason = $('#reason');
    var reasonPanel = $('#reason-panel');
    var btnCollapse = $('#btn-collapse');
    var turnMeta = $('#turn-meta');
    var piCmd = $('#pi-cmd');
    var btnCopy = $('#btn-copy');
    if (!sel || !chat || !composer) return;

    var EXAMPLE = '¿Qué pasa durante el prefill y por qué condiciona el TTFT cuando corro un modelo en la CPU de una laptop?';
    if (input) input.value = EXAMPLE;

    /* Referencias medidas en esta máquina (fallback si el config no trae métricas). */
    var REF = {
      'lfm3-1.7b': { label: 'LFM3-1.7B', params: '1.7B', quant: 'Q4_K_M', tps: '~11 tok/s', thinking: true },
      'mnn-pocket': { label: 'MNN-Pocket-0.3B', params: '0.3B', quant: 'Q4_1', tps: '~44 tok/s', thinking: false }
    };

    var models = [];
    var messages = [];      /* historial {role, content} */
    var turnReasons = {};   /* índice de turno assistant → reasoning */
    var busy = false;
    var thinkingOn = true;
    var piBase = null;      /* {cmd, base_url, notes} del config */
    var piModelInCmd = null;

    function selectedId() { return sel.value || 'lfm3-1.7b'; }
    function modelById(id) {
      for (var i = 0; i < models.length; i++) if (models[i].id === id) return models[i];
      return { id: id, label: (REF[id] && REF[id].label) || id };
    }
    function isThinking(id) {
      var m = modelById(id);
      if (m.id === 'lfm3-1.7b') return true;
      if (m.id === 'mnn-pocket') return false;
      return REF[id] ? REF[id].thinking : true;
    }

    function renderFacts() {
      if (!facts) return;
      var id = selectedId();
      var m = modelById(id);
      var ref = REF[id] || {};
      var tps = (m.metrics && m.metrics.tok_per_s) ? '~' + fmt1(m.metrics.tok_per_s) + ' tok/s'
              : (m.est_tps ? '~' + m.est_tps + ' tok/s' : ref.tps || '—');
      facts.innerHTML = '';
      [
        m.params || ref.params || '',
        m.quant || ref.quant || '',
        (m.size_mb ? fmtInt(m.size_mb) + ' MB' : ''),
        tps,
        isThinking(id) ? 'modelo thinking' : 'sin razonamiento'
      ].forEach(function (t) { if (t) facts.appendChild(el('span', 'chip', t)); });
    }

    function renderPiCmd() {
      if (!piCmd) return;
      var id = selectedId();
      var cmd = null;
      if (piBase && piBase.cmd) {
        if (piBase.cmd.indexOf('{model}') !== -1) cmd = piBase.cmd.replace(/\{model\}/g, id);
        else if (piBase.cmd.indexOf(id) !== -1) { cmd = piBase.cmd; piModelInCmd = id; }
        else if (piModelInCmd && piModelInCmd !== id) {
          var re = new RegExp(piModelInCmd.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g');
          cmd = piBase.cmd.replace(re, id);
        } else cmd = piBase.cmd;
      }
      if (!cmd) {
        cmd = 'pi --provider openai --model ' + id +
              ' --api-base-url http://127.0.0.1:8180/v1/proxy/' + id;
      }
      piCmd.textContent = cmd;
    }

    function syncThinking() {
      var t = isThinking(selectedId());
      if (!thinkingBtn) return;
      if (t) {
        thinkingBtn.removeAttribute('disabled');
        thinkingBtn.classList.toggle('is-on', thinkingOn);
        thinkingBtn.setAttribute('aria-checked', thinkingOn ? 'true' : 'false');
      } else {
        thinkingBtn.setAttribute('disabled', '');
        thinkingBtn.classList.remove('is-on');
        thinkingBtn.setAttribute('aria-checked', 'false');
      }
    }

    function buildSelect() {
      sel.innerHTML = '';
      models.forEach(function (m) {
        var ref = REF[m.id] || {};
        var label = m.label || ref.label || m.id;
        var bits = [label];
        var meta = [m.params || ref.params, m.quant || ref.quant].filter(Boolean).join(' · ');
        if (meta) bits.push(meta);
        var o = el('option', null, bits.join(' — '));
        o.value = m.id;
        sel.appendChild(o);
      });
    }

    function clearChat() {
      messages = [];
      turnReasons = {};
      chat.innerHTML = '';
      if (chatEmpty) {
        chat.appendChild(chatEmpty);
        chatEmpty.hidden = false;
      }
      if (reason) {
        reason.textContent = 'LFM3-1.7B es un modelo thinking: primero genera reasoning_content y después la respuesta final. Acá vas a ver ese razonamiento completo, turno por turno.';
        reason.classList.add('reason-empty');
      }
      if (turnMeta) turnMeta.innerHTML = '';
    }

    function addBubble(role, text) {
      if (chatEmpty) chatEmpty.hidden = true;
      var row = el('div', 'bubble-row' + (role === 'user' ? ' is-user' : ''));
      var b = el('div', 'bubble' + (role === 'user' ? ' bubble--user' : ''));
      b.textContent = text;
      row.appendChild(b);
      chat.appendChild(row);
      chat.scrollTop = chat.scrollHeight;
      return b;
    }

    function showReasoning(text, meta) {
      if (!reason) return;
      reason.classList.remove('reason-empty');
      if (!text) {
        reason.textContent = 'Sin razonamiento: este modelo no genera reasoning_content (o el modo thinking estuvo apagado).';
        reason.classList.remove('is-thinking');
      } else {
        reason.textContent = text;
        reason.classList.add('is-thinking');
      }
      if (turnMeta && meta) turnMeta.innerHTML = meta;
    }

    function metricsChips(bubble, data) {
      var t = data.timings || {};
      var u = data.usage || {};
      var foot = el('div', 'bubble-foot');
      foot.appendChild(el('span', 'chip', 'TTFT≈ ' + fmtMs(t.prompt_ms)));
      foot.appendChild(el('span', 'chip', (t.predicted_per_second != null ? fmt1(t.predicted_per_second) : '—') + ' tok/s decode'));
      foot.appendChild(el('span', 'chip', fmtInt(u.total_tokens != null ? u.total_tokens : (u.prompt_tokens || 0) + (u.completion_tokens || 0)) + ' tok'));
      var titleBits = [];
      if (t.prompt_per_second != null) titleBits.push('prefill ' + fmt1(t.prompt_per_second) + ' tok/s');
      if (t.predicted_ms != null) titleBits.push('decode ' + fmtMs(t.predicted_ms));
      if (titleBits.length) foot.setAttribute('title', titleBits.join(' · '));
      bubble.appendChild(foot);
    }

    function setBusy(v) {
      busy = v;
      if (btnSend) btnSend.disabled = v;
      if (input) input.disabled = v;
      if (!v && input) input.focus();
    }

    function failNotice(msg) { showNotice(notice, msg); }

    function send() {
      if (busy) return;
      var text = (input.value || '').trim();
      if (!text) { input.focus(); return; }
      hideNotice(notice);
      var model = selectedId();
      var thinking = isThinking(model) ? thinkingOn : false;
      messages.push({ role: 'user', content: text });
      addBubble('user', text);
      setBusy(true);

      var pend = pendingBubble(isThinking(model) && thinking ? 'Pensando (razona primero)…' : 'Generando…');
      chat.appendChild(pend);
      chat.scrollTop = chat.scrollHeight;
      function dropPending() { if (pend.parentNode) pend.parentNode.removeChild(pend); }

      api('POST', '/api/llm/chat', {
        model: model,
        messages: messages,
        max_tokens: 512,
        temperature: 0.6,
        thinking: thinking
      }).then(function (r) {
        dropPending();
        if (r.status === 409 && r.data.reason === 'model_not_loaded') {
          messages.pop();
          var missing = r.data.model || model;
          showNotice(notice,
            'El modelo ' + missing + ' no está cargado en el hub. Cargalo y se reenvía tu pregunta.',
            'Cargar modelo', function (b) {
              loadModel(missing).then(function (ok) {
                if (!ok) { b.disabled = false; b.textContent = 'Reintentar'; showNotice(notice, 'No se pudo cargar ' + missing + '. ¿El hub está corriendo?'); return; }
                showNotice(notice, missing + ' quedó cargado. Reenviando…');
                send();
              });
            });
          return;
        }
        if (!r.data.ok && r.status >= 400) {
          messages.pop();
          failNotice('Error del hub (' + r.status + (r.data.reason ? ' · ' + r.data.reason : '') + '): el turno no se envió.');
          return;
        }
        var content = r.data.content || '';
        var reasoning = r.data.reasoning || '';
        messages.push({ role: 'assistant', content: content });
        if (input) input.value = '';
        var bubble = addBubble('assistant', '');
        var idx = messages.length;
        turnReasons[idx] = reasoning;
        var t = r.data.timings || {};
        metricsChips(bubble, r.data);
        if (reasoning) {
          var chip = el('button', 'chip chip--link', 'ver razonamiento');
          chip.type = 'button';
          chip.addEventListener('click', function () {
            showReasoning(reasoning,
              '<span>turno <b>' + idx + '</b></span><span><b>' + esc(model) + '</b></span>');
          });
          bubble.querySelector('.bubble-foot').appendChild(chip);
        }
        var textNode = el('span');
        bubble.insertBefore(textNode, bubble.firstChild);
        revealTokens(textNode, content, t.predicted_per_second, function () {
          chat.scrollTop = chat.scrollHeight;
        });
        showReasoning(reasoning,
          '<span>turno <b>' + idx + '</b></span><span><b>' + esc(r.data.model || model) + '</b></span>' +
          (t.prompt_per_second != null ? '<span>prefill <b>' + fmt1(t.prompt_per_second) + ' tok/s</b></span>' : ''));
      }).catch(function () {
        dropPending();
        messages.pop();
        failNotice('No se pudo hablar con el hub. ¿Está corriendo el servidor en 127.0.0.1?');
      }).then(function () {
        setBusy(false);
      });
    }

    /* ---------------- eventos */

    if (thinkingBtn) {
      thinkingBtn.classList.add('is-on');
      thinkingBtn.addEventListener('click', function () {
        if (thinkingBtn.hasAttribute('disabled')) return;
        thinkingOn = !thinkingOn;
        syncThinking();
      });
    }
    sel.addEventListener('change', function () { renderFacts(); renderPiCmd(); syncThinking(); });
    composer.addEventListener('submit', function (e) { e.preventDefault(); send(); });
    if (input) {
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
      });
    }
    if (btnClear) btnClear.addEventListener('click', function () { hideNotice(notice); clearChat(); });
    if (btnCollapse) {
      btnCollapse.addEventListener('click', function () {
        var c = reasonPanel.classList.toggle('is-collapsed');
        btnCollapse.textContent = c ? 'Mostrar' : 'Ocultar';
        btnCollapse.setAttribute('aria-expanded', c ? 'false' : 'true');
      });
    }
    if (btnCopy) btnCopy.addEventListener('click', function () { copyText(piCmd.textContent || '', btnCopy); });

    /* ---------------- config inicial */

    setStatus('#dot-state', '#state-text', false, 'leyendo config…');
    api('GET', '/api/llm/config').then(function (r) {
      var cfg = r.data || {};
      models = (cfg.models || []).filter(function (m) { return m && m.id && (REF[m.id] || true); });
      if (!models.length) {
        models = [
          { id: 'lfm3-1.7b', label: REF['lfm3-1.7b'].label, params: '1.7B', quant: 'Q4_K_M' },
          { id: 'mnn-pocket', label: REF['mnn-pocket'].label, params: '0.3B', quant: 'Q4_1' }
        ];
      }
      buildSelect();
      if (piBase == null) piBase = cfg.pi || null;
      if (piBase && piBase.notes && $('#pi-note')) $('#pi-note').appendChild(document.createTextNode(' ' + piBase.notes));
      renderFacts(); renderPiCmd(); syncThinking();
      setStatus('#dot-state', '#state-text', true, 'config ok');
    }).catch(function () {
      models = [
        { id: 'lfm3-1.7b', label: REF['lfm3-1.7b'].label, params: '1.7B', quant: 'Q4_K_M' },
        { id: 'mnn-pocket', label: REF['mnn-pocket'].label, params: '0.3B', quant: 'Q4_1' }
      ];
      buildSelect();
      renderFacts(); renderPiCmd(); syncThinking();
      setStatus('#dot-state', '#state-text', false, 'sin config');
      failNotice('No se pudo leer /api/llm/config: la lista de modelos usa valores de referencia y los pedidos van a fallar si el hub no está corriendo.');
    });
  }

  /* ======================================================= DEMO 4 · search */

  function initSearch() {
    var form = $('#search-form');
    var input = $('#query');
    var btnSearch = $('#btn-search');
    var scopeSel = $('#scope-select');
    var chips = $('#chips');
    var results = $('#results');
    var resultsMeta = $('#results-meta');
    var notice = $('#search-notice');
    var camNotice = $('#camera-notice');
    var camVideo = $('#cam-preview');
    var camWrap = $('#cam-preview-wrap');
    var btnCamOn = $('#btn-cam-on');
    var btnCamCapture = $('#btn-cam-capture');
    var btnCamOff = $('#btn-cam-off');
    var modeTxt = $('#mode-txt');
    var modeImg = $('#mode-img');
    var thumbWrap = $('#thumb-wrap');
    var thumb = $('#thumb');
    var imageNote = $('#image-note');
    if (!form || !results) return;

    var cfg = {
      rows_total: 294595,
      indexed_rows: 294595,
      dims: 768,
      model: 'embeddinggemma-2-Q8_0',
      scopes: { p1: 2946, p5: 14730, p25: 73649, full: 294595 },
      corpus: 'UNRN/ri-conicet'
    };
    var busy = false;
    var lastRequest = null;   /* último pedido {body, label, mode}, para reintentar tras cargar el modelo */
    var camStream = null;     /* MediaStream activo; null = cámara apagada */

    function scopeValue() { return parseInt(scopeSel.value, 10) || cfg.scopes.p1; }

    function buildScope() {
      if (!scopeSel) return;
      scopeSel.innerHTML = '';
      [['p1', '1 %'], ['p5', '5 %'], ['p25', '25 %'], ['full', '100 %']].forEach(function (pair) {
        var rows = cfg.scopes[pair[0]];
        if (!rows) return;
        var o = el('option', null, pair[1] + ' · ' + NF.format(rows) + ' filas');
        o.value = String(rows);
        scopeSel.appendChild(o);
      });
      scopeSel.value = String(cfg.scopes.p5 || cfg.scopes.p1);
    }

    function setFact(id, v) { var n = id && $(id); if (n) n.textContent = v; }

    function fillCorpus() {
      setFact('#c-corpus', cfg.corpus);
      setFact('#c-model', cfg.model);
      setFact('#c-indexed', fmtInt(cfg.indexed_rows) + ' de ' + fmtInt(cfg.rows_total));
      setFact('#m-total', fmtInt(cfg.rows_total));
      setFact('#m-dims', fmtInt(cfg.dims));
    }

    function handleUrl(item) {
      var h = item.handle;
      if (h) {
        h = String(h);
        if (/^https?:\/\//i.test(h)) return h;
        if (/^hdl\.handle\.net/i.test(h)) return 'https://' + h;
        return 'https://hdl.handle.net/' + h.replace(/^\/+/, '');
      }
      return 'https://hdl.handle.net/11336/' + String(item.item_id || '').replace(/\//g, '-');
    }

    function renderMetrics(data) {
      var took = Number(data.took_ms);
      var embed = Number(data.embed_ms) || 0;
      var scan = Number(data.scan_ms) || 0;
      setFact('#m-took', isFinite(took) ? NF.format(Math.round(took)) + ' ms' : '—');
      setFact('#m-embed', fmtInt(Math.round(embed)));
      setFact('#m-scan', fmtInt(Math.round(scan)));
      setFact('#m-rows', fmtInt(data.scanned_rows) + ' de ' + fmtInt(data.rows_total || cfg.rows_total));
      var ebar = $('#m-embed-bar');
      var total = embed + scan;
      if (ebar && total > 0) ebar.style.width = Math.max(4, Math.round(100 * embed / total)) + '%';
    }

    function resultCard(item) {
      var card = el('li', 'result');
      var top = el('div', 'res-top');
      top.appendChild(el('h3', 'res-title', item.title || 'Sin título'));
      if (item.year) top.appendChild(el('span', 'res-year', String(item.year)));
      card.appendChild(top);

      var meta = el('div', 'res-meta');
      var authors = Array.isArray(item.authors) ? item.authors.join(', ') : (item.authors || '');
      if (authors) meta.appendChild(el('span', null, authors));
      if (item.type) meta.appendChild(el('span', null, item.type));
      if (item.language) meta.appendChild(el('span', null, item.language));
      card.appendChild(meta);

      var score = Number(item.score);
      var row = el('div', 'score-row');
      var track = el('span', 'score-track');
      var fill = el('span', 'score-fill');
      var pct = isFinite(score) ? Math.max(0, Math.min(1, score)) * 100 : 0;
      fill.style.width = pct.toFixed(1) + '%';
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(el('span', 'score-val', (isFinite(score) ? score.toFixed(3).replace('.', ',') : '—') + ' cos'));
      card.appendChild(row);

      var links = el('div', 'res-links mono');
      var a = el('a', null, 'hdl.handle.net');
      a.href = handleUrl(item);
      a.target = '_blank';
      a.rel = 'noopener';
      links.appendChild(a);
      if (item.pdf_url) {
        var p = el('a', null, 'PDF');
        p.href = String(item.pdf_url);
        p.target = '_blank';
        p.rel = 'noopener';
        links.appendChild(p);
      }
      card.appendChild(links);
      return card;
    }

    function setMode(mode) {
      if (modeTxt) modeTxt.classList.toggle('chip--aqua', mode === 'texto');
      if (modeImg) modeImg.classList.toggle('chip--aqua', mode === 'imagen');
    }

    function showThumb(dataUrl) {
      if (!thumbWrap) return;
      if (thumb) thumb.src = dataUrl;
      thumbWrap.hidden = false;
    }

    function hideThumb() {
      if (!thumbWrap) return;
      thumbWrap.hidden = true;
      if (thumb) thumb.removeAttribute('src');
    }

    function setButtons() {
      if (btnSearch) btnSearch.disabled = busy;
      if (btnCamOn) btnCamOn.disabled = !!camStream || busy;
      if (btnCamCapture) btnCamCapture.disabled = !camStream || busy;
      if (btnCamOff) btnCamOff.disabled = !camStream;
    }

    /* ---------------------------------------------------------- webcam */

    function stopCamera() {
      if (camStream) {
        camStream.getTracks().forEach(function (t) { t.stop(); }); // stop() = luz del lente apagada
        camStream = null;
      }
      if (camVideo) {
        try { camVideo.pause(); } catch (e) { /* el video ya puede estar suelto */ }
        camVideo.srcObject = null;
      }
      if (camWrap) camWrap.hidden = true;
      setButtons();
    }

    function startCamera() {
      if (camStream || busy) return;
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        showNotice(camNotice, 'Tu navegador no expone getUserMedia (o no es un origen seguro). Entrá con http://127.0.0.1 y probá de nuevo; la cámara quedó apagada.');
        return;
      }
      hideNotice(camNotice);
      if (btnCamOn) { btnCamOn.disabled = true; btnCamOn.textContent = 'Pidiendo permiso…'; }
      navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 } },
        audio: false
      }).then(function (stream) {
        camStream = stream;
        var vt = stream.getVideoTracks()[0];
        if (vt) vt.addEventListener('ended', stopCamera); // si desconectan el dispositivo
        if (camVideo) { camVideo.srcObject = stream; var p = camVideo.play(); if (p && p.catch) p.catch(function () {}); }
        if (camWrap) camWrap.hidden = false;
        if (btnCamOn) btnCamOn.textContent = 'Buscar con la webcam';
        setButtons();
      }).catch(function (err) {
        var name = err && err.name ? err.name : 'Error';
        stopCamera();
        if (btnCamOn) btnCamOn.textContent = 'Buscar con la webcam';
        showNotice(camNotice, 'No se pudo prender la cámara (' + name + '). Revisá si hay cámara disponible y si le diste permiso en la barra de direcciones; la cámara quedó apagada.');
      });
    }

    function captureAndSearch() {
      if (!camStream || busy) return;
      var vw = camVideo.videoWidth || 640;
      var vh = camVideo.videoHeight || 480;
      var scale = Math.min(1, 512 / Math.max(vw, vh)); // bajada chica: el mmproj no necesita 10 MB
      var canvas = document.createElement('canvas');
      canvas.width = Math.max(16, Math.round(vw * scale));
      canvas.height = Math.max(16, Math.round(vh * scale));
      var ctx = canvas.getContext('2d');
      ctx.drawImage(camVideo, 0, 0, canvas.width, canvas.height);
      var dataUrl = canvas.toDataURL('image/jpeg', 0.85);
      var b64 = dataUrl.slice(dataUrl.indexOf(',') + 1); // JPEG sin prefijo data:
      showThumb(dataUrl);
      runSearch({
        body: { image_b64: b64, k: 10, scope: scopeValue() },
        label: 'la foto capturada',
        mode: 'imagen'
      });
    }

    /* ------------------------------------------------------- búsqueda */

    function runSearch(req) {
      if (busy || !req) return;
      lastRequest = req;
      busy = true;
      setButtons();
      hideNotice(notice);
      if (imageNote) imageNote.hidden = req.mode !== 'imagen';
      if (req.mode === 'texto') hideThumb();
      setMode(req.mode);
      if (resultsMeta) resultsMeta.hidden = true;
      results.innerHTML = '';
      results.appendChild(el('div', 'results-empty', 'Buscando ' + req.label + ' en ' + NF.format(scopeValue()) + ' filas…'));

      api('POST', '/api/search', req.body).then(function (r) {
        var data = r.data || {};
        if (r.status === 409 && data.reason === 'model_not_loaded') {
          results.innerHTML = '';
          var missing = data.model || 'embeddinggemma';
          showNotice(notice,
            'El modelo de embeddings (' + missing + ') no está cargado en el hub.',
            'Cargar modelo', function (b) {
              loadModel(missing).then(function (ok) {
                if (!ok) { b.disabled = false; b.textContent = 'Reintentar'; showNotice(notice, 'No se pudo cargar ' + missing + '. ¿El hub está corriendo?'); return; }
                showNotice(notice, missing + ' quedó cargado. Buscando de nuevo…');
                runSearch(lastRequest);
              });
            });
          return;
        }
        if (r.status >= 400 || (!data.ok && !Array.isArray(data.results))) {
          results.innerHTML = '';
          results.appendChild(el('div', 'results-empty', 'Error del hub (' + r.status + (data.reason ? ' · ' + data.reason : '') + ').'));
          return;
        }
        renderMetrics(data);
        var list = data.results || [];
        results.innerHTML = '';
        if (resultsMeta) {
          resultsMeta.hidden = false;
          resultsMeta.innerHTML = '';
          resultsMeta.appendChild(el('span', null, list.length + (list.length === 1 ? ' resultado' : ' resultados')));
          resultsMeta.appendChild(el('span', null, '·'));
          resultsMeta.appendChild(el('span', null, NF.format(data.scanned_rows) + ' filas escaneadas'));
          resultsMeta.appendChild(el('span', null, '·'));
          resultsMeta.appendChild(el('span', null, 'took ' + fmtMs(data.took_ms)));
          resultsMeta.appendChild(el('span', null, '·'));
          resultsMeta.appendChild(el('span', null, 'modo ' + (data.mode || req.mode)));
        }
        if (!list.length) {
          results.appendChild(el('div', 'results-empty', req.mode === 'imagen'
            ? 'No encontramos publicaciones parecidas a esta foto en este alcance. Probá con otro encuadre o subí el alcance.'
            : 'No encontramos publicaciones para ' + req.label + ' en este alcance. Probá otras palabras o subí el alcance.'));
          return;
        }
        list.forEach(function (item) { results.appendChild(resultCard(item)); });
      }).catch(function () {
        results.innerHTML = '';
        results.appendChild(el('div', 'results-empty', 'No se pudo hablar con el hub. ¿Está corriendo el servidor en 127.0.0.1?'));
      }).then(function () {
        busy = false;
        setButtons();
      });
    }

    function runQuery(q) {
      q = String(q || '').trim();
      if (!q) { input.focus(); return; }
      runSearch({ body: { query: q, k: 10, scope: scopeValue() }, label: '«' + q + '»', mode: 'texto' });
    }

    form.addEventListener('submit', function (e) { e.preventDefault(); runQuery(input.value); });
    if (chips) {
      chips.addEventListener('click', function (e) {
        var b = e.target.closest ? e.target.closest('.chip--query') : null;
        if (!b) return;
        input.value = b.textContent;
        runQuery(b.textContent);
      });
    }
    if (input) {
      // volver a escribir apaga el indicador imagen: la próxima búsqueda es de texto
      input.addEventListener('input', function () { setMode('texto'); });
    }
    if (btnCamOn) btnCamOn.addEventListener('click', startCamera);
    if (btnCamCapture) btnCamCapture.addEventListener('click', captureAndSearch);
    if (btnCamOff) btnCamOff.addEventListener('click', stopCamera);
    setMode('texto');
    setButtons();

    buildScope();
    fillCorpus();
    setStatus('#dot-state', '#state-text', false, 'leyendo config…');
    api('GET', '/api/search/config').then(function (r) {
      var d = r.data || {};
      if (d.rows_total) cfg.rows_total = d.rows_total;
      if (d.indexed_rows) cfg.indexed_rows = d.indexed_rows;
      if (d.dims) cfg.dims = d.dims;
      if (d.model) cfg.model = d.model;
      if (d.corpus) cfg.corpus = d.corpus;
      if (d.scopes) cfg.scopes = d.scopes;
      buildScope();
      fillCorpus();
      setStatus('#dot-state', '#state-text', true, 'config ok');
    }).catch(function () {
      fillCorpus();
      setStatus('#dot-state', '#state-text', false, 'sin config');
      showNotice(notice, 'No se pudo leer /api/search/config: los alcances usan los valores de referencia del corpus.');
    });
  }

  /* --------------------------------------------------------------- boot */

  function boot() {
    var page = document.documentElement.getAttribute('data-page');
    if (page === 'llm') initLlm();
    else if (page === 'search') initSearch();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
