"""Import guard loaded only by the public tree test subprocess."""

import importlib.abc
import importlib.machinery
import json
import os
import sys


_allowed = set(json.loads(os.environ.get("CLEANRELEASE_ALLOWED_IMPORTS", "[]")))


class _DeclaredDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if "." in fullname or fullname in _allowed:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None:
            return None
        locations = [spec.origin or "", *(spec.submodule_search_locations or ())]
        if any("site-packages" in location.replace("\\", "/").split("/") or
               "dist-packages" in location.replace("\\", "/").split("/")
               for location in locations):
            raise ModuleNotFoundError(f"No module named '{fullname}' (blocked: undeclared dependency)")
        return None


sys.meta_path.insert(0, _DeclaredDependencies())
