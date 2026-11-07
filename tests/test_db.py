import sqlite3

from sqlalchemy import inspect


def test_adds_new_columns_to_existing_db(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE cameras (id INTEGER PRIMARY KEY, name VARCHAR(128))")
    con.execute("INSERT INTO cameras (name) VALUES ('Main Gate')")
    con.commit()
    con.close()
    from coeus.db import init_engine
    engine = init_engine(f"sqlite:///{path}")
    cols = {c["name"] for c in inspect(engine).get_columns("cameras")}
    assert {"yt_account_id", "archive_enabled", "yt_privacy"} <= cols
    with engine.connect() as c:
        row = c.exec_driver_sql("SELECT name, yt_privacy, archive_enabled FROM cameras").one()
    assert tuple(row) == ("Main Gate", "private", 0)
