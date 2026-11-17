from __future__ import annotations

import sqlite3

from arsipbersih.dating import store
from arsipbersih.db import SCHEMA, open_db


def _unit(conn, uid, taken_at=None, source=None):
    conn.execute(
        "insert into units (id, taken_at, date_source) values (?, ?, ?)", (uid, taken_at, source)
    )
    conn.commit()


def test_manual_override_wins_over_raw_date(tmp_path):
    conn = open_db(tmp_path)
    _unit(conn, "u1", "2020-05-01T10:00:00", "exif")
    assert store.effective_dates(conn)["u1"] == ("2020-05-01T10:00:00", "exif", "second")
    store.set_override(conn, "u1", "2019-03-01T12:00:00", "month", "manual", None, "owner")
    assert store.effective_dates(conn, ["u1"])["u1"] == ("2019-03-01T12:00:00", "manual", "month")


def test_auto_borrowed_never_beats_camera_date(tmp_path):
    conn = open_db(tmp_path)
    _unit(conn, "u1", "2020-05-01T10:00:00", "quicktime")
    _unit(conn, "u2", "2021-01-02T00:00:00", "filename")
    _unit(conn, "u3")
    for u in ("u1", "u2", "u3"):
        store.set_override(conn, u, "2018-01-01T09:00:00", "second", "borrowed", "orig", "auto")
    d = store.effective_dates(conn)
    assert d["u1"][1] == "quicktime"
    assert d["u2"] == ("2018-01-01T09:00:00", "borrowed", "second")
    assert d["u3"][1] == "borrowed"


def test_newest_override_wins(tmp_path):
    conn = open_db(tmp_path)
    _unit(conn, "u1", "2021-01-02T00:00:00", "filename")
    store.set_override(conn, "u1", "2019-01-01T00:00:00", "day", "manual", None, "owner")
    store.set_override(conn, "u1", "2019-02-01T00:00:00", "day", "manual", None, "owner")
    assert store.effective_dates(conn)["u1"][0] == "2019-02-01T00:00:00"
    # owner beats a newer auto row
    store.set_override(conn, "u1", "2018-01-01T00:00:00", "second", "borrowed", "o", "auto")
    assert store.effective_dates(conn)["u1"][0] == "2019-02-01T00:00:00"


def test_clear_auto_keeps_owner_rows(tmp_path):
    conn = open_db(tmp_path)
    _unit(conn, "u1", "2021-01-02T00:00:00", "filename")
    _unit(conn, "u2", "2021-01-02T00:00:00", "filename")
    store.set_override(conn, "u1", "2018-01-01T00:00:00", "second", "borrowed", "o", "auto")
    store.set_override(conn, "u2", "2017-01-01T00:00:00", "day", "event", "ev", "owner")
    store.clear_auto_overrides(conn, "borrowed")
    d = store.effective_dates(conn)
    assert d["u1"][1] == "filename"
    assert d["u2"][1] == "event"
    # commit=False leaves the transaction open
    store.set_override(conn, "u1", "2016-01-01T00:00:00", "day", "event", "e", "auto", commit=False)
    assert conn.in_transaction
    conn.rollback()
    assert store.effective_dates(conn)["u1"][1] == "filename"


def test_view_reports_day_precision_for_wa_names(tmp_path):
    conn = open_db(tmp_path)
    _unit(conn, "wa", "2023-04-05T00:00:00", "filename")
    _unit(conn, "wat", "2023-04-05T13:14:15", "filename")
    _unit(conn, "ex", "2023-04-05T00:00:00", "exif")
    _unit(conn, "none")
    d = store.effective_dates(conn)
    assert d["wa"][2] == "day"
    assert d["wat"][2] == "second"
    assert d["ex"][2] == "second"
    assert d["none"] == (None, None, None)


def test_event_decisions_newest_per_key(tmp_path):
    conn = open_db(tmp_path)
    store.record_event_decision(conn, "k", "ignored", None, None, "2020-01-01", "2020-01-05")
    store.record_event_decision(conn, "k", "approved", "2020-01-01", "2020-01-06", "2020-01-01", "2020-01-05")
    store.record_event_decision(conn, "j", "ignored", None, None, None, None)
    d = store.event_decisions(conn)
    assert d["k"]["decision"] == "approved" and d["k"]["range_end"] == "2020-01-06"
    assert d["j"]["decision"] == "ignored"


def test_old_db_gets_date_tables(tmp_path):
    root = tmp_path / "c"
    conn = open_db(root)
    conn.execute("drop view unit_dates")
    for t in ("date_overrides", "event_date_decisions", "xmp_writes"):
        conn.execute(f"drop table {t}")
    conn.execute("pragma user_version=2")
    conn.commit()
    conn.close()
    conn = open_db(root)
    names = {r[0] for r in conn.execute("select name from sqlite_master")}
    assert {"date_overrides", "event_date_decisions", "xmp_writes", "unit_dates"} <= names
    assert conn.execute("pragma user_version").fetchone()[0] >= 3
    # SCHEMA is re-runnable
    conn.executescript(SCHEMA)
