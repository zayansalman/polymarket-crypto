B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; cd "$B"
echo "swap before: $(sysctl -n vm.swapusage)"
run() { /usr/bin/time -l python3 bench.py "$@" 2> t.txt | grep RESULT | sed 's/RESULT //' > r.json
  fp=$(awk '/peak memory footprint/{printf "%.0f", $1/1048576}' t.txt); wall=$(awk '/ real /{print $1}' t.txt)
  python3 -c "import json;r=json.load(open('r.json'));print(f\"{r['device']:>4} n={r['n']:<4} chunk={r['chunk']:<4} footprint={$fp}MB steady={r['steady_call_s']}s first={r['first_call_s']}s load={r['load_s']}s p_up={r['p_up']} uniq={r['unique_paths']} p5/50/95={r['close_p5_p50_p95']}\")" 2>/dev/null || { echo "FAILED: $*"; tail -5 t.txt; }
  echo "$fp"; }
for n in 10 25 50; do fp=$(run --device cpu --n $n --chunk $n | tee /dev/stderr | tail -1); done 2>&1
if [ "${fp:-9999}" -lt 2500 ]; then run --device cpu --n 100 --chunk 100 | head -1; else echo "skip cpu n=100 unchunked (n=50 footprint ${fp}MB)"; fi
run --device cpu --n 100 --chunk 25 | head -1
for n in 1 25 100; do run --device mps --n $n --chunk $n | head -1; done
run --device mps --n 100 --chunk 25 | head -1
echo "swap after: $(sysctl -n vm.swapusage)"
