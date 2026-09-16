B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182; wc -l < $B/scratchpad/kronos-test/backtest_results.jsonl; ps -axo etime,command | grep "[b]acktest.py --hours" | grep -v zsh | cut -c1-20; python3 -c "
import json; r=[json.loads(l) for l in open('$B/scratchpad/kronos-test/backtest_results.jsonl')]
import statistics as s; print('median s/hour', round(s.median(x['forecast_s'] for x in r),1))"
