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

function updateShareValue() {
  var el = document.getElementById('ctl-shares');
  var out = document.getElementById('ctl-shares-val');
  if (!el || !out) return;
  var n = parseFloat(el.value);
  var px = parseFloat(out.getAttribute('data-px')) || 0;
  if (!(n > 0)) { out.textContent = '≈ $—'; return; }
  if (px > 0) {
    out.textContent = '≈ $' + (n * px).toFixed(2) + ' at ' + px.toFixed(2);
  } else {
    out.textContent = '≈ $' + (n * 0.5).toFixed(2) + '–$' + (n * 1).toFixed(2);
  }
}

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
  kept.forEach(function(k) {
    var el = document.getElementById(k.id);
    if (!el) return;
    el.value = k.value;
    if (k.dirty) el.dataset.dirty = k.dirty;
    if (k.focus) el.focus();
  });
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
  if (!(v >= 5)) {
    showToast('Minimum order is 5 shares (Polymarket)', 'error');
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
  postKnob(name, v, el.getAttribute('aria-label') || name);
}

// Save one runtime knob and toast the result. `shown` (optional) replaces the
// saved value in the success toast, e.g. 'Market' for 'market'.
function postKnob(name, value, label, shown) {
  return fetch('/api/runtime-config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: name, value: value })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'ok') {
        showToast(label + ' → ' + (shown || data.value), 'success');
      } else {
        showToast('Update failed: ' + (data.detail || 'unknown error'), 'error');
      }
      setTimeout(refreshAll, 300);
    })
    .catch(function(err) { showToast('Update failed: ' + err.message, 'error'); });
}

// EXECUTION card toggle: one click switches Model <-> Market (no Apply step).
function setExecutionStrategy(value) {
  var shown = value === 'market' ? 'Market' : 'Model';
  postKnob('execution_strategy', value, 'Execution strategy', shown);
}

// Market strategy: one click buys that side at the current ask. The running
// loop executes it through the normal pipeline; in LIVE it is a real order.
// The click is the intent — no dialog. Both buttons stay off until it answers.
function buyMarket(side) {
  var card = document.getElementById('exec-card');
  if (!card || window.pendingMarketOrder) return;
  var btn = card.querySelector('.mo-btn[data-side="' + side + '"]');
  var ask = btn ? parseFloat(btn.getAttribute('data-ask')) : NaN;
  window.pendingMarketOrder = { side: side };
  applyPendingMarketOrder();
  fetch('/api/market-order', {
    method: 'POST',
    headers: dashboardHeaders(),
    body: JSON.stringify({
      side: side,
      window_slug: card.getAttribute('data-window') || '',
      ask: ask > 0 ? ask : null,
      asset: card.getAttribute('data-asset') || '',
      timeframe: card.getAttribute('data-timeframe') || ''
    })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'filled' || data.status === 'placed') {
        var label = data.mode === 'live' ? 'LIVE' : 'Paper';
        var msg = label + ' BUY ' + (data.side || side);
        if (data.shares != null && data.price != null) {
          msg += ' ' + Math.round(Number(data.shares) * 100) / 100 +
            ' sh @ ' + Number(data.price).toFixed(3);
        }
        if (data.status === 'placed') msg += ' — placed, part still resting on the book';
        showToast(msg, 'success', 5000);
      } else if (data.status === 'pending') {
        showToast(data.detail || 'Order is still being processed', 'info', 6000);
      } else {
        showToast('Buy ' + side + ' not placed: ' + (data.detail || data.status), 'error', 6000);
      }
    })
    .catch(function(err) { showToast('Buy ' + side + ' failed: ' + err.message, 'error', 6000); })
    .finally(function() {
      window.pendingMarketOrder = null;
      setTimeout(refreshAll, 300);
    });
}

// Keep a waiting Buy visible: the SSE refresh replaces the card's HTML, so
// re-mark the clicked button and keep both buttons off until the click answers.
function applyPendingMarketOrder() {
  var pending = window.pendingMarketOrder;
  if (!pending) return;
  document.querySelectorAll('#exec-card .mo-btn').forEach(function(b) {
    b.disabled = true;
    b.classList.toggle('pending', b.getAttribute('data-side') === pending.side);
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

function handleRefreshBacktest() {
  var btn = document.getElementById('btn-refresh-backtest');
  if (btn) btn.disabled = true;
  fetch('/api/data')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var el = document.getElementById('backtest-content');
      if (el && data.backtest) el.innerHTML = data.backtest;
      showToast('Backtest report refreshed', 'success');
    })
    .catch(function(err) {
      showToast('Refresh failed: ' + err.message, 'error');
    })
    .finally(function() {
      if (btn) btn.disabled = false;
    });
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
    applyPendingMarketOrder();
  }

  // Activity
  if (data.activity) {
    var act = document.getElementById('activity-content');
    if (act) act.innerHTML = data.activity || '';
  }

  // History
  if (data.history) {
    var hist = document.getElementById('history-content');
    if (hist) hist.innerHTML = data.history || '';
  }

  // Backtest
  if (data.backtest) {
    var bt = document.getElementById('backtest-content');
    if (bt) bt.innerHTML = data.backtest || '';
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
