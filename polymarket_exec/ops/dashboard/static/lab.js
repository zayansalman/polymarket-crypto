/* Strategy Lab: renders /api/lab. Read-only; never writes settings. */
(function () {
  'use strict';

  var MINUS = '−';
  var FALLBACK_BALANCE = 100;
  var usd = new Intl.NumberFormat(undefined, {
    style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
  });
  var VERDICT = {
    too_early: 'Too few trades to tell',
    luck: 'Within luck',
    ahead: 'Ahead by more than luck',
    behind: 'Behind by more than luck',
  };

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }
  function money(v) {
    if (v == null) return '—';
    var r = Math.round(v * 100) / 100;
    if (r === 0) return usd.format(0);
    return (r > 0 ? '+' : MINUS) + usd.format(Math.abs(r));
  }
  function plainMoney(v) { return v == null ? '—' : usd.format(Math.abs(v)); }
  function pct(x) { return x == null ? '—' : Math.round(x * 100) + '%'; }
  function cents(p) { return p == null ? '—' : (Math.round(p * 1000) / 10) + '¢'; }
  function date(ts) {
    return new Date(ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' });
  }
  // The practice balance the sizes are judged against (the real one when the
  // server sends it: $500 since 2026-11-18).
  function balance(d) { return d.balance_usd > 0 ? d.balance_usd : FALLBACK_BALANCE; }
  function wholeMoney(v) { return '$' + Math.round(v).toLocaleString(); }
  function dir(node, v) {
    node.classList.toggle('is-up', v != null && v > 0.005);
    node.classList.toggle('is-down', v != null && v < -0.005);
  }

  function head(table, cols) {
    var thead = el('thead');
    var tr = el('tr');
    cols.forEach(function (c) {
      var th = el('th', c.num ? 'num' : null, c.label);
      th.scope = 'col';
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    table.appendChild(thead);
  }

  function resultCells(tr, r) {
    tr.appendChild(el('td', 'num', String(r.trades)));
    tr.appendChild(el('td', 'num', r.trades ? pct(r.won) : '—'));
    var p = el('td', 'num', r.trades ? money(r.profit_usd) : '—');
    if (r.trades) {
      dir(p, r.profit_usd);
      if (r.range_usd != null) {
        p.appendChild(el('span', 'sub', money(r.per_trade_usd) + ' ± ' +
          plainMoney(r.range_usd) + ' a trade'));
      }
    }
    tr.appendChild(p);
    var v = el('td', 'strategy-verdict', VERDICT[r.verdict] || '');
    v.dataset.verdict = r.verdict;
    tr.appendChild(v);
  }

  var RESULT_COLS = [
    { label: 'Trades', num: true }, { label: 'Won', num: true },
    { label: 'Profit after fees', num: true }, { label: 'Verdict' },
  ];

  function renderExit(d) {
    var t = $('exit-table');
    t.replaceChildren();
    head(t, [{ label: 'Approach' }].concat(RESULT_COLS));
    var body = el('tbody');
    d.exit_styles.forEach(function (r) {
      var tr = el('tr');
      if (r.rules.exit_style === d.current.rules.exit_style) tr.className = 'is-main';
      var name = el('td');
      name.appendChild(el('span', 'strategy-name', r.label));
      var parts = [];
      var ex = r.exits || {};
      if (ex.TARGET) parts.push(ex.TARGET + ' hit the target');
      if (ex.STOP) parts.push(ex.STOP + ' hit the stop');
      if (ex.TIME) parts.push(ex.TIME + ' sold near the end');
      if (ex.BAND) parts.push(ex.BAND + ' sold when the edge faded');
      if (ex.SETTLE && r.rules.exit_style === 'scalp') parts.push(ex.SETTLE + ' held to the end');
      if (r.rules.exit_style === d.current.rules.exit_style) parts.unshift('Your setting');
      if (parts.length) name.appendChild(el('span', 'sub', parts.join(', ') + '.'));
      tr.appendChild(name);
      resultCells(tr, r);
      body.appendChild(tr);
    });
    t.appendChild(body);
  }

  function renderSizes(d) {
    var t = $('size-table');
    t.replaceChildren();
    head(t, [
      { label: 'Size' }, { label: 'Average cost', num: true }, { label: 'Profit after fees', num: true },
      { label: 'Worst losing streak', num: true }, { label: 'Orders cut short', num: true },
    ]);
    var body = el('tbody');
    d.sizes.forEach(function (r) {
      var tr = el('tr');
      if (r.rules.shares === d.current.rules.shares) tr.className = 'is-main';
      var name = el('td');
      name.appendChild(el('span', 'strategy-name', r.label));
      if (r.rules.shares === d.current.rules.shares) name.appendChild(el('span', 'sub', 'Your setting'));
      tr.appendChild(name);
      tr.appendChild(el('td', 'num', r.avg_cost_usd == null ? '—' : plainMoney(r.avg_cost_usd)));
      var p = el('td', 'num', r.trades ? money(r.profit_usd) : '—');
      dir(p, r.profit_usd);
      tr.appendChild(p);
      var dd = el('td', 'num', plainMoney(r.worst_drawdown_usd));
      dd.appendChild(el('span', 'sub', Math.round(Math.abs(r.worst_drawdown_usd) / balance(d) * 100) +
        '% of ' + wholeMoney(balance(d))));
      tr.appendChild(dd);
      tr.appendChild(el('td', 'num', r.capped_share == null ? '—' : pct(r.capped_share)));
      body.appendChild(tr);
    });
    t.appendChild(body);
  }

  function renderFactors(d) {
    var t = $('factor-table');
    t.replaceChildren();
    head(t, [{ label: 'Change' }].concat(RESULT_COLS));
    var body = el('tbody');
    var group = null;
    d.factors.forEach(function (r) {
      if (r.group !== group) {
        group = r.group;
        var gr = el('tr', 'lab-group');
        var gc = el('th', null, group);
        gc.colSpan = 5;
        gc.scope = 'colgroup';
        gr.appendChild(gc);
        body.appendChild(gr);
      }
      var tr = el('tr');
      if (r.is_current) tr.className = 'is-main';
      var name = el('td');
      name.appendChild(el('span', 'strategy-name', r.label));
      if (r.is_current) name.appendChild(el('span', 'sub', 'Your setting'));
      tr.appendChild(name);
      resultCells(tr, r);
      body.appendChild(tr);
    });
    t.appendChild(body);
  }

  function renderGrid(d) {
    var g = d.grid;
    $('grid-intro').textContent = 'The lab tried ' + g.tested + ' combinations of exit style, edge, ' +
      'price range, timing, fee check and learned chances. It picked the five that made the most on ' +
      'the older half of the history (' + g.tune_windows + ' windows), then tested them on the newer ' +
      'half (' + g.check_windows + ' windows), which played no part in picking them.';
    var t = $('grid-table');
    t.replaceChildren();
    head(t, [
      { label: 'Rules' }, { label: 'Older half', num: true }, { label: 'Newer half', num: true },
      { label: 'Verdict on the newer half' },
    ]);
    var body = el('tbody');
    function add(label, tune, check, cls) {
      var tr = el('tr', cls || null);
      tr.appendChild(el('td', null, label));
      var a = el('td', 'num', tune ? money(tune.profit_usd) : '—');
      if (tune) { dir(a, tune.profit_usd); a.appendChild(el('span', 'sub', tune.trades + ' trades')); }
      tr.appendChild(a);
      var b = el('td', 'num', money(check.profit_usd));
      dir(b, check.profit_usd);
      b.appendChild(el('span', 'sub', check.trades + ' trades'));
      tr.appendChild(b);
      var v = el('td', 'strategy-verdict', VERDICT[check.verdict] || '');
      v.dataset.verdict = check.verdict;
      tr.appendChild(v);
      body.appendChild(tr);
    }
    g.picks.forEach(function (p) { add(p.label, p.tune, p.check); });
    add('Your current settings', null, g.current_check, 'is-main');
    t.appendChild(body);
  }

  function answers(d) {
    var out = [];
    var settle = d.exit_styles[0];
    var scalp = d.exit_styles[1];
    if (scalp.trades && settle.trades) {
      var better = scalp.profit_usd > settle.profit_usd ? scalp : settle;
      var worse = better === scalp ? settle : scalp;
      var line = (better === settle ? 'Hold until the window settles.' : 'Scalping did better here.') +
        ' Holding made ' + money(settle.profit_usd) + ' over ' + settle.trades + ' trades. Scalping made ' +
        money(scalp.profit_usd) + ' over ' + scalp.trades + ' trades';
      line += worse.verdict === 'behind' ? ', which is behind by more than luck.' : '. That gap could be luck.';
      out.push(line);
    }
    var safe = null;
    d.sizes.forEach(function (r) {
      if (Math.abs(r.worst_drawdown_usd) <= balance(d) * 0.25) safe = r;
    });
    var cur = d.current;
    out.push('Size: bigger trades only magnify the result. ' + (safe ?
      'The largest size whose worst losing streak stayed under a quarter of the ' +
      wholeMoney(balance(d)) + ' balance was ' +
      safe.label + '.' : 'Even 5 shares had a losing streak over a quarter of the balance.') +
      ' Don’t size up until the rules make money after fees.');
    var standouts = d.factors.filter(function (r) {
      return !r.is_current && (r.verdict === 'ahead' || r.verdict === 'behind');
    });
    if (standouts.length) {
      out.push('Changes that stand out beyond luck: ' + standouts.map(function (r) {
        return r.group.toLowerCase() + ' ' + r.label + ' (' + money(r.profit_usd) + ')';
      }).join('; ') + '.');
    } else {
      out.push('None of the single changes did better or worse than your settings by more than luck.');
    }
    var g = d.grid;
    if (g.picks.length) {
      var tune = 0;
      var check = 0;
      g.picks.forEach(function (p) { tune += p.tune.profit_usd; check += p.check.profit_usd; });
      out.push('Picking rules from past results: the five best on the older half averaged ' +
        money(tune / g.picks.length) + ' there and ' + money(check / g.picks.length) +
        ' on the newer half. ' + (check > 0 && g.picks.every(function (p) { return p.check.verdict !== 'behind'; }) ?
        'They held up so far, but give it more data before trusting it.' :
        'What looked best in the past did not carry forward, so the bot shouldn’t copy it.'));
    }
    out.push('Your current settings: ' + money(cur.profit_usd) + ' over ' + cur.trades + ' trades, ' +
      (VERDICT[cur.verdict] || '').toLowerCase() + '.');
    return out;
  }

  function render(d) {
    if (d.error) {
      $('lab-scope').textContent = d.error;
      return;
    }
    if (!d.ready) {
      $('lab-scope').textContent = d.detail || 'Not enough history yet.';
      return;
    }
    $('lab-scope').textContent = 'Tested on ' + d.windows + ' finished windows from ' + date(d.first_ts) +
      ' to ' + date(d.last_ts) + ' (the oldest fifth only trains the learned chances). Figures are for ' +
      d.current.rules.shares + ' shares a trade unless a row says otherwise, after estimated fees.';
    var list = $('lab-answers');
    list.replaceChildren();
    answers(d).forEach(function (a) { list.appendChild(el('li', null, a)); });
    renderExit(d);
    renderSizes(d);
    renderFactors(d);
    renderGrid(d);
    $('lab-updated').textContent = 'Analyzed ' + new Date(d.computed_at * 1000).toLocaleTimeString([],
      { hour: 'numeric', minute: '2-digit' }) + ' in ' + d.seconds + ' s';
  }

  function load(refresh) {
    var btn = $('btn-rerun');
    btn.disabled = true;
    btn.textContent = 'Analyzing…';
    fetch('/api/lab' + (refresh ? '?refresh=true' : ''), { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(render)
      .catch(function (e) { $('lab-scope').textContent = 'Couldn’t reach the bot: ' + e.message; })
      .then(function () { btn.disabled = false; btn.textContent = 'Run again'; });
  }

  $('btn-rerun').addEventListener('click', function () { load(true); });
  load(false);
})();
