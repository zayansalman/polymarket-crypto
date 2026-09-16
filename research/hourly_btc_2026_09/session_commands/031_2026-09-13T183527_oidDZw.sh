B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 -c "
import importlib
for m in ['lightgbm','sklearn','xgboost','qlib','statsmodels','scipy']:
    try: mod=importlib.import_module(m); print(m, getattr(mod,'__version__','?'))
    except Exception as e: print(m, 'MISSING')"; grep -rli "qlib" $B/Kronos-upstream --include="*.py" --include="*.md" | head -5; grep -rli "qlib\|quantlib" --include="*.md" . 2>/dev/null | grep -v "^./data" | head; head -2 $B/repo/data/BTCUSDT_1h_20251018_220012.csv; sysctl -n vm.swapusage
