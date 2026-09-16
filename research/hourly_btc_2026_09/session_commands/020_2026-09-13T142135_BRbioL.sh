B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 "$B/backtest.py" --hours 2 --paths 10 --chunk 10 --out "$B/smoke.jsonl" 2>&1 | grep -v Warning | tail -5 && python3 -c "
import json
for l in open('$B/smoke.jsonl'):
    r=json.loads(l); print({k:(round(v,3) if isinstance(v,float) else v) for k,v in r.items()})"; rm -f "$B/smoke.jsonl"
