"""Adapter for fresh identifiers: a random source_key for a new fact (randomness is infrastructure, so the
domain takes the key as an argument and the use case gets it from this port)."""
import uuid


def new_source_key():
    return "f-" + uuid.uuid4().hex[:12]
