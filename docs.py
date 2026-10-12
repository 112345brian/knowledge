"""Composition root for checking and rebuilding the generated documentation regions."""
import docs_service
from docs_service import DocsDeps
from docs_store import RepoDocs
from wiring import bind_all

API = ("build", "check")


def compose(root=None):
    return bind_all(docs_service, DocsDeps(RepoDocs(root) if root else RepoDocs()), API)

