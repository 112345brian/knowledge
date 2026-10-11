"""Adapter for `DocsFiles`: reads and writes the repository's own docs and sources under one root directory."""
import os
from typing import Dict

PACKAGES = ("ingest", "client")   # sub-packages whose modules are listed by their bare name
HERE = os.path.dirname(os.path.abspath(__file__))


class RepoDocs:
    def __init__(self, root: str = HERE):
        self.root = os.path.abspath(root)

    def _full(self, path: str) -> str:
        return os.path.join(self.root, *path.split("/"))

    def read(self, path: str) -> str:
        with open(self._full(path), encoding="utf-8", newline="") as f:
            return f.read()

    def write(self, path: str, text: str) -> None:
        with open(self._full(path), "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def module_sources(self) -> Dict[str, str]:
        """{bare module name: source} for the top-level modules and those of the ingest and client packages. A name
        used twice is an error: the architecture config keys modules by their bare name."""
        found: Dict[str, str] = {}
        for directory in (self.root, *(os.path.join(self.root, p) for p in PACKAGES)):
            if not os.path.isdir(directory):
                continue
            for f in sorted(os.listdir(directory)):
                if not f.endswith(".py") or f == "__init__.py":
                    continue
                name = f[:-3]
                if name in found:
                    raise ValueError(f"module name {name!r} exists in more than one directory")
                with open(os.path.join(directory, f), encoding="utf-8") as fh:
                    found[name] = fh.read()
        return found
