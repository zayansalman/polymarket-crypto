"""Load joblib pickles with a strict allowlist so an untrusted file can't run code."""
import importlib, inspect, pickle
from joblib import numpy_pickle as npk

SAFE_EXACT = {
    ("builtins", n) for n in ["dict", "list", "set", "tuple", "slice", "frozenset", "float", "int", "str", "bytes", "bytearray", "complex", "bool"]
} | {
    ("collections", "OrderedDict"), ("copyreg", "_reconstructor"), ("_codecs", "encode"),
    ("numpy", "ndarray"), ("numpy", "dtype"),
    ("numpy.core.multiarray", "_reconstruct"), ("numpy._core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "scalar"), ("numpy._core.multiarray", "scalar"),
    ("numpy.random._pickle", "__randomstate_ctor"), ("numpy.random._pickle", "__bit_generator_ctor"),
    ("numpy.random._pickle", "__generator_ctor"),
    ("joblib.numpy_pickle", "NumpyArrayWrapper"),
}
seen = []

class Restricted(npk.NumpyUnpickler):
    def find_class(self, module, name):
        seen.append((module, name))
        if (module, name) in SAFE_EXACT:
            return super().find_class(module, name)
        if module.startswith("sklearn.") or module.startswith("numpy.") and name[:1].isupper():
            obj = getattr(importlib.import_module(module), name)
            if inspect.isclass(obj):
                return obj
        if module.startswith("numpy") and (name.startswith("dtype") or name[:5] in ("float","int32","int64","uint8","bool_")):
            obj = getattr(importlib.import_module(module), name)
            if inspect.isclass(obj):
                return obj
        raise pickle.UnpicklingError(f"blocked global {module}.{name}")

def load(path):
    with open(path, "rb") as fh:
        u = Restricted(path, fh, None)
        return u.load()
