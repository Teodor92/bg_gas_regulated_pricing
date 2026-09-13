"""Load the integration's parser without importing Home Assistant.

The scraper deliberately shares `parser.py` with the integration rather than
carrying its own copy: a second parser would drift from the first, and the
drift would only show up as wrong prices. But the package's `__init__.py`
imports Home Assistant, which has no business being a CI dependency of a
scraper. Loading the two leaf modules into a synthetic package gives us the
parser and its constants with `__init__.py` never executed.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType

PACKAGE = "bg_gas_regulated_pricing"
COMPONENT = Path(__file__).resolve().parent.parent / "custom_components" / PACKAGE


def load() -> tuple[ModuleType, ModuleType]:
    """Return the integration's (const, parser) modules."""
    alias = "_bg_gas_integration"
    if alias not in sys.modules:
        package = types.ModuleType(alias)
        package.__path__ = [str(COMPONENT)]
        sys.modules[alias] = package

    loaded: dict[str, ModuleType] = {}
    # const first: parser imports it by relative name.
    for name in ("const", "parser"):
        qualified = f"{alias}.{name}"
        if qualified not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                qualified, COMPONENT / f"{name}.py"
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"could not load {qualified}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[qualified] = module
            spec.loader.exec_module(module)
        loaded[name] = sys.modules[qualified]

    return loaded["const"], loaded["parser"]
