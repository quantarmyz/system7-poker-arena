"""equity: migración competition_id (idempotente) + log_equity retrocompatible."""
import importlib
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def _fresh(db_path, monkeypatch):
    monkeypatch.setenv("S7_STATS_DB", str(db_path))
    import s7_stats
    importlib.reload(s7_stats)
    return s7_stats


def test_equity_migration_idempotent(tmp_path, monkeypatch):
    s7_stats = _fresh(tmp_path / "eq.db", monkeypatch)
    s7_stats.init()
    s7_stats.init()   # segunda pasada: no debe fallar ni duplicar nada
    cols = {r[1] for r in sqlite3.connect(s7_stats.DB).execute("PRAGMA table_info(equity)")}
    assert "competition_id" in cols and "reentry" in cols


def test_log_equity_con_y_sin_comp(tmp_path, monkeypatch):
    s7_stats = _fresh(tmp_path / "eq2.db", monkeypatch)
    s7_stats.init()
    s7_stats.log_equity("playground", 10, 50.0, 60.0, reentry=1, competition_id="cX")
    s7_stats.log_equity("clasif-foo", 5, 20.0, 25.0)              # llamada legacy (Eval, posicional)
    rows = sqlite3.connect(s7_stats.DB).execute(
        "select run_label,hands,raw_chips,adj_chips,reentry,competition_id from equity order by ts").fetchall()
    assert rows[0] == ("playground", 10, 50.0, 60.0, 1, "cX")
    assert rows[1][0] == "clasif-foo" and rows[1][5] == ""


def test_migra_esquema_viejo(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE equity(ts REAL, run_label TEXT, hands INTEGER, "
              "raw_chips REAL, adj_chips REAL, reentry INTEGER DEFAULT 0)")
    c.execute("INSERT INTO equity VALUES(1.0,'playground',3,10.0,11.0,0)")
    c.commit(); c.close()
    s7_stats = _fresh(db, monkeypatch)
    s7_stats.init()
    con = sqlite3.connect(s7_stats.DB)
    assert con.execute("select competition_id from equity").fetchall() == [(None,)]
    s7_stats.log_equity("playground", 4, 12.0, 13.0, reentry=0, competition_id="cY")
    assert con.execute("select count(*) from equity where competition_id='cY'").fetchone()[0] == 1
