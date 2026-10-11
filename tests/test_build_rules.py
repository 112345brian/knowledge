import build_rules


def test_remove_db_files_removes_the_file_and_each_sidecar_through_the_given_remover():
    removed = []
    build_rules.remove_db_files(removed.append, "/out/x.db")
    assert removed == ["/out/x.db", "/out/x.db-journal", "/out/x.db-wal", "/out/x.db-shm"]
