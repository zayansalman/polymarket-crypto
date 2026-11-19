/* Bitcoin Paper Trader: client for the simple screen.
 *
 * Polls GET /api/simple (read-only) every few seconds and drives the paper
 * controls: Start, Stop, the BTC 5 min / 15 min choice, and "switch back to
 * paper" when real-money mode was picked in the advanced console.
 *
 * Safety: this page never sends the X-Dashboard-Token header (the page does
 * not even have the token), and the server requires that token to select or
 * start LIVE. So nothing here can arm real money.
 *
 * All data goes into the DOM via textContent, never innerHTML.
 */
(function () {
  'use strict';

  var POLL_MS = 3000;
  var MINUS = '−';
  var SVG_NS = 'http://www.w3.org/2000/svg';

  var S = {
    data: null,
    offset: 0,          // server clock minus browser clock, in seconds
    inflight: false,
    busy: false,
    failures: 0,
    stopArmed: false,
    stopTimer: null,
    knownTrades: null,
    chartKey: '',
    rulesKey: '',
    chart: null,
    settingsDirty: false,
    settingsKey: '',
    buyId: undefined,   // open position shown in the big buy panel
  };
  var BASE_TITLE = document.title;

  function $(id) { return document.getElementById(id); }

  // ---------------------------------------------------------------- format

  var usd = new Intl.NumberFormat(undefined, {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  var usd0 = new Intl.NumberFormat(undefined, {
    style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0,
  });
  var plain2 = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

  function money(v) { return v == null ? '—' : usd.format(v); }

  function signedMoney(v) {
    if (v == null) return '—';
    var r = Math.round(v * 100) / 100;
    if (r === 0) return usd.format(0);
    // \u2060 (word joiner) keeps the sign on the same line as its amount.
    return (r > 0 ? '+' : MINUS) + '\u2060' + usd.format(Math.abs(r));
  }

  function cents(p) {
    if (p == null) return '—';
    var c = Math.round(p * 1000) / 10;
    return (c % 1 === 0 ? c.toFixed(0) : c.toFixed(1)) + '¢';
  }

  function percent(x, digits) {
    if (x == null) return '—';
    return (x * 100).toFixed(digits == null ? 0 : digits) + '%';
  }

  // 5 -> "5×", 2.5 -> "2.5×"
  function times(k) { return plain2.format(Math.round(k * 100) / 100) + '\u00d7'; }

  function signedPercent(x) {
    if (x == null) return '';
    var v = Math.round(x * 1000) / 10;
    if (v === 0) return '0%';
    return (v > 0 ? '+' : MINUS) + Math.abs(v).toFixed(1) + '%';
  }

  function clock(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }

  function isToday(ts) {
    var d = new Date(ts * 1000);
    var n = new Date();
    return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() &&
      d.getDate() === n.getDate();
  }

  function shortDate(ts) {
    return new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  function dayTime(ts) {
    if (ts == null) return '—';
    return (isToday(ts) ? 'Today' : shortDate(ts)) + ', ' + clock(ts);
  }

  function mmss(sec) {
    sec = Math.max(0, Math.floor(sec));
    var m = Math.floor(sec / 60);
    var s = sec % 60;
    return m + ':' + (s < 10 ? '0' : '') + s;
  }

  function ago(sec) {
    if (sec == null) return '';
    if (sec < 5) return 'just now';
    if (sec < 90) return Math.round(sec) + ' seconds ago';
    var m = Math.round(sec / 60);
    return m + ' minutes ago';
  }

  function nowTs() { return Date.now() / 1000 + S.offset; }
  function windowSeconds(tf) { return tf === '15m' ? 900 : 300; }
  function tfLabel(tf) { return tf === '15m' ? '15 min' : '5 min'; }
  function shares(n) { return plain2.format(n) + (n === 1 ? ' share' : ' shares'); }

  // ---------------------------------------------------------------- DOM

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function svg(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  function setText(id, text) {
    var node = $(id);
    if (node) node.textContent = text;
  }

  function setDirection(node, v) {
    node.classList.toggle('is-up', v != null && v > 0);
    node.classList.toggle('is-down', v != null && v < 0);
  }

  function toast(message, kind) {
    var box = $('toasts');
    var t = el('div', 'toast', message);
    if (kind) t.dataset.kind = kind;
    box.appendChild(t);
    setTimeout(function () { t.remove(); }, kind === 'error' ? 6000 : 3200);
  }

  // ---------------------------------------------------------------- status bar

  function renderStatus(d) {
    var node = $('status');
    var state = 'stopped';
    var text = 'Stopped';
    if (d.bot.state === 'running') {
      var fresh = d.market && d.market.fresh;
      state = fresh ? 'running' : 'warn';
      if (d.bot.mode === 'live') text = 'Running with real money';
      else text = fresh ? 'Running' : 'Running, waiting for data';
    }
    node.dataset.state = state;
    setText('status-text', text);

    var tag = $('tagline');
    if (d.bot.mode === 'live') {
      tag.textContent = 'Real-money mode is selected in the advanced console.';
      tag.dataset.live = '1';
    } else {
      tag.textContent = 'Practice money only. No real orders are sent.';
      tag.dataset.live = '0';
    }
  }

  // An update installed while the app keeps running serves this newer page
  // against the older server until a restart: features the server doesn't
  // report yet say so instead of looking empty.
  function serverIsOlder(d) { return !d.settings || !d.strategies || !('entry_blocks' in d); }

  function renderBanner(d) {
    var box = $('banner');
    var action = $('banner-action');
    var text = '';
    var act = null;
    if (d.bot.mode === 'live') {
      if (d.bot.state === 'running') {
        text = 'The bot is trading real money. It was switched to real-money mode in the ' +
          'advanced console. Press Stop to halt it.';
      } else {
        text = 'Real-money mode is selected in the advanced console, so this screen ' +
          'won’t start the bot.';
        act = { label: 'Switch back to paper', run: switchToPaper };
      }
    } else if (d.selection && !d.selection.supported) {
      text = 'The advanced console has ' + String(d.selection.asset).toUpperCase() + ' ' +
        d.selection.timeframe + ' selected, which the paper bot doesn’t trade. ' +
        'Pick 5 min or 15 min below.';
    } else if (serverIsOlder(d)) {
      text = UPDATE_PENDING;
    } else if (d.entry_blocks && d.entry_blocks.last_hour >= 3 && d.entry_blocks.reason) {
      text = 'Trades are being blocked. In the last hour the bot tried to trade ' +
        d.entry_blocks.last_hour + ' times, but its safety rules refused: ' + d.entry_blocks.reason;
    }
    box.hidden = !text;
    setText('banner-text', text);
    action.hidden = !act;
    if (act) {
      action.textContent = act.label;
      action.onclick = act.run;
      action.disabled = S.busy;
    }
  }

  function renderControls(d) {
    var running = d.bot.state === 'running';
    var start = $('btn-start');
    var stop = $('btn-stop');
    start.hidden = running;
    stop.hidden = !running;
    var unsupported = !!(d.selection && !d.selection.supported);
    start.disabled = S.busy || d.bot.mode !== 'paper' || unsupported;
    start.title = d.bot.mode !== 'paper' ? 'Switch back to paper mode first' : '';
    stop.disabled = S.busy;
    if (!running && S.stopArmed) disarmStop();

    var tf = d.selection ? d.selection.timeframe : '5m';
    var isBtc = !d.selection || d.selection.asset === 'btc';
    var locked = (d.any_open || 0) > 0;
    document.querySelectorAll('.segmented button').forEach(function (btn) {
      btn.setAttribute('aria-pressed', String(isBtc && btn.dataset.tf === tf));
      btn.disabled = S.busy || locked;
      btn.title = locked ? 'Finish the open trade first' : '';
    });
  }

  // ---------------------------------------------------------------- live window

  function currentWindow(d) {
    var m = d.market;
    var t = nowTs();
    if (d.bot.state === 'running' && m && m.fresh && m.start_ts && m.end_ts && t < m.end_ts) {
      return { start: m.start_ts, end: m.end_ts, tf: m.timeframe, live: true };
    }
    var tf = d.selection && (d.selection.timeframe === '15m') ? '15m' : '5m';
    var sec = windowSeconds(tf);
    var start = Math.floor(t / sec) * sec;
    return { start: start, end: start + sec, tf: tf, live: false };
  }

  function renderMarket(d) {
    var win = currentWindow(d);
    S.window = win;
    setText('window-title', 'Bitcoin up or down, ' + clock(win.start) + ' to ' + clock(win.end));
    setText('track-start', clock(win.start));
    setText('track-end', clock(win.end));

    var m = d.market;
    var same = m && m.start_ts === win.start;
    var note = '';
    var stale = !win.live;
    if (!same) {
      note = d.bot.state === 'running'
        ? 'Waiting for prices for this window.'
        : 'Start the bot to see live prices for this window.';
    } else if (stale && m.tick_ts) {
      note = 'Last checked at ' + clock(m.tick_ts) + '.';
    }

    var now = $('btc-now');
    var beat = $('btc-beat');
    var diff = $('btc-diff');
    now.textContent = same && m.btc_price != null ? money(m.btc_price) : '—';
    beat.textContent = same && m.price_to_beat != null ? money(m.price_to_beat) : '—';
    if (same && m.difference != null) {
      var dv = m.difference;
      diff.textContent = dv === 0 ? 'Even'
        : (dv > 0 ? '▲ ' : '▼ ') + money(Math.abs(dv)) + (dv > 0 ? ' above' : ' below');
      setDirection(diff, dv);
    } else {
      diff.textContent = '—';
      setDirection(diff, null);
    }
    setText('up-cost', same ? cents(m.up_cost) : '—');
    setText('down-cost', same ? cents(m.down_cost) : '—');
    setText('chance-up', same && m.chance_up != null ? percent(m.chance_up) : '—');
    var rawNode = $('chance-raw');
    var showRaw = same && m.chance_up != null && m.chance_up_raw != null &&
      Math.abs(m.chance_up_raw - m.chance_up) >= 0.005;
    rawNode.hidden = !showRaw;
    rawNode.textContent = showRaw ? 'learned; raw math ' + percent(m.chance_up_raw) : '';

    var readouts = $('readouts');
    readouts.dataset.stale = stale ? '1' : '0';
    var noteEl = $('readouts-note');
    noteEl.hidden = !note;
    noteEl.textContent = note;

    var rules = d.rules || {};
    var duration = win.end - win.start;
    var cutoff = Math.min(1, Math.max(0, (rules.min_seconds_left || 0) / duration));
    $('track-cutoff').style.width = (cutoff * 100).toFixed(2) + '%';
  }

  function renderVerdict(d) {
    var v = d.decision || {};
    var box = $('verdict');
    box.dataset.tone = v.tone || 'idle';
    setText('verdict-head', v.headline || '');
    setText('verdict-detail', v.detail || '');
  }

  function tickClock() {
    var d = S.data;
    if (!d) return;
    var win = S.window;
    var t = nowTs();
    if (!win || t >= win.end + 1) {
      renderMarket(d);
      win = S.window;
    }
    var left = win.end - t;
    var rules = d.rules || {};
    var inCutoff = left <= (rules.min_seconds_left || 0);
    setText('countdown', mmss(left));
    setText('countdown-label', left <= 0 ? 'window closing'
      : inCutoff ? 'left. Too late for new trades in this window.' : 'left in this window');
    var frac = Math.min(1, Math.max(0, (t - win.start) / (win.end - win.start)));
    $('track-fill').style.width = (frac * 100).toFixed(2) + '%';

    var m = d.market;
    if (m && m.tick_ts) {
      setText('updated', 'Last market check ' + ago(t - m.tick_ts));
    } else {
      setText('updated', 'No market checks yet');
    }
  }

  // ---------------------------------------------------------------- money

  function renderWallet(d) {
    var w = d.wallet;
    var s = d.stats || {};
    var delta = $('balance-delta');
    if (!w) {
      setText('balance', '—');
      delta.textContent = 'The practice wallet is created the first time the app starts.';
      setDirection(delta, null);
      setText('balance-stats', '');
      setText('balance-fees', '');
      return;
    }
    setText('balance', money(w.equity_usd));
    var p = w.pnl_usd;
    var pc = Math.round(p * 100);
    if (pc === 0) {
      delta.textContent = 'No change yet';
    } else {
      delta.textContent = (pc > 0 ? '▲ ' : '▼ ') + signedMoney(p) +
        (w.pnl_pct != null ? ' (' + signedPercent(w.pnl_pct) + ')' : '');
    }
    setDirection(delta, pc);

    var sw = w.rescale;
    var parts = sw
      ? ['Started with ' + usd0.format(sw.first_start_usd) + ' on ' + shortDate(w.since_ts) +
        ', switched to ' + usd0.format(sw.to_usd) + ' on ' + shortDate(sw.at_ts) + '. Results from' +
        ' before the switch count ' + times(w.initial_usd / sw.first_start_usd) +
        ', so the percentage gain or loss carried over.']
      : ['Started with ' + money(w.initial_usd) + ' on ' + shortDate(w.since_ts) + '.'];
    if (s.trades) {
      parts.push(s.trades + (s.trades === 1 ? ' trade: ' : ' trades: ') + s.wins + ' won, ' +
        s.losses + ' lost.');
    } else {
      parts.push('No finished trades yet.');
    }
    var open = openTrades(d);
    if (open.length > 1) {
      parts.push('Includes ' + open.length + ' open trades at ' + money(w.open_value_usd) + '.');
    } else if (open.length) {
      parts.push('Includes the open trade at ' + money(w.open_value_usd) + '.');
    }
    setText('balance-stats', parts.join(' '));

    // Fees are part of every result now (the wallet pays them). Older servers
    // don't report fees_paid_usd, and their totals are before fees: say nothing.
    if (w.fees_paid_usd > 0) {
      setText('balance-fees', 'Every result here is after trading fees, the same ones real ' +
        'Polymarket trades pay: ' + money(w.fees_paid_usd) + ' so far' +
        (s.trades && s.gross_pnl_usd != null
          ? ' (' + signedMoney(s.gross_pnl_usd) + ' before fees on finished trades).' : '.'));
    } else {
      setText('balance-fees', '');
    }
  }

  // Every open trade, newest first (older servers only send the newest one).
  function openTrades(d) {
    if (Array.isArray(d.positions)) return d.positions;
    return d.position ? [d.position] : [];
  }

  function tradeLimit(d) {
    var r = d.rules || {};
    return d.bot && d.bot.mode === 'paper' && r.max_open > 1 ? r.max_open : 1;
  }

  // Big buy panel: the open trade in large type at the top of the page. A buy
  // that happens while the page is open (or within the last 90 s before it
  // loaded) gets a one-time highlight and puts the trade in the tab title.
  function renderBuy(d) {
    var box = $('buy');
    var p = d.position;
    if (!p) {
      box.hidden = true;
      box.classList.remove('is-new');
      document.title = BASE_TITLE;
      S.buyId = null;
      return;
    }
    var up = p.side === 'Up';
    if (p.position_id !== S.buyId) {
      var nowTs = Date.now() / 1000 + S.offset;
      var fresh = S.buyId !== undefined || (p.opened_ts != null && nowTs - p.opened_ts < 90);
      box.classList.remove('is-new');
      if (fresh) {
        void box.offsetWidth;  // restart the highlight animation
        box.classList.add('is-new');
      }
      S.buyId = p.position_id;
    }
    box.hidden = false;
    box.dataset.side = up ? 'up' : 'down';
    setText('buy-head', (up ? '▲ ' : '▼ ') + 'Bought ' + p.side);
    var m = d.market || {};
    var beat = m.window_slug === p.window_slug && m.price_to_beat != null
      ? ' ' + money(m.price_to_beat) : ' the price to beat';
    setText('buy-detail', shares(p.shares) + ' at ' + cents(p.entry_price) + ' for ' +
      money(p.cost_usd) + feeText(p.fee_usd) + '. Pays ' + money(p.shares) +
      ' if Bitcoin finishes ' + (up ? 'at or above' : 'below') + beat +
      (p.end_ts ? ' at ' + clock(p.end_ts) : '') + '.');
    var open = openTrades(d);
    setText('buy-meta', (p.opened_ts ? 'Bought at ' + clock(p.opened_ts) + '. ' : '') +
      'Worth ' + money(p.value_usd) + ' now (' + signedMoney(p.unrealized_usd) +
      (p.fee_usd > 0 ? ' after the fee' : '') + ').' +
      (open.length > 1 ? ' The newest of ' + open.length + ' open trades.' : ''));
    document.title = (up ? '▲' : '▼') + ' Bought ' + p.side + ' – ' + BASE_TITLE;
  }

  // " + 17¢ fee" / " + $1.20 fee", or nothing for a fee-free (older) row.
  function feeText(fee) {
    if (!(fee > 0)) return '';
    return ' + ' + (fee < 1 ? cents(fee) : money(fee)) + ' fee';
  }

  function renderPosition(d) {
    var box = $('position');
    box.replaceChildren();
    var open = openTrades(d);
    var limit = tradeLimit(d);
    setText('position-title', open.length > 1 ? 'Open trades (' + open.length + ')' : 'Open trade');
    if (!open.length) {
      box.appendChild(el('p', 'muted', limit > 1
        ? 'No open trade. The bot holds up to ' + limit + ' at once.'
        : 'No open trade. The bot holds at most one at a time.'));
      return;
    }
    box.classList.toggle('positions-many', open.length > 1);
    open.forEach(function (p) {
      var item = el('div', 'position-item');
      var side = el('p', 'position-side ' + (p.side === 'Up' ? 'is-up' : 'is-down'),
        (p.side === 'Up' ? '▲ ' : '▼ ') + p.side);
      item.appendChild(side);

      var lines = el('div', 'position-lines');
      function line(before, strong, after) {
        var row = el('p');
        row.appendChild(document.createTextNode(before));
        row.appendChild(el('strong', null, strong));
        if (after) row.appendChild(document.createTextNode(after));
        lines.appendChild(row);
      }
      line('Bought ', shares(p.shares), ' at ' + cents(p.entry_price) + ' for ' +
        money(p.cost_usd) + feeText(p.fee_usd) +
        (p.opened_ts && open.length > 1 ? ' at ' + clock(p.opened_ts) : '') + '.');
      line('Worth ', money(p.value_usd), ' now (' + signedMoney(p.unrealized_usd) +
        (p.fee_usd > 0 ? ' after the fee' : '') + ')' +
        (p.marked_live ? '.' : ', valued at cost until a new price arrives.'));
      var pays = p.side === 'Up' ? 'at or above' : 'below';
      var when = p.end_ts ? ' at ' + clock(p.end_ts) : '';
      line('Pays ', money(p.shares), ' if Bitcoin finishes ' + pays + ' the price to beat' + when + '.');
      item.appendChild(lines);
      box.appendChild(item);
    });
  }

  // ---------------------------------------------------------------- chart

  function niceStep(span, count) {
    var raw = span / Math.max(1, count);
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag;
    var step = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
    return step * mag;
  }

  function renderChart(d) {
    var box = $('chart');
    var pts = d.curve || [];
    var width = Math.round(box.clientWidth || 640);
    var last = pts.length ? pts[pts.length - 1] : null;
    var key = [pts.length, last && last.t, last && last.v, width].join('|');
    if (key === S.chartKey) return;
    S.chartKey = key;
    box.replaceChildren();
    S.chart = null;

    var anyTrades = (d.stats && d.stats.trades) || d.position;
    if (pts.length < 2 || !anyTrades) {
      box.appendChild(el('div', 'chart-empty',
        'Your balance appears here after the first trade.'));
      return;
    }

    var W = Math.max(300, width);
    var H = 236;
    var M = { l: 58, r: 78, t: 14, b: 30 };
    var iw = W - M.l - M.r;
    var ih = H - M.t - M.b;
    var initial = d.wallet ? d.wallet.initial_usd : pts[0].v;
    var vals = pts.map(function (p) { return p.v; });
    var lo = Math.min.apply(null, vals.concat([initial]));
    var hi = Math.max.apply(null, vals.concat([initial]));
    if (hi - lo < 1) { hi += 0.5; lo -= 0.5; }
    var step = niceStep(hi - lo, 4);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    var t0 = pts[0].t;
    var t1 = Math.max(pts[pts.length - 1].t, t0 + 1);
    function x(t) { return M.l + (t - t0) / (t1 - t0) * iw; }
    function y(v) { return M.t + (hi - v) / (hi - lo) * ih; }
    var fmtTick = step >= 1 ? function (v) { return usd0.format(v); } : money;

    var root = svg('svg', {
      viewBox: '0 0 ' + W + ' ' + H, width: W, height: H, tabindex: '0',
      role: 'img',
      'aria-label': 'Balance over time. Started at ' + money(initial) + ' and is ' +
        money(last.v) + ' after the latest finished trade. Use the arrow keys to read each point.',
    });

    var grid = svg('g', { class: 'grid' });
    var axis = svg('g', { class: 'axis' });
    for (var v = lo; v <= hi + step / 2; v += step) {
      var yy = Math.round(y(v)) + 0.5;
      grid.appendChild(svg('line', { x1: M.l, x2: W - M.r, y1: yy, y2: yy }));
      var label = svg('text', { x: M.l - 10, y: yy + 4, 'text-anchor': 'end' });
      label.textContent = fmtTick(v);
      axis.appendChild(label);
    }
    root.appendChild(grid);

    // Starting balance reference line.
    var ref = svg('g', { class: 'ref' });
    var ry = Math.round(y(initial)) + 0.5;
    ref.appendChild(svg('line', { x1: M.l, x2: W - M.r, y1: ry, y2: ry }));
    var refLabel = svg('text', { class: 'ref-label', x: M.l + 6, y: ry - 6 });
    refLabel.textContent = 'Starting balance';
    ref.appendChild(refLabel);
    root.appendChild(ref);

    // Step path: the balance only changes when a trade finishes.
    var d0 = 'M' + x(pts[0].t).toFixed(1) + ',' + y(pts[0].v).toFixed(1);
    for (var i = 1; i < pts.length; i++) {
      d0 += 'H' + x(pts[i].t).toFixed(1) + 'V' + y(pts[i].v).toFixed(1);
    }
    var bottom = (M.t + ih).toFixed(1);
    var area = d0 + 'V' + bottom + 'H' + x(pts[0].t).toFixed(1) + 'Z';
    root.appendChild(svg('path', { class: 'area', d: area }));
    root.appendChild(svg('path', { class: 'line', d: d0 }));

    var ex = x(last.t);
    var ey = y(last.v);
    root.appendChild(svg('circle', { class: 'end-dot', cx: ex, cy: ey, r: 4.5 }));
    var endLabel = svg('text', { class: 'end-label', x: ex + 10, y: ey + 4 });
    endLabel.textContent = money(last.v);
    root.appendChild(endLabel);

    // Time axis: first and last point.
    var spanDays = (t1 - t0) / 86400;
    function tLabel(t) { return spanDays < 1 ? clock(t) : shortDate(t); }
    var xa = svg('text', { x: M.l, y: H - 8, 'text-anchor': 'start' });
    xa.textContent = tLabel(t0);
    var xb = svg('text', { x: W - M.r, y: H - 8, 'text-anchor': 'end' });
    xb.textContent = tLabel(t1);
    axis.appendChild(xa);
    axis.appendChild(xb);
    root.appendChild(axis);

    // Hover / keyboard layer.
    var cross = svg('g', { class: 'cross', visibility: 'hidden' });
    var cl = svg('line', { y1: M.t, y2: M.t + ih });
    var cd = svg('circle', { r: 4.5 });
    cross.appendChild(cl);
    cross.appendChild(cd);
    root.appendChild(cross);
    var hit = svg('rect', { x: M.l, y: 0, width: iw, height: H, fill: 'transparent' });
    root.appendChild(hit);

    var tip = el('div', 'chart-tip');
    tip.hidden = true;
    box.appendChild(root);
    box.appendChild(tip);

    function show(idx) {
      idx = Math.max(0, Math.min(pts.length - 1, idx));
      S.chart.index = idx;
      var p = pts[idx];
      var px = x(p.t);
      var py = y(p.v);
      cl.setAttribute('x1', px); cl.setAttribute('x2', px);
      cd.setAttribute('cx', px); cd.setAttribute('cy', py);
      cross.setAttribute('visibility', 'visible');
      var when = (isToday(p.t) ? 'today' : shortDate(p.t)) + ' at ' + clock(p.t);
      var nOpen = openTrades(d).length;
      var what = idx === 0 ? 'Starting balance, ' + shortDate(p.t)
        : p.now ? (nOpen > 1 ? 'Now, including the ' + nOpen + ' open trades'
          : nOpen ? 'Now, including the open trade' : 'Now')
          : 'After the trade that finished ' + when;
      tip.replaceChildren(el('strong', null, money(p.v)), el('span', null, what));
      tip.hidden = false;
      var scale = box.clientWidth / W;
      var left = px * scale + 12;
      if (left + tip.offsetWidth > box.clientWidth) left = px * scale - tip.offsetWidth - 12;
      tip.style.left = Math.max(0, left) + 'px';
      tip.style.top = Math.max(0, py * scale - 24) + 'px';
    }
    function hide() {
      cross.setAttribute('visibility', 'hidden');
      tip.hidden = true;
    }
    function nearest(clientX) {
      var rect = root.getBoundingClientRect();
      var sx = (clientX - rect.left) / rect.width * W;
      var t = t0 + (sx - M.l) / iw * (t1 - t0);
      var idx = 0;
      for (var k = 0; k < pts.length; k++) { if (pts[k].t <= t) idx = k; }
      return idx;
    }
    S.chart = { index: pts.length - 1 };
    hit.addEventListener('pointermove', function (e) { show(nearest(e.clientX)); });
    hit.addEventListener('pointerleave', hide);
    root.addEventListener('focus', function () { show(S.chart.index); });
    root.addEventListener('blur', hide);
    root.addEventListener('keydown', function (e) {
      var idx = S.chart.index;
      if (e.key === 'ArrowLeft') idx -= 1;
      else if (e.key === 'ArrowRight') idx += 1;
      else if (e.key === 'Home') idx = 0;
      else if (e.key === 'End') idx = pts.length - 1;
      else if (e.key === 'Escape') { hide(); return; }
      else return;
      e.preventDefault();
      show(idx);
    });
  }

  // ---------------------------------------------------------------- trades

  var OUTCOME = { won: 'Won', lost: 'Lost', sold: 'Sold early', open: 'Open' };

  function renderTrades(d) {
    var rows = d.trades || [];
    var table = $('trades');
    var body = $('trades-body');
    $('trades-empty').hidden = rows.length > 0;
    table.hidden = rows.length === 0;
    var known = S.knownTrades;
    var next = new Set();
    var sw = d.wallet && d.wallet.rescale;
    var marked = false;
    body.replaceChildren();
    rows.forEach(function (r) {
      if (sw && !marked && r.scale > 1.000001) {
        // Trades below this row were made on the old balance: show them as they
        // happened and say how the totals count them.
        marked = true;
        var mark = el('tr', 'trades-switch');
        var cell = el('td', null, 'Practice balance switched from ' + usd0.format(sw.from_usd) +
          ' to ' + usd0.format(sw.to_usd) + ' ' + (isToday(sw.at_ts) ? 'today' : 'on ' + shortDate(sw.at_ts)) +
          ' at ' + clock(sw.at_ts) + '. The trades below are' +
          ' shown as they happened; the balance and totals count them ' + times(r.scale) + '.');
        cell.colSpan = 5;
        mark.appendChild(cell);
        body.appendChild(mark);
      }
      var key = r.position_id + ':' + r.result;
      next.add(key);
      var tr = el('tr');
      if (known && !known.has(key)) tr.className = 'is-new';

      var win = el('td', 'window', r.start_ts && r.end_ts
        ? dayTime(r.start_ts) + ' to ' + clock(r.end_ts)
        : dayTime(r.opened_ts));
      win.appendChild(el('span', 'sub', tfLabel(r.timeframe) + ' window'));
      tr.appendChild(win);

      var bet = el('td', null);
      bet.appendChild(el('span', r.side === 'Up' ? 'is-up' : 'is-down',
        (r.side === 'Up' ? '▲ ' : '▼ ') + r.side));
      bet.appendChild(el('span', 'sub', shares(r.shares) + ' at ' + cents(r.entry_price)));
      tr.appendChild(bet);

      tr.appendChild(el('td', 'num col-paid', money(r.cost_usd)));

      var outcome = el('td', null, OUTCOME[r.result] || r.result);
      if (r.payout_usd != null && r.result !== 'lost') {
        outcome.appendChild(el('span', 'sub', 'Got back ' + money(r.payout_usd)));
      }
      tr.appendChild(outcome);

      var profit = el('td', 'num', r.pnl_usd == null ? '—' : signedMoney(r.pnl_usd));
      setDirection(profit, r.pnl_usd == null ? null : Math.round(r.pnl_usd * 100));
      if (r.fee_usd > 0) {
        profit.appendChild(el('span', 'sub', r.pnl_usd == null
          ? 'paid a ' + money(r.fee_usd) + ' fee' : 'after a ' + money(r.fee_usd) + ' fee'));
      }
      tr.appendChild(profit);

      body.appendChild(tr);
    });
    S.knownTrades = next;
  }

  // ---------------------------------------------------------------- explainers

  function renderRules(d) {
    var r = d.rules || {};
    var key = JSON.stringify(r);
    if (key === S.rulesKey) return;
    S.rulesKey = key;
    var box = $('rules');
    box.replaceChildren();
    function para(parts) {
      var p = el('p');
      parts.forEach(function (part) {
        if (typeof part === 'string') p.appendChild(document.createTextNode(part));
        else p.appendChild(el('strong', null, part.b));
      });
      box.appendChild(p);
    }
    para(['Every ', { b: plain2.format(r.tick_seconds || 5) + ' seconds' },
      ' the bot reads Bitcoin’s price from Chainlink, the feed Polymarket uses to ' +
      'settle these markets, and estimates the chance that Bitcoin finishes the window ' +
      'at or above the price to beat.']);
    para(['It buys Up or Down only when that estimate beats the asking price by at least ',
      { b: cents(r.edge_min) }, ' and by no more than ', { b: cents(r.edge_max) },
      ' (a bigger gap usually means its data is behind the market), for shares priced from ',
      { b: cents(r.price_min) }, ' to ', { b: cents(r.price_max) }, ', with more than ',
      { b: (r.min_seconds_left || 0) + ' seconds' }, ' left in the window.']);
    var many = r.max_open > 1;
    if (r.trade_shares) {
      para(['It buys ', { b: plain2.format(r.trade_shares) + ' shares' }, ' a trade, which costs ' +
        money(r.trade_shares * r.price_min) + ' to ' + money(r.trade_shares * r.price_max) +
        ' across its price range. ' + (many ? '' : 'It makes at most one trade per window and ' +
        'holds until the window settles. ') + 'A winning share pays $1. A losing share pays nothing.']);
    } else {
      para(['It ' + (many ? '' : 'makes at most one trade per window, ') + 'spends ',
        { b: money(r.trade_min_usd) + ' to ' + money(r.trade_max_usd) },
        ' a trade (at least ' + plain2.format(r.min_shares || 5) + ' shares), and holds ' +
        'until the window settles. A winning share pays $1. A losing share pays nothing.']);
    }
    if (many) {
      para(['It holds up to ', { b: r.max_open + ' trades at once' }, ', so it can buy again in a ' +
        'window it already holds, at least ', { b: (r.spacing_seconds || 0) + ' seconds' },
        ' after its last trade there, and start the next window before the last one has settled. ' +
        'It holds each trade until its window settles. Trades in the same window win or lose ' +
        'together, so extra trades make the swings bigger, like a bigger trade size.']);
    }
    if (r.learning) {
      para(['The ', { b: 'learned chances' }, ' setting is on, so that estimate is corrected by what ' +
        'the bot has learned from its finished windows, whenever the correction has been making more ' +
        'money than its raw math (see “What it has learned”).']);
    }
    if (r.fee_aware) {
      para(['The ', { b: 'edge must beat trading fees' }, ' setting is on, so the bot subtracts ' +
        'the fee from its edge before comparing it with the minimum.']);
    }
    para(['Practice trades pay the same trading fee real Polymarket purchases do, about ',
      { b: cents((r.fee_rate || 0.07) * 0.25) + ' a share' }, ' on a 50¢ share and less near 0¢ or ' +
      '100¢, and every balance, profit and loss on this screen is after it.']);
    para(['A practice record can’t prove the strategy makes money. Treat it as a test ' +
      'over many trades, not a forecast.']);
  }

  function renderActivity(d) {
    var list = $('activity');
    list.replaceChildren();
    var rows = d.activity || [];
    if (!rows.length) {
      list.appendChild(el('li', null, 'Nothing yet.'));
      return;
    }
    rows.forEach(function (a) {
      var li = el('li');
      var time = el('time', null, a.t ? (isToday(a.t) ? clock(a.t) : shortDate(a.t)) : '');
      if (a.t) time.dateTime = new Date(a.t * 1000).toISOString();
      li.appendChild(time);
      li.appendChild(el('span', null, a.text || ''));
      list.appendChild(li);
    });
  }

  // ---------------------------------------------------------------- accuracy

  function renderAccuracy(d) {
    var a = d.accuracy || { trades: 0, buckets: [], summary: [] };
    setText('accuracy-summary', a.summary && a.summary.length ? a.summary.join(' ')
      : d.accuracy ? 'Accuracy appears after the first settled trades.'
      : 'Restart the app to see this (see the note at the top).');
    var buckets = a.buckets || [];
    $('accuracy-wrap').hidden = buckets.length === 0;
    var body = $('accuracy-body');
    body.replaceChildren();
    buckets.forEach(function (b) {
      var tr = el('tr');
      tr.appendChild(el('td', null, b.label));
      tr.appendChild(el('td', 'num', String(b.trades)));
      var won = el('td', 'num', percent(b.won));
      won.appendChild(el('span', 'sub', 'it said ' + percent(b.bot_said)));
      tr.appendChild(won);
      tr.appendChild(el('td', 'num', cents(b.price_paid)));
      var profit = el('td', 'num', signedMoney(b.profit_after_fees_usd));
      setDirection(profit, Math.round(b.profit_after_fees_usd * 100));
      tr.appendChild(profit);
      body.appendChild(tr);
    });
  }

  // ---------------------------------------------------------------- settings

  function round1(x) { return Math.round(x * 10) / 10; }
  function sameNumber(a, b) { return Math.abs(Number(a) - Number(b)) < 1e-9; }

  // Server values (knob units) -> form values (cents, seconds, shares).
  function settingsToForm(st) {
    var v = st.values;
    return {
      shares: v.trade_shares || Math.max(5, (st.bounds.trade_shares || [5])[0]),
      edgeMin: round1(v.paper_entry_edge_min * 100),
      edgeMax: round1(v.paper_entry_edge_max * 100),
      priceMin: Math.round(v.paper_min_entry_price * 100),
      priceMax: Math.round(v.paper_max_entry_price * 100),
      cutoff: v.paper_entry_min_remaining_seconds,
      tick: v.paper_tick_seconds,
      feeAware: !!v.paper_fee_aware,
      learning: !!v.paper_learning,
      maxOpen: v.paper_max_open_positions == null ? 1 : v.paper_max_open_positions,
      spacing: v.paper_entry_spacing_seconds == null ? 0 : v.paper_entry_spacing_seconds,
    };
  }

  function fillSettingsForm(f) {
    $('set-shares').value = f.shares;
    $('set-edge-min').value = f.edgeMin;
    $('set-edge-max').value = f.edgeMax;
    $('set-price-min').value = f.priceMin;
    $('set-price-max').value = f.priceMax;
    $('set-cutoff').value = f.cutoff;
    $('set-tick').value = f.tick;
    $('set-fee-aware').checked = f.feeAware;
    $('set-learning').checked = f.learning;
    $('set-max-open').value = f.maxOpen;
    $('set-spacing').value = f.spacing;
  }

  function readSettingsForm() {
    function num(id) {
      var raw = $(id).value.trim();
      return raw === '' ? NaN : Number(raw);
    }
    return {
      shares: num('set-shares'),
      edgeMin: num('set-edge-min'),
      edgeMax: num('set-edge-max'),
      priceMin: num('set-price-min'),
      priceMax: num('set-price-max'),
      cutoff: num('set-cutoff'),
      tick: num('set-tick'),
      feeAware: $('set-fee-aware').checked,
      learning: $('set-learning').checked,
      maxOpen: num('set-max-open'),
      spacing: num('set-spacing'),
    };
  }

  // Mirrors the server's checks so most mistakes are caught before saving.
  function settingsProblem(f, st) {
    var b = st.bounds;
    function within(x, lo, hi) { return isFinite(x) && x >= lo && x <= hi; }
    if (!within(f.shares, b.trade_shares[0], b.trade_shares[1])) {
      return 'Shares per trade must be between ' + b.trade_shares[0] + ' and ' +
        b.trade_shares[1] + '.';
    }
    if (!within(f.edgeMin / 100, b.paper_entry_edge_min[0], b.paper_entry_edge_min[1])) {
      return 'Minimum edge must be between ' + b.paper_entry_edge_min[0] * 100 + '¢ and ' +
        b.paper_entry_edge_min[1] * 100 + '¢.';
    }
    if (!within(f.edgeMax / 100, b.paper_entry_edge_max[0], b.paper_entry_edge_max[1])) {
      return 'The edge limit must be between ' + b.paper_entry_edge_max[0] * 100 + '¢ and ' +
        b.paper_entry_edge_max[1] * 100 + '¢.';
    }
    if (f.edgeMin >= f.edgeMax) return 'The minimum edge must be smaller than the edge limit.';
    if (!within(f.priceMin, 1, 99) || !within(f.priceMax, 1, 99)) {
      return 'Share prices must be between 1¢ and 99¢.';
    }
    if (f.priceMin >= f.priceMax) return 'The lowest share price must be below the highest.';
    if (!within(f.cutoff, 0, 600) || Math.round(f.cutoff) !== f.cutoff) {
      return 'The no-new-trades time must be a whole number of seconds from 0 to 600.';
    }
    if (!within(f.tick, 1, 60)) return 'Check the market every 1 to 60 seconds.';
    if (b.paper_max_open_positions) {
      var mo = b.paper_max_open_positions;
      if (!within(f.maxOpen, mo[0], mo[1]) || Math.round(f.maxOpen) !== f.maxOpen) {
        return 'Trades open at once must be a whole number from ' + mo[0] + ' to ' + mo[1] + '.';
      }
    }
    if (b.paper_entry_spacing_seconds) {
      var sp = b.paper_entry_spacing_seconds;
      if (!within(f.spacing, sp[0], sp[1]) || Math.round(f.spacing) !== f.spacing) {
        return 'The wait between trades must be a whole number of seconds from ' + sp[0] +
          ' to ' + sp[1] + '.';
      }
    }
    return '';
  }

  // Form values -> only the settings that differ from what's saved.
  function settingsChanges(f, st) {
    var v = st.values;
    var out = {};
    // Shares only: a dollar range would be blocked by the gate's $3 per-trade
    // cap, while N shares raise that cap to N (see gate.effective_max_trade_usd).
    if (!v.trade_shares || !sameNumber(v.trade_shares, f.shares)) out.trade_shares = f.shares;
    function frac(cents) { return Math.round(cents * 10) / 1000; }
    if (!sameNumber(v.paper_entry_edge_min, frac(f.edgeMin))) {
      out.paper_entry_edge_min = frac(f.edgeMin);
    }
    if (!sameNumber(v.paper_entry_edge_max, frac(f.edgeMax))) {
      out.paper_entry_edge_max = frac(f.edgeMax);
    }
    if (!sameNumber(v.paper_min_entry_price, f.priceMin / 100)) {
      out.paper_min_entry_price = f.priceMin / 100;
    }
    if (!sameNumber(v.paper_max_entry_price, f.priceMax / 100)) {
      out.paper_max_entry_price = f.priceMax / 100;
    }
    if (!sameNumber(v.paper_entry_min_remaining_seconds, f.cutoff)) {
      out.paper_entry_min_remaining_seconds = f.cutoff;
    }
    if (!sameNumber(v.paper_tick_seconds, f.tick)) out.paper_tick_seconds = f.tick;
    if (!!v.paper_fee_aware !== f.feeAware) out.paper_fee_aware = f.feeAware;
    if (!!v.paper_learning !== f.learning) out.paper_learning = f.learning;
    if ('paper_max_open_positions' in v && !sameNumber(v.paper_max_open_positions, f.maxOpen)) {
      out.paper_max_open_positions = f.maxOpen;
    }
    if ('paper_entry_spacing_seconds' in v && !sameNumber(v.paper_entry_spacing_seconds, f.spacing)) {
      out.paper_entry_spacing_seconds = f.spacing;
    }
    return out;
  }

  function fmt3(x) { return x == null ? '—' : x.toFixed(3); }

  function renderLearning(d) {
    var L = d.learning || {};
    var edgeMin = (d.rules && d.rules.edge_min) || 0.045;
    var text;
    if (serverIsOlder(d)) {
      text = 'Restart the app to turn on learning (see the note at the top).';
    } else if (L.enabled === false) {
      text = 'Learned chances are switched off in Settings, so the bot trades on its raw math. ' +
        'It still checks every hour how a correction would have done.';
    } else if (L.trade_windows != null) {
      var replay = 'Replayed over the last ' + L.trade_windows + ' finished windows with your settings, ' +
        'learned chances made ' + signedMoney(L.trade_learned_usd) + ' after fees over ' +
        L.trade_learned_n + ' trades, vs ' + signedMoney(L.trade_raw_usd) + ' over ' + L.trade_raw_n +
        ' trades with its raw math. Each window was judged with a correction learned only from ' +
        'earlier windows.';
      if (L.state === 'active') {
        var skipT = L.example_learned - L.example_market < edgeMin;
        text = 'The bot is trading on learned chances (last checked ' + dayTime(L.fitted_at) + '). ' +
          replay + ' For example, when its raw math says ' + percent(L.example_model) +
          ' and the share costs ' + cents(L.example_market) + ', it now says ' +
          percent(L.example_learned) + (skipT ? ', so it skips that trade.' : '.');
      } else {
        text = 'Not using learned chances right now (last checked ' + dayTime(L.fitted_at) + '). ' +
          replay + ' The bot trades on its raw math until that flips.';
      }
    } else if (L.state === 'active') {
      var skip = L.example_learned - L.example_market < edgeMin;
      text = 'The bot is trading on learned chances (last checked ' + dayTime(L.fitted_at) + ', from ' +
        L.windows + ' finished windows). On the newest ' + L.holdout_windows + ' windows, which it ' +
        'didn’t learn from, its corrected chances beat its raw math: error score ' +
        fmt3(L.brier_learned) + ' vs ' + fmt3(L.brier_model) + ' (the market’s own prices: ' +
        fmt3(L.brier_market) + '; lower is better). For example, when its raw math says ' +
        percent(L.example_model) + ' and the share costs ' + cents(L.example_market) + ', it now says ' +
        percent(L.example_learned) + (skip ? ', so it skips that trade.' : '.');
    } else if (L.state === 'inactive') {
      text = 'Not using learned chances right now: ' + L.reason + '. The bot trades on its raw math ' +
        'until a correction does better (last checked ' + dayTime(L.fitted_at) + ').';
    } else {
      text = 'The first check runs a few minutes after the bot starts. It needs 100 finished ' +
        'windows to learn from.';
    }
    setText('learning-status', text);
  }

  var VERDICT = {
    too_early: 'Too early to tell',
    luck: 'Within luck',
    ahead: 'Ahead by more than luck',
    behind: 'Behind by more than luck',
  };

  function renderStrategies(d) {
    var body = $('strategies-body');
    body.replaceChildren();
    if (serverIsOlder(d)) {
      var tr = el('tr');
      var td = el('td', null, 'Restart the app to see this comparison (see the note at the top).');
      td.colSpan = 6;
      tr.appendChild(td);
      body.appendChild(tr);
      return;
    }
    (d.strategies || []).forEach(function (s) {
      var tr = el('tr');
      if (s.id === 'main') tr.className = 'is-main';
      var name = el('td');
      name.appendChild(el('span', 'strategy-name', s.name));
      var since = s.since_ts ? '. Since ' + shortDate(s.since_ts) + '.'
        : (s.id === 'main' ? '. Counts from when learning starts.' : '. No trades yet.');
      name.appendChild(el('span', 'sub', s.about + since));
      tr.appendChild(name);
      tr.appendChild(el('td', 'num', String(s.trades)));
      var won = el('td', 'num', s.trades ? percent(s.won) : '—');
      tr.appendChild(won);
      tr.appendChild(el('td', 'num col-paid', s.trades ? cents(s.price_paid) : '—'));
      var profit = el('td', 'num', s.trades ? signedMoney(s.profit_after_fees_usd) : '—');
      if (s.trades) {
        setDirection(profit, Math.round(s.profit_after_fees_usd * 100));
        if (s.per_trade_range_usd != null) {
          // Keep "+$0.08 ± $0.41" on one line; only " a trade" may wrap.
          profit.appendChild(el('span', 'sub', signedMoney(s.per_trade_usd) + '\u00a0±\u00a0' +
            usd.format(Math.round(s.per_trade_range_usd * 100) / 100) + ' a trade'));
        }
      }
      tr.appendChild(profit);
      var verdict = el('td', 'strategy-verdict', VERDICT[s.verdict] || '');
      verdict.dataset.verdict = s.verdict;
      if (s.verdict === 'too_early' && s.trades) {
        verdict.appendChild(el('span', 'sub', s.trades + ' of 50 trades'));
      }
      tr.appendChild(verdict);
      body.appendChild(tr);
    });
  }

  function renderSettingsDefaults(st) {
    var dft = st.defaults;
    var text = {
      'edge-min': 'Default ' + cents(dft.paper_entry_edge_min) + '.',
      'edge-max': 'Default ' + cents(dft.paper_entry_edge_max) + '.',
      price: 'Default ' + cents(dft.paper_min_entry_price) + ' to ' +
        cents(dft.paper_max_entry_price) + '.',
      cutoff: 'Default ' + dft.paper_entry_min_remaining_seconds + ' seconds.',
      tick: 'Default ' + plain2.format(dft.paper_tick_seconds) + ' seconds.',
      fee: 'Off by default.',
      learning: 'On by default.',
      'max-open': dft.paper_max_open_positions == null ? ''
        : 'Default ' + dft.paper_max_open_positions + '.',
      spacing: dft.paper_entry_spacing_seconds == null ? ''
        : 'Default ' + dft.paper_entry_spacing_seconds + ' seconds.',
    };
    document.querySelectorAll('[data-default]').forEach(function (node) {
      node.textContent = text[node.dataset.default] || '';
    });
  }

  function renderSettings(d) {
    var st = d.settings;
    // No settings means the page is newer than the running server (an update
    // is installed but the app hasn't been restarted): nothing to show or save.
    $('settings-form').hidden = !st;
    $('settings-restart').hidden = !!st;
    if (!st) return;
    $('shares-note').hidden = !!st.values.trade_shares;
    // Older servers don't know these settings: don't offer them.
    $('set-learning').closest('.setting').hidden = !('paper_learning' in st.values);
    $('set-max-open').closest('.setting').hidden = !('paper_max_open_positions' in st.values);
    $('set-spacing').closest('.setting').hidden = !('paper_entry_spacing_seconds' in st.values);
    var key = JSON.stringify(st.values);
    if (!S.settingsDirty && key !== S.settingsKey) {
      fillSettingsForm(settingsToForm(st));
      S.settingsKey = key;
    }
    renderSettingsDefaults(st);
    var locked = d.bot.mode !== 'paper';
    $('settings-form').querySelectorAll('input, button').forEach(function (node) {
      node.disabled = locked || S.busy;
    });
    if (locked) {
      showSettingsError(SETTINGS_LOCKED);
    } else if ($('settings-error').textContent === SETTINGS_LOCKED) {
      showSettingsError('');
    }
  }

  var SETTINGS_LOCKED = 'Switch back to paper mode to change these settings.';
  var UPDATE_PENDING = 'An update is installed, but the old version of the app is still ' +
    'running. To finish: open Task Manager, end “Python”, then double-click ' +
    '“Start Polymarket Paper Trading” on your desktop.';

  function showSettingsError(message) {
    var box = $('settings-error');
    box.hidden = !message;
    box.textContent = message || '';
  }

  function applySavedSettings(st) {
    if (S.data && st) S.data.settings = st;
    S.settingsDirty = false;
    S.settingsKey = '';
    if (S.data) renderSettings(S.data);
  }

  function saveSettings(e) {
    e.preventDefault();
    var d = S.data;
    if (!d || !d.settings) return;
    var form = readSettingsForm();
    var problem = settingsProblem(form, d.settings);
    if (problem) {
      showSettingsError(problem);
      return;
    }
    var changes = settingsChanges(form, d.settings);
    if (!Object.keys(changes).length) {
      showSettingsError('');
      S.settingsDirty = false;
      toast('Nothing changed.');
      return;
    }
    showSettingsError('');
    withBusy(post('/api/simple/settings', { changes: changes }).then(function (j) {
      if (j.status === 'ok') {
        applySavedSettings(j.settings);
        toast('Settings saved. The bot uses them from its next check.');
      } else {
        showSettingsError(j.detail || 'Couldn’t save the settings.');
      }
    }));
  }

  function undoSettings() {
    showSettingsError('');
    S.settingsDirty = false;
    S.settingsKey = '';
    if (S.data) renderSettings(S.data);
  }

  function resetSettings() {
    showSettingsError('');
    withBusy(post('/api/simple/settings/reset').then(function (j) {
      if (j.status === 'ok') {
        applySavedSettings(j.settings);
        toast('Trading rules reset to their defaults.');
      } else {
        showSettingsError(j.detail || 'Couldn’t reset the settings.');
      }
    }));
  }

  function openSettings() {
    var panel = $('settings-panel');
    panel.open = true;
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    var first = panel.querySelector('input:not([disabled])');
    if (first) first.focus({ preventScroll: true });
  }

  // ---------------------------------------------------------------- render + poll

  function render() {
    var d = S.data;
    if (!d) return;
    renderStatus(d);
    renderBanner(d);
    renderControls(d);
    renderMarket(d);
    renderVerdict(d);
    renderWallet(d);
    renderBuy(d);
    renderPosition(d);
    renderChart(d);
    renderAccuracy(d);
    renderLearning(d);
    renderStrategies(d);
    renderTrades(d);
    renderSettings(d);
    renderRules(d);
    renderActivity(d);
    tickClock();
  }

  function apply(d) {
    S.data = d;
    S.failures = 0;
    if (typeof d.server_time === 'number') S.offset = d.server_time - Date.now() / 1000;
    document.body.dataset.offline = '0';
    render();
  }

  function offline(err) {
    S.failures += 1;
    var status = $('status');
    status.dataset.state = 'offline';
    if (!S.data) {
      setText('status-text', 'Can’t reach the bot');
      setText('verdict-head', 'The paper trader isn’t responding.');
      setText('verdict-detail', 'If you closed it, open it again with the ' +
        '“Start Polymarket Paper Trading” shortcut on your desktop. (' +
        (err && err.message ? err.message : 'no response') + ')');
      $('verdict').dataset.tone = 'warn';
      return;
    }
    if (S.failures >= 2) {
      document.body.dataset.offline = '1';
      setText('status-text', 'Reconnecting');
    }
  }

  function poll() {
    if (S.inflight) return;
    S.inflight = true;
    fetch('/api/simple', { cache: 'no-store' })
      .then(function (r) {
        return r.json().then(function (j) { return { ok: r.ok, body: j }; });
      })
      .then(function (res) {
        if (!res.ok || res.body.error) throw new Error(res.body.error || 'HTTP error');
        apply(res.body);
      })
      .catch(offline)
      .finally(function () { S.inflight = false; });
  }

  // ---------------------------------------------------------------- actions

  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : '{}',
    }).then(function (r) {
      return r.json().catch(function () { return {}; });
    });
  }

  function withBusy(promise) {
    S.busy = true;
    render();
    return promise
      .catch(function (err) { toast('Couldn’t reach the bot: ' + err.message, 'error'); })
      .finally(function () {
        S.busy = false;
        render();
        poll();
      });
  }

  function start() {
    withBusy(post('/api/start').then(function (j) {
      if (j.status === 'running') toast('Paper trading started.');
      else toast(j.detail || 'The bot didn’t start.', 'error');
    }));
  }

  function armStop() {
    S.stopArmed = true;
    var b = $('btn-stop');
    b.textContent = 'Sell open trade and stop';
    b.dataset.confirm = '1';
    clearTimeout(S.stopTimer);
    S.stopTimer = setTimeout(disarmStop, 5000);
  }

  function disarmStop() {
    S.stopArmed = false;
    clearTimeout(S.stopTimer);
    var b = $('btn-stop');
    b.textContent = 'Stop';
    b.dataset.confirm = '0';
  }

  function stop() {
    // Stopping sells an open practice trade at the current price, so a stop
    // with a trade open takes a second click (no browser dialogs).
    if (S.data && S.data.position && !S.stopArmed) {
      armStop();
      return;
    }
    disarmStop();
    withBusy(post('/api/stop').then(function (j) {
      if (j.status === 'stopped') toast('Stopped.');
      else toast(j.detail || 'Stop didn’t finish. Try again.', 'error');
    }));
  }

  function chooseTimeframe(tf) {
    var d = S.data;
    if (!d) return;
    if (d.selection && d.selection.asset === 'btc' && d.selection.timeframe === tf) return;
    withBusy(post('/api/runtime-config', { key: 'market', value: { asset: 'btc', timeframe: tf } })
      .then(function (j) {
        if (j.status === 'ok') {
          toast('Switched to ' + (tf === '15m' ? '15-minute' : '5-minute') + ' windows.');
        } else {
          toast(j.detail || 'Couldn’t switch windows.', 'error');
        }
      }));
  }

  function switchToPaper() {
    withBusy(post('/api/mode', { mode: 'paper' }).then(function (j) {
      if (j.mode === 'paper' && j.status !== 'error') toast('Switched back to paper mode.');
      else toast(j.detail || 'Couldn’t switch modes.', 'error');
    }));
  }

  // ---------------------------------------------------------------- boot

  document.addEventListener('DOMContentLoaded', function () {
    $('btn-start').addEventListener('click', start);
    $('btn-stop').addEventListener('click', stop);
    $('btn-settings').addEventListener('click', openSettings);
    var settingsForm = $('settings-form');
    settingsForm.addEventListener('input', function () {
      S.settingsDirty = true;
    });
    settingsForm.addEventListener('submit', saveSettings);
    $('settings-undo').addEventListener('click', undoSettings);
    $('settings-reset').addEventListener('click', resetSettings);
    document.querySelectorAll('.segmented button').forEach(function (btn) {
      btn.addEventListener('click', function () { chooseTimeframe(btn.dataset.tf); });
    });
    if (window.ResizeObserver) {
      // The chart's cache key includes its width, so this redraws only when
      // the width actually changes (never in a resize/redraw loop).
      new ResizeObserver(function () {
        if (S.data) renderChart(S.data);
      }).observe($('chart'));
    }
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) poll();
    });
    poll();
    setInterval(poll, POLL_MS);
    setInterval(tickClock, 1000);
  });
})();
