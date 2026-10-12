"""Run the real pipeline in-process and record, per step, what a lane must keep: the printed lines, where it
commits, and whether it writes after its last commit.

`trace_build(root, ...)` builds the synthetic world (tests/pipeline_world.py) under `root`, runs `build.build`
through a `SpyFiles` (the real BuildFiles with one connection that logs its commits and writes) and returns a
`Trace`: the order the step files were loaded in, and a `StepTrace` per step. A lane proves its migrated step
equivalent by tracing the legacy pipeline here and its own bound step against the same world and comparing
`StepTrace.lines`, `.commits` and `.writes_after_last_commit`.
"""
import contextlib
import io
import os
import pathlib
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from pipeline_world import build_world  # noqa: E402

_WRITE = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


class _SpyCursor(sqlite3.Cursor):
    def execute(self, sql, *args):
        self.connection._note(sql)
        return super().execute(sql, *args)

    def executemany(self, sql, *args):
        self.connection._note(sql)
        return super().executemany(sql, *args)


class SpyConnection(sqlite3.Connection):
    """A connection that logs "commit" and the verb of every write statement, in order."""
    events: List[str]

    def _note(self, sql):
        m = _WRITE.match(sql) if isinstance(sql, str) else None
        if m:
            self.events.append(m.group(1).upper())

    def cursor(self, factory=_SpyCursor):
        return super().cursor(factory)

    def execute(self, sql, *args):
        self._note(sql)
        return super().execute(sql, *args)

    def executemany(self, sql, *args):
        self._note(sql)
        return super().executemany(sql, *args)

    def commit(self):
        self.events.append("commit")
        return super().commit()


@dataclass
class StepTrace:
    path: str
    lines: List[str] = field(default_factory=list)
    events: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def commits(self) -> int:
        return self.events.count("commit")

    @property
    def writes(self) -> int:
        return sum(1 for e in self.events if e != "commit")

    @property
    def writes_after_last_commit(self) -> bool:
        """True when the step issues a write that no commit of its own covers (the build's later commit, or the
        connection closing, decides what becomes of it)."""
        if "commit" not in self.events:
            return self.writes > 0
        last = len(self.events) - 1 - self.events[::-1].index("commit")
        return any(e != "commit" for e in self.events[last + 1:])


@dataclass
class Trace:
    steps: List[StepTrace] = field(default_factory=list)
    database: str = ""
    failure: Optional[Exception] = None

    def step(self, name: str) -> StepTrace:
        found = [s for s in self.steps if s.path.endswith("/" + name) or s.path == name]
        assert len(found) == 1, f"{name}: {[s.path for s in self.steps]}"
        return found[0]

    @property
    def order(self) -> List[str]:
        return [s.path for s in self.steps]


class SpyFiles:
    """local_buildfiles, except that the build database connection is a SpyConnection and every step module's
    `run` is wrapped to capture its output and the connection events it caused."""
    def __init__(self, trace: Trace):
        import local_buildfiles
        self._real = local_buildfiles
        self._trace = trace
        self._con: Optional[SpyConnection] = None

    def __getattr__(self, name):
        return getattr(self._real, name)

    def open_database(self, path):
        if self._con is None and self._trace.database == "":
            self._trace.database = path
            self._con = sqlite3.connect(path, factory=SpyConnection)
            self._con.events = []
            return self._con
        return self._real.open_database(path)

    def load_module(self, path):
        mod = self._real.load_module(path)
        real_run = mod.run
        trace, spy = self._trace, self

        def run(con, ctx, *args, **kwargs):
            rel = os.path.relpath(path, REPO).replace(os.sep, "/")
            st = StepTrace(rel)
            trace.steps.append(st)
            before = len(spy._con.events) if spy._con is not None else 0
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    return real_run(con, ctx, *args, **kwargs)
            except Exception as e:
                st.error = f"{type(e).__name__}: {e}"
                raise
            finally:
                st.lines = out.getvalue().splitlines()
                if spy._con is not None:
                    st.events = spy._con.events[before:]
        mod.run = run
        return mod


def scrub(lines, root) -> List[str]:
    """Replace the world's temp root so a recorded line does not depend on where the test ran."""
    out = []
    for ln in lines:
        for variant in {str(root), os.path.realpath(str(root)), pathlib.Path(root).as_posix(),
                        pathlib.Path(os.path.realpath(str(root))).as_posix()}:
            ln = ln.replace(variant, "<ROOT>")
        out.append(ln)
    return out


def trace_build(root, sources=("concerts", "ratings", "scrobbles", "measurements", "claims"), mutate=None) -> Trace:
    """Build the world under `root` (after `mutate(root)` edits its files), run build.build in this process and
    return the Trace. A step failure still returns the Trace (with `error` set on the failing step) and the
    exception as `Trace.failure`."""
    env = build_world(root, sources)
    if mutate is not None:
        mutate(str(root))
    saved = {k: os.environ.get(k) for k in ("KNOWLEDGE_PRIVATE_DIR", "KNOWLEDGE_HOME", "KNOWLEDGE_FROZEN_NOW", "PYTHONHASHSEED")}
    os.environ["KNOWLEDGE_PRIVATE_DIR"] = env["KNOWLEDGE_PRIVATE_DIR"]
    os.environ["KNOWLEDGE_HOME"] = env["KNOWLEDGE_HOME"]
    os.environ["KNOWLEDGE_FROZEN_NOW"] = env["KNOWLEDGE_FROZEN_NOW"]
    try:
        for name in ("build", "paths", "clock"):
            sys.modules.pop(name, None)
        import build
        import clock
        import paths
        trace = Trace()
        target = os.path.join(str(root), "out", "trace.db")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            build.build(target, paths.load(), clock.from_environment(), SpyFiles(trace))
        except build.BuildError as e:
            trace.failure = e
        for st in trace.steps:
            st.lines = scrub(st.lines, root)
        return trace
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def summarize(trace: Trace) -> Dict[str, dict]:
    """The comparable form of a trace (what the golden file stores)."""
    return {s.path: {"lines": s.lines, "commits": s.commits, "writes_after_last_commit": s.writes_after_last_commit,
                     "wrote": s.writes > 0}
            for s in trace.steps}
