python3 -c "
import importlib
for m in ['numpy','pandas','einops','huggingface_hub','safetensors','tqdm']:
    try:
        mod = importlib.import_module(m); print(m, getattr(mod,'__version__','?'))
    except Exception as e: print(m, 'MISSING')
"; sed -n 1,80p /private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test/Kronos-upstream/requirements.txt
