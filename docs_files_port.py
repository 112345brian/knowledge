"""Port for the files the documentation generator reads and writes: the docs, the two config files, and every module's source."""
from typing import Mapping, Protocol


class DocsFiles(Protocol):
    def read(self, path: str) -> str: ...        # repo-relative, '/'-separated; FileNotFoundError when absent
    def write(self, path: str, text: str) -> None: ...
    def module_sources(self) -> Mapping[str, str]: ...   # bare module name -> source text
