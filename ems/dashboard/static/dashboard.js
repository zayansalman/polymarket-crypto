/**
 * Polymarket Crypto EMS — Client-Side Logic
 *
 * - Panel folds and the STRATEGY card picker
 * - Strategy switches and SETTINGS knobs (fetch API)
 * - Server-Sent Events for live updates
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
  // Cards marked data-static hold reference text, not live data (the STRATEGY
  // card). The live node goes back in place of its fresh copy, so the pick and
  // the rendered maths survive without being redone every refresh.
  var statics = {};
  container.querySelectorAll('[data-static]').forEach(function(el) {
    statics[el.dataset.static] = el;
  });
  container.innerHTML = html;
  container.querySelectorAll('[data-static]').forEach(function(el) {
    var live = statics[el.dataset.static];
    if (live) el.replaceWith(live);
  });
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
}

// ---------------------------------------------------------------------------
// STRATEGY card: pick a strategy, read its summary
// ---------------------------------------------------------------------------

// A native dropdown closes the moment its node leaves the page, and every
// refresh moves the card. While the picker has focus the newest view is
// parked and applied once the pick is made — or after HOLD_MAX_MS, so an
// abandoned dropdown can't freeze the dashboard.
var HOLD_MAX_MS = 15000;
var heldExecView = null;
var heldSince = 0;

function pickerHasFocus() {
  var a = document.activeElement;
  return !!(a && a.id === 'strategy-pick');
}

function applyExecView(html) {
  var execEl = document.getElementById('execution-content');
  if (execEl) swapKeepingInputs(execEl, html);
}

function releaseHeldView() {
  if (heldExecView === null) return;
  var html = heldExecView;
  heldExecView = null;
  applyExecView(html);
}

function showStrategy(key) {
  var card = document.querySelector('.strategy-card');
  if (!card || !card.querySelector('.sc-body[data-strategy="' + key + '"]')) return;
  card.querySelectorAll('.sc-body').forEach(function(el) {
    el.hidden = el.dataset.strategy !== key;
  });
  var pick = document.getElementById('strategy-pick');
  if (pick) pick.value = key;
  var docs = card.querySelector('.sc-docs');
  if (docs) docs.href = '/strategy-docs/' + encodeURIComponent(key);
}

function pickStrategy(el) {
  showStrategy(el.value);
  try { localStorage.setItem('strategy-card:pick', el.value); } catch (e) {}
  el.blur();          // hands the refresh back to the stream...
  releaseHeldView();  // ...and applies what it held (onblur won't fire in a window without focus)
}

// Called by KaTeX's auto-render once it loads. Each summary is rendered once:
// the card node is kept across refreshes, so its maths stays rendered.
function renderStrategyMath() {
  if (typeof renderMathInElement !== 'function') return;
  document.querySelectorAll('.strategy-card .sc-body').forEach(function(el) {
    if (el.dataset.math === '1') return;
    renderMathInElement(el, {
      delimiters: [
        { left: '\\[', right: '\\]', display: true },
        { left: '\\(', right: '\\)', display: false }
      ],
      throwOnError: false
    });
    el.dataset.math = '1';
  });
}

document.addEventListener('DOMContentLoaded', function() {
  try {
    var saved = localStorage.getItem('strategy-card:pick');
    if (saved) showStrategy(saved);
  } catch (e) {}
});

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

  // Page body
  if (data.execution_view) {
    if (pickerHasFocus() && (heldExecView === null || Date.now() - heldSince < HOLD_MAX_MS)) {
      if (heldExecView === null) heldSince = Date.now();
      heldExecView = data.execution_view;
    } else {
      heldExecView = null;
      applyExecView(data.execution_view);
    }
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
