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

function showToast(message, type) {
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
  }, 3000);
}

// ---------------------------------------------------------------------------
// Button Handlers
// ---------------------------------------------------------------------------

function setButtonsDisabled(disabled) {
  document.querySelectorAll('.btn-row .btn').forEach(function(btn) {
    btn.disabled = disabled;
  });
}

function setMode(mode) {
  if (mode === 'live' && !confirm(
    'Switch to LIVE mode? The bot will be STOPPED. ' +
    'You must press Start to begin trading with real funds on Polymarket.'
  )) {
    return;
  }
  fetch('/api/mode', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: mode })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'error') {
        showToast('Mode switch refused: ' + (data.detail || ''), 'error');
      } else {
        showToast('Mode → ' + (data.mode || mode).toUpperCase(), 'success');
        setTimeout(function() { window.location.reload(); }, 600);
      }
    })
    .catch(function(err) { showToast('Mode switch failed: ' + err.message, 'error'); });
}

function handleStart() {
  setButtonsDisabled(true);
  fetch('/api/start', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      showToast('Bot started: ' + (data.detail || data.status), 'success');
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

function setTradeShares() {
  var el = document.getElementById('ctl-shares');
  if (!el) return;
  var v = parseFloat(el.value);
  if (!(v >= 5)) {
    showToast('Minimum order is 5 shares (Polymarket)', 'error');
    return;
  }
  if (!confirm('Set trade size to ' + v + ' shares? Applies to paper + live on the next tick.')) {
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

function setActiveModel() {
  var el = document.getElementById('ctl-model');
  if (!el) return;
  var v = el.value;
  if (!confirm('Switch the active trading model to "' + v + '"? Applies to paper + live on the next tick.')) {
    return;
  }
  fetch('/api/runtime-config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ key: 'active_model', value: v })
  })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'ok') {
        showToast('Active model → ' + data.value, 'success');
      } else {
        showToast('Switch failed: ' + (data.detail || 'unknown error'), 'error');
      }
      setTimeout(refreshAll, 300);
    })
    .catch(function(err) { showToast('Switch failed: ' + err.message, 'error'); });
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
    if (execEl) execEl.innerHTML = data.execution_view || '';
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
