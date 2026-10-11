"""The music step modules are thin: each composes the service and reports what it returned."""
import ast
import os


def test_every_music_step_reports_the_result_the_service_returned():
    """A step that called a service and dropped its StepResult would silently print nothing."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("concerts", "music_ratings", "scrobbles", "seed_artist_members"):
        tree = ast.parse(open(os.path.join(repo_root, "client", name + ".py"), encoding="utf-8").read())
        run = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "run")
        assigns = [n for n in ast.walk(run) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                   and ast.unparse(n.value.func).startswith("music_ingest.compose")] or \
                  [n for n in ast.walk(run) if isinstance(n, ast.Assign) and "music_ingest." in ast.unparse(n.value)]
        assert assigns, f"client/{name}.py: run() does not bind a service result"
        used = {n.attr for n in ast.walk(run) if isinstance(n, ast.Attribute)}
        assert "lines" in used, f"client/{name}.py: run() never reports result.lines"
