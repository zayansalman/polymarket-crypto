/**
 * BTC 5m Binary Pricing Model Dashboard — Client-Side Logic
 *
 * - Tab switching (vanilla JS, no framework)
 * - Start / Stop / Refresh button handlers (fetch API)
 * - Server-Sent Events for live updates (replaces gr.Timer polling)
 * - Error handling and automatic reconnection
 * - Toast notifications
 */

// ---------------------------------------------------------------------------
// Tab Switching
// ---------------------------------------------------------------------------

function showTab(tabName) {
  // Hide all tab contents
  document.querySelectorAll('.tab-content').forEach(function(el) {
    el.classList.remove('active');
  });
  // Deactivate all tab buttons
  document.querySelectorAll('.tab-btn').forEach(function(el) {
    el.classList.remove('active');
  });
  // Activate selected tab
  var contentEl = document.getElementById('tab-' + tabName);
  var btnEl = document.querySelector('.tab-btn[data-tab="' + tabName + '"]');
  if (contentEl) contentEl.classList.add('active');
  if (btnEl) btnEl.classList.add('active');
  // Store preference
  try { localStorage.setItem('btc-dashboard-active-tab', tabName); } catch (e) {}
}

// Restore active tab on load
document.addEventListener('DOMContentLoaded', function() {
  try {
    var saved = localStorage.getItem('btc-dashboard-active-tab');
    if (saved) showTab(saved);
  } catch (e) {}
});

// Activity log dropdown: remember open/closed across reloads
document.addEventListener('DOMContentLoaded', function() {
  var fold = document.getElementById('activity-fold');
  if (!fold) return;
  try { fold.open = localStorage.getItem('btc-dashboard-activity-open') === '1'; } catch (e) {}
  fold.addEventListener('toggle', function() {
    try { localStorage.setItem('btc-dashboard-activity-open', fold.open ? '1' : '0'); } catch (e) {}
  });
});

// Panel folds inside the refreshed views (e.g. ORDER SIZE). Their HTML is
// replaced every few seconds, so open/closed is stored per fold and re-applied
// after each swap. The listener sits on the document, not the fold, so it
// survives innerHTML swaps. ``toggle`` does not bubble, hence the capture flag.
// No inline ontoggle: a <details open> fires toggle while the page is still
// loading, before this script exists, which threw "rememberFold is not
// defined". Missing that first event is harmless — restoreFolds applies the
// stored state on DOMContentLoaded.
function rememberFold(el) {
  if (!el || !el.dataset.fold) return;
  try { localStorage.setItem('fold:' + el.dataset.fold, el.open ? '1' : '0'); } catch (e) {}
}

function restoreFolds(root) {
  (root || document).querySelectorAll('details[data-fold]').forEach(function(el) {
    try {
      var v = localStorage.getItem('fold:' + el.dataset.fold);
      if (v !== null) el.open = v === '1';
    } catch (e) {}
  });
}

document.addEventListener('DOMContentLoaded', function() { restoreFolds(document); });
document.addEventListener('toggle', function(e) {
  var el = e.target;
  if (el && el.matches && el.matches('details[data-fold]')) rememberFold(el);
}, true);

// ---------------------------------------------------------------------------
// Toast Notifications
// ---------------------------------------------------------------------------

function showToast(message, type, ms) {
  type = type || 'info';
  var container = document.querySelector('.toast-container');
  if (!container) {
    container = document.createElement('div');
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  var toast = document.createElement('div');
  toast.className = 'toast ' + type;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(function() {
    toast.classList.add('fade-out');
    setTimeout(function() { toast.remove(); }, 300);
  }, ms || 3000);
}

// ---------------------------------------------------------------------------
// Button Handlers
// ---------------------------------------------------------------------------

function setButtonsDisabled(disabled) {
  document.querySelectorAll('.btn-row .btn').forEach(function(btn) {
    btn.disabled = disabled;
  });
}

// Per-process token from the page: LIVE selection and LIVE Start require it,
// so only a click in this dashboard can arm real money.
function dashboardHeaders() {
  var meta = document.querySelector('meta[name="dashboard-token"]');
  return {
    'Content-Type': 'application/json',
    'X-Dashboard-Token': meta ? meta.getAttribute('content') : ''
  };
}

// No browser dialogs anywhere (confirm/alert/prompt): the click is the intent.
function setMode(mode) {
  // Clicking LIVE IS the real-money consent — no env phrase, no dialog.
  fetch('/api/mode', {
    method: 'POST',
    headers: dashboardHeaders(),
    body: JSON.stringify({ mode: mode })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'error') {
        showToast(data.detail || 'Mode switch failed', 'error');
        return;
      }
      var newMode = data.mode || mode;
      document.querySelectorAll('.mode-opt').forEach(function(b) {
        b.classList.toggle('active', b.classList.contains(newMode));
      });
      var liveBtn = document.querySelector('.mode-opt.live');
      if (liveBtn && data.live_hint) {
        liveBtn.title = data.live_hint;
        liveBtn.setAttribute('data-armed', data.live_armed ? '1' : '0');
      }
      if (newMode === 'live' && data.live_armed === false) {
        // LIVE is always selectable; an unarmed selection just can't Start yet.
        showToast('LIVE — ' + (data.live_hint || 'not armed'), 'error', 5000);
      } else {
        showToast('Mode → ' + newMode.toUpperCase(), 'success');
      }
      refreshAll();
    })
    .catch(function(err) { showToast('Mode switch failed: ' + err.message, 'error'); });
}

function handleStart() {
  setButtonsDisabled(true);
  fetch('/api/start', { method: 'POST', headers: dashboardHeaders() })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'running' || data.status === 'mock_running') {
        showToast('Bot started: ' + (data.detail || data.status), 'success');
      } else if (data.status === 'error') {
        showToast(data.detail || 'Start failed', 'error', 6000);
      } else {
        showToast('Start refused: ' + (data.detail || data.status), 'error', 6000);
      }
      refreshAll();
    })
    .catch(function(err) {
      showToast('Start failed: ' + err.message, 'error');
      console.error('Start error:', err);
    })
    .finally(function() { setButtonsDisabled(false); });
}

function handleStop() {
  setButtonsDisabled(true);
  fetch('/api/stop', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      showToast('Bot stopped: ' + (data.detail || data.status), 'info');
      refreshAll();
    })
    .catch(function(err) {
      showToast('Stop failed: ' + err.message, 'error');
      console.error('Stop error:', err);
    })
    .finally(function() { setButtonsDisabled(false); });
}

// ORDER SIZE ticket: re-price the typed share count against the live asks the
// panel carries (data-up / data-down), and light Apply only when it differs
// from the saved size.
function updateTicket() {
  var el = document.getElementById('ctl-shares');
  var cost = document.getElementById('ctl-cost');
  if (!el || !cost) return;
  var n = parseFloat(el.value);
  var ok = n > 0;
  var fmt = function(v) { return '$' + v.toFixed(2); };
  ['up', 'down'].forEach(function(side) {
    var out = cost.querySelector('[data-cost="' + side + '"]');
    var px = parseFloat(cost.getAttribute('data-' + side));
    if (out) out.textContent = ok && px > 0 ? fmt(n * px) : '—';
  });
  var max = cost.querySelector('[data-cost="max"]');
  if (max) max.textContent = ok ? fmt(n) : '—';
  var apply = document.getElementById('ctl-apply');
  if (apply) apply.disabled = !ok || n === parseFloat(el.getAttribute('data-saved'));
}

function onSharesInput(el) {
  el.dataset.dirty = '1';
  updateTicket();
}

function setShares(n) {
  var el = document.getElementById('ctl-shares');
  if (!el) return;
  var min = parseFloat(el.min) || 5;
  el.value = Math.min(1000, Math.max(min, Math.round(n)));
  onSharesInput(el);
}

function stepShares(delta) {
  var el = document.getElementById('ctl-shares');
  if (el) setShares((parseFloat(el.value) || 0) + delta);
}

function pickShares(n) { setShares(n); }

// Refreshes replace panel HTML every few seconds — keep what the operator is
// typing (focused or edited-but-unsaved inputs, by id) so it isn't wiped.
function swapKeepingInputs(container, html) {
  var kept = [];
  var active = document.activeElement;
  container.querySelectorAll('input[id], select[id]').forEach(function(el) {
    if (el === active || el.dataset.dirty === '1') {
      kept.push({ id: el.id, value: el.value, focus: el === active, dirty: el.dataset.dirty });
    }
  });
  container.innerHTML = html;
  restoreFolds(container);  // a collapsed panel must stay collapsed across refreshes

  kept.forEach(function(k) {
    var el = document.getElementById(k.id);
    if (!el) return;
    el.value = k.value;
    if (k.dirty) el.dataset.dirty = k.dirty;
    // preventScroll matters: a bare focus() scrolls the input into view, so a
    // refresh every few seconds drags the page back to whatever was focused
    // however far the operator had scrolled away.
    if (k.focus) el.focus({ preventScroll: true });
  });
  // No window-level scroll restore here. Panels change height between
  // refreshes, so forcing the old pixel offset back lands the reader somewhere
  // different each time — which reads as the page jumping at random. The
  // browser keeps the scroll position by itself; preventScroll above is what
  // actually stops the jumping.
  updateTicket();  // a kept share count must be re-priced at the fresh quote
}

function setLossHalt() {
  var el = document.getElementById('halt-usd');
  if (!el) return;
  var v = parseFloat(el.value);
  if (isNaN(v) || v < 0) {
    showToast('Enter a loss halt in USD (0 or more)', 'error');
    return;
  }
  fetch('/api/runtime-config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: 'live_daily_loss_halt_usd', value: v })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'ok') {
        delete el.dataset.dirty;
        showToast('Loss halt → $' + Number(data.value).toFixed(2), 'success');
      } else {
        showToast('Update failed: ' + (data.detail || 'unknown error'), 'error');
      }
      setTimeout(refreshAll, 300);
    })
    .catch(function(err) { showToast('Update failed: ' + err.message, 'error'); });
}

function resetLossHalt() {
  fetch('/api/loss_halt/reset', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      showToast(data.halt_reset
        ? 'Loss halt reset — today\'s tally and peaks zeroed'
        : 'Bot is running — stop it to zero the loss-halt tally', 'success');
      setTimeout(refreshAll, 300);
    })
    .catch(function(err) { showToast('Reset failed: ' + err.message, 'error'); });
}

function setTradeShares() {
  var el = document.getElementById('ctl-shares');
  if (!el) return;
  var v = parseFloat(el.value);
  var min = parseFloat(el.min) || 5;
  if (!(v >= min)) {
    showToast('Minimum order is ' + min + ' shares (Polymarket)', 'error');
    return;
  }
  fetch('/api/runtime-config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: 'trade_shares', value: v })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'ok') {
        delete el.dataset.dirty;
        showToast('Trade size → ' + Number(data.value) + ' shares', 'success');
      } else {
        showToast('Update failed: ' + (data.detail || 'unknown error'), 'error');
      }
      setTimeout(refreshAll, 300);
    })
    .catch(function(err) { showToast('Update failed: ' + err.message, 'error'); });
}

function setKnob(name, kind) {
  var el = document.getElementById('knob-' + name);
  if (!el) return;
  var v;
  if (kind === 'bool') {
    v = el.checked;
  } else if (kind === 'enum') {
    v = el.value;
  } else {
    v = parseFloat(el.value);
    if (isNaN(v)) {
      showToast('Enter a number', 'error');
      return;
    }
  }
  var label = (el.getAttribute('aria-label') || name);
  fetch('/api/runtime-config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: name, value: v })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'ok') {
        showToast(label + ' → ' + data.value, 'success');
      } else {
        showToast('Update failed: ' + (data.detail || 'unknown error'), 'error');
      }
      setTimeout(refreshAll, 300);
    })
    .catch(function(err) { showToast('Update failed: ' + err.message, 'error'); });
}

function setStrategy(name) {
  var el = document.getElementById('strategy-' + name);
  if (!el) return;
  var on = el.checked;
  var label = (el.getAttribute('aria-label') || name);
  fetch('/api/runtime-config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: 'strategy', value: { name: name, enabled: on } })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'ok') {
        showToast(label + ' → ' + (on ? 'ON' : 'OFF'), 'success');
      } else {
        el.checked = !on;  // the switch must never show a state the bot isn't in
        showToast('Update failed: ' + (data.detail || 'unknown error'), 'error');
      }
      setTimeout(refreshAll, 300);
    })
    .catch(function(err) {
      el.checked = !on;
      showToast('Update failed: ' + err.message, 'error');
    });
}

function setMarket(kind, value) {
  var sel = document.querySelector('.mkt-sel');
  if (!sel) return;
  var active = function(k) {
    var b = sel.querySelector('.mkt-btn.active[data-' + k + ']');
    return b ? b.getAttribute('data-' + k) : '';
  };
  var next = { asset: active('asset'), timeframe: active('timeframe') };
  next[kind] = value;
  fetch('/api/runtime-config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: 'market', value: next })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status !== 'ok') {
        showToast('Market select failed: ' + (data.detail || 'unknown error'), 'error');
      }
      refreshAll();
    })
    .catch(function(err) { showToast('Market select failed: ' + err.message, 'error'); });
}

function handleRefresh() {
  setButtonsDisabled(true);
  showToast('Refreshing...', 'info');
  refreshAll()
    .then(function() {
      showToast('Data refreshed', 'success');
    })
    .catch(function(err) {
      showToast('Refresh failed: ' + err.message, 'error');
    })
    .finally(function() { setButtonsDisabled(false); });
}

// ---------------------------------------------------------------------------
// Data Refresh — updates DOM from JSON payload
// ---------------------------------------------------------------------------

function refreshAll() {
  return fetch('/api/data')
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(data) {
      updateDashboard(data);
    });
}

function updateDashboard(data) {
  if (!data) return;

  // Topbar Start/Stop visual state — driven by runtime.state so the user can
  // see at a glance which control is the live action.
  if (data.runtime) {
    var running = data.runtime.state === 'running';
    var startBtn = document.querySelector('.btn.start.btn-ctl');
    var stopBtn = document.querySelector('.btn.stop.btn-ctl');
    if (startBtn) {
      startBtn.disabled = running;
      startBtn.classList.toggle('is-active', !running);
      startBtn.classList.toggle('is-inactive', running);
      startBtn.title = running ? 'Bot is running' : 'Start the bot';
    }
    if (stopBtn) {
      stopBtn.disabled = !running;
      stopBtn.classList.toggle('is-active', running);
      stopBtn.classList.toggle('is-inactive', !running);
      stopBtn.title = running ? 'Stop the bot' : 'Bot is stopped';
    }
  }

  // Topbar market selector (selection + open-position glow)
  if (data.market_selector !== undefined) {
    var mktEl = document.getElementById('market-selector');
    if (mktEl) mktEl.innerHTML = data.market_selector || '';
  }

  // Execution view (status ribbon + strategy/market/perf/TCA/blotter)
  if (data.execution_view) {
    var execEl = document.getElementById('execution-content');
    if (execEl) swapKeepingInputs(execEl, data.execution_view || '');
  }

  // Activity
  if (data.activity) {
    var act = document.getElementById('activity-content');
    if (act) act.innerHTML = data.activity || '';
  }
}

// ---------------------------------------------------------------------------
// Server-Sent Events (replaces gr.Timer polling)
// ---------------------------------------------------------------------------

var sseReconnectDelay = 1000;
var sseMaxReconnectDelay = 30000;
var sseReconnectTimer = null;
var eventSource = null;

function updateSseIndicator(state) {
  var dot = document.querySelector('.sse-dot');
  if (!dot) return;
  dot.classList.remove('connected', 'disconnected', 'connecting');
  dot.classList.add(state);
}

function connectSSE() {
  if (eventSource) {
    try { eventSource.close(); } catch (e) {}
  }

  updateSseIndicator('connecting');

  eventSource = new EventSource('/api/stream');

  eventSource.onopen = function() {
    updateSseIndicator('connected');
    sseReconnectDelay = 1000; // reset backoff
  };

  eventSource.onmessage = function(event) {
    try {
      var data = JSON.parse(event.data);
      updateDashboard(data);
    } catch (err) {
      console.error('SSE parse error:', err);
    }
  };

  eventSource.onerror = function() {
    updateSseIndicator('disconnected');
    try { eventSource.close(); } catch (e) {}
    eventSource = null;

    // Exponential backoff
    sseReconnectDelay = Math.min(sseReconnectDelay * 2, sseMaxReconnectDelay);
    sseReconnectTimer = setTimeout(connectSSE, sseReconnectDelay);
  };
}

function disconnectSSE() {
  if (sseReconnectTimer) {
    clearTimeout(sseReconnectTimer);
    sseReconnectTimer = null;
  }
  if (eventSource) {
    try { eventSource.close(); } catch (e) {}
    eventSource = null;
  }
  updateSseIndicator('disconnected');
}

// Start SSE on load
document.addEventListener('DOMContentLoaded', function() {
  connectSSE();
});

// Graceful disconnect on page unload
window.addEventListener('beforeunload', function() {
  disconnectSSE();
});
