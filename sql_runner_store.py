"""Adapter for `sql_runner_port.SqlRunner`: executes scripts and statements against a sqlite file. Stateless; the
module is the adapter."""
import sqlite3


def run(path, scripts, statements):
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        for script in scripts:
            con.executescript(script)
        for sql, params in statements:
            con.execute(sql, params)
        con.commit()
    finally:
        con.close()
