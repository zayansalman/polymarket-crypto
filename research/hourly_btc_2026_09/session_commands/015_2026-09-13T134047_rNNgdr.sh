BASE=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test
uv pip install --target "$BASE/pylibs" einops==0.8.1 2>&1 | tail -3
curl -s "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000" -o "$BASE/btc_1h_latest.json"
python3 -c "
import json, datetime as d
k = json.load(open('$BASE/btc_1h_latest.json'))
f = lambda ms: d.datetime.fromtimestamp(ms/1000, d.UTC).strftime('%Y-%m-%d %H:%M UTC')
print(len(k), 'candles', f(k[0][0]), '->', f(k[-1][0]), '| last closes at', f(k[-1][6]+1))
"
