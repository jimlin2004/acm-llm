"""Auto-discover flow modules: every module in this package self-registers."""

import importlib
import pkgutil

for _mod in pkgutil.iter_modules(__path__):
    importlib.import_module(f"{__name__}.{_mod.name}")
