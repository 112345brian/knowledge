"""An in-memory Inputs port (JSON kept as text) for the music steps; the repository fake is the client's
`fake_client_repositories.FakeMusic`."""
import json


class FakeInputs:
    """`Inputs` over a dict of path -> text (JSON files are stored as text and parsed on read)."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.reads = []

    def exists(self, path):
        return path in self.files

    def read_text(self, path, encoding="utf-8"):
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def read_json(self, path):
        return json.loads(self.read_text(path))

    def list_files(self, directory, pattern):
        return []

    def open_database(self, path):
        raise AssertionError("the music steps open no database of their own")

    def fingerprint(self, path):
        raise AssertionError("not used")

    def acquisition(self, row, path):
        raise AssertionError("not used")
