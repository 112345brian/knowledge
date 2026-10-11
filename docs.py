"""Composition root for the documentation generator: binds `docs_service` to the repository file adapter."""
from docs_service import DocsDeps
import docs_service
import docs_store
from wiring import bind_all

API = ("build", "check")


def compose(root=None):
    """The generator wired to the files under `root` (default: this checkout)."""
    store = docs_store.RepoDocs(root) if root is not None else docs_store.RepoDocs()
    return bind_all(docs_service, DocsDeps(docs_files=store), API)
