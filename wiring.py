"""Composition helper: publish a service's functions with their dependencies bound.

An application service function takes its `*Deps` object first. A composition root builds that object from
the real adapters and calls `bind(fn, deps)` to publish `fn` without the leading argument. This lives in the
composition ring because binding is wiring, not domain: the domain ring holds only the port protocols
(`*_port.py`).
"""
import functools
import inspect
from typing import Any, Callable


def bind(fn: Callable, deps: Any) -> Callable:
    """`fn(deps, *args, **kw)` as `bound(*args, **kw)`: the facade's public function. The signature and
    docstring are those of `fn` minus the leading `deps`, so introspection and help() still work."""
    @functools.wraps(fn)
    def bound(*args, **kwargs):
        return fn(deps, *args, **kwargs)

    params = list(inspect.signature(fn).parameters.values())[1:]
    setattr(bound, "__signature__", inspect.Signature(params, return_annotation=inspect.signature(fn).return_annotation))
    return bound
