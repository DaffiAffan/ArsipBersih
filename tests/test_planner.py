import pytest
from arsipbersih.db import new_id

from arsipbersih.planner import (
    NameReserver,
    archive_dir_for,
    compute_plans,
    plan_apply,
    plan_rapikan,
    review_dir_for,
)


@pytest.mark.parametrize("args,expected", [
    (("Rinjani", "Hari-1", "2023-07", "2023-08-01T06:00:00", "HP"), "Arsip/2023/2023-07 Rinjani/Hari-1"),
    (("Rinjani", None, None, None, "HP"), "Arsip/Tanpa-Tanggal/Rinjani"),
    ((None, None, None, "2023-08-12T14:30:22", "HP"), "Arsip/2023/2023-08"),
    ((None, None, None, None, "HP-Lama"), "Arsip/Tanpa-Tanggal/HP-Lama"),
])
def test_archive_dir_for(args, expected):
    assert archive_dir_for(*args) == expected


def test_review_dir_keeps_source_context():
    assert review_dir_for("_review", "burem", "Sumber/HP-Lama/DCIM/Camera/IMG_1.jpg") == "_review/burem/HP-Lama/DCIM/Camera"


def test_name_reserver_unit_consistent_suffix(root):
    d = root / "Arsip/2023/2023-08"; d.mkdir(parents=True); (d / "img_1234.heic").write_bytes(b"x")
    r = NameReserver(root)
    assert r.reserve_unit("Arsip/2023/2023-08", ["IMG_1234.HEIC", "IMG_1234.MOV"], "HP-Xiaomi") == \
        ["IMG_1234__HP-Xiaomi.HEIC", "IMG_1234__HP-Xiaomi.MOV"]
    assert r.reserve_unit("Arsip/2023/2023-08", ["IMG_1234.HEIC"], "HP-Xiaomi") == ["IMG_1234__HP-Xiaomi__2.HEIC"]


def test_plan_rapikan_routes_clean_keepers_and_flagged(conn_with, root):
    conn = conn_with([
        dict(name="IMG_1.jpg", taken="2023-08-12T10:00:00"),
        dict(name="IMG_2.jpg", taken="2023-08-12T10:00:01", category="burem"),
    ])
    compute_plans(conn)
    moves = {m.files[0].dst_rel.split("/")[0]: m for m in plan_rapikan(conn, root)}
    assert set(moves) == {"Arsip", "_review"}


def test_name_reserver_sidecar_keeps_pairing(root):
    from arsipbersih.pairing import _group_stem

    d = root / "Arsip/2023/2023-08"; d.mkdir(parents=True); (d / "IMG_1.JPG").write_bytes(b"x")
    names = NameReserver(root).reserve_unit("Arsip/2023/2023-08", ["IMG_1.JPG", "IMG_1.JPG.xmp"], "HP")
    assert names == ["IMG_1__HP.JPG", "IMG_1__HP.JPG.xmp"]
    assert _group_stem(names[0], "jpg") == _group_stem(names[1], "xmp")


def test_name_reserver_rejects_duplicate_names_in_unit(root):
    with pytest.raises(ValueError, match="duplicate file names"):
        NameReserver(root).reserve_unit("Arsip/x", ["A.jpg", "a.JPG"], "HP")


def test_name_reserver_respects_db_rows(conn_with, root):
    conn = conn_with([dict(name="IMG_1.jpg", path="Arsip/2023/2023-08", loc="archive")])
    r = NameReserver(root, conn)
    assert r.reserve_unit("Arsip/2023/2023-08", ["img_1.JPG"], "HP") == ["img_1__HP.JPG"]


def test_plan_rapikan_skips_unplannable_unit(conn_with, root):
    from arsipbersih.db import new_id

    conn = conn_with([
        dict(name="a?.jpg", taken="2023-08-12T10:00:00"),
        dict(name="IMG_2.jpg", taken="2023-08-12T10:00:01"),
    ])
    uid = conn.execute("select id from units where primary_file_id in "
                       "(select id from files where rel_path like '%a?.jpg')").fetchone()[0]
    conn.execute("insert into files(id, unit_id, rel_path, kind, ext) values(?,?,?,?,?)",
                 (new_id(), uid, "Sumber/HP/DCIM/a*.jpg", "photo", "jpg"))
    conn.commit()
    skipped = []
    moves = plan_rapikan(conn, root, skipped)
    assert [s[0] for s in skipped] == [uid]
    assert len(moves) == 1 and moves[0].unit_id != uid


def test_plan_rapikan_returns_uncategorized_review_unit_to_archive(conn_with, root):
    conn = conn_with([
        dict(name="IMG_1.jpg", path="_review/burem/HP/DCIM", loc="review", taken="2023-08-12T10:00:00"),
        dict(name="IMG_2.jpg", path="_review/burem/HP/DCIM", loc="review", taken="2023-08-12T10:00:01",
             category="burem"),
    ])
    compute_plans(conn)
    moves = plan_rapikan(conn, root)
    assert len(moves) == 1
    assert moves[0].to_location == "archive"
    assert moves[0].files[0].dst_rel == "Arsip/2023/2023-08/IMG_1.jpg"


def test_plan_rapikan_names_missing_path(root, conn_with):
    conn = conn_with([{"name": "IMG_1.jpg"}])
    conn.execute("update files set missing=1"); conn.commit()
    skipped = []
    assert plan_rapikan(conn, root, skipped) == []
    assert skipped[0][1] == "file missing on disk: Sumber/HP/DCIM/IMG_1.jpg"


def test_plan_apply_skips_unit_whose_file_is_gone(root, conn_with):
    conn = conn_with([{"name": "IMG_2.jpg", "loc": "review", "path": "_review/burem/HP",
                       "category": "burem"}])
    uid = conn.execute("select id from units").fetchone()[0]
    skipped = []
    assert plan_apply(conn, root, [], [uid], skipped) == []
    assert skipped == [(uid, "file missing on disk: _review/burem/HP/IMG_2.jpg")]


def _set_event(conn, name, key="bali", event="Bali"):
    conn.execute(
        "update units set event_key=?, event_name=? where primary_file_id in "
        "(select id from files where rel_path like ?)",
        (key, event, f"%/{name}"),
    )
    conn.commit()


def test_event_keeps_existing_archive_folder_month(conn_with, root):
    conn = conn_with([
        dict(name="OLD.jpg", path="Arsip/2024/2024-07 Bali", loc="archive", taken="2024-07-10T10:00:00"),
        dict(name="NEW.jpg", taken="2024-06-30T10:00:00"),
    ])
    _set_event(conn, "OLD.jpg")
    _set_event(conn, "NEW.jpg")
    moves = plan_rapikan(conn, root)
    assert moves[0].files[0].dst_rel == "Arsip/2024/2024-07 Bali/NEW.jpg"


def test_event_without_archived_unit_uses_earliest_month(conn_with, root):
    conn = conn_with([
        dict(name="A.jpg", taken="2024-07-10T10:00:00"),
        dict(name="B.jpg", taken="2024-06-30T10:00:00"),
    ])
    _set_event(conn, "A.jpg")
    _set_event(conn, "B.jpg")
    compute_plans(conn)
    dirs = {r[0] for r in conn.execute("select planned_archive_dir from units")}
    assert dirs == {"Arsip/2024/2024-06 Bali"}


def _doc_unit(conn_with, root, category, taken, names=("DOC_1.jpg",)):
    conn = conn_with([dict(name=names[0], loc="review", path="_review/" + category + "/HP",
                           taken=taken, category=category)])
    uid, fid = conn.execute("select id, primary_file_id from units").fetchone()
    rel = conn.execute("select rel_path from files where id=?", (fid,)).fetchone()[0]
    for extra in names[1:]:
        ext = extra.rsplit(".", 1)[-1].lower()
        conn.execute(
            "insert into files(id, unit_id, rel_path, orig_rel_path, size, kind, ext, source_label)"
            " values(?,?,?,?,?,?,?,?)",
            (new_id(), uid, rel.rsplit("/", 1)[0] + "/" + extra, rel.rsplit("/", 1)[0] + "/" + extra,
             10, "photo", ext, "HP"),
        )
    conn.commit()
    for (r,) in conn.execute("select rel_path from files").fetchall():
        (root / r).parent.mkdir(parents=True, exist_ok=True)
        (root / r).write_bytes(b"x")
    compute_plans(conn)
    return conn, uid


def test_keep_document_goes_to_dokumen_year(conn_with, root):
    conn, uid = _doc_unit(conn_with, root, "dokumen-identitas", "2022-03-04T10:00:00")
    (m,) = plan_apply(conn, root, [uid], [])
    assert m.files[0].dst_rel == "Arsip/_Dokumen/Identitas/2022/DOC_1.jpg"


def test_keep_document_without_date_goes_to_tanpa_tanggal(conn_with, root):
    conn, uid = _doc_unit(conn_with, root, "dokumen-keuangan", None)
    (m,) = plan_apply(conn, root, [uid], [])
    assert m.files[0].dst_rel == "Arsip/_Dokumen/Keuangan/Tanpa-Tanggal/DOC_1.jpg"


def test_keep_document_moves_whole_pair(conn_with, root):
    conn, uid = _doc_unit(conn_with, root, "dokumen-kertas", "2021-01-02T10:00:00",
                          names=("DOC_1.jpg", "DOC_1.dng"))
    (m,) = plan_apply(conn, root, [uid], [])
    assert sorted(f.dst_rel for f in m.files) == [
        "Arsip/_Dokumen/Kertas/2021/DOC_1.dng", "Arsip/_Dokumen/Kertas/2021/DOC_1.jpg"]


def test_keep_screenshot_unchanged_destination(conn_with, root):
    conn, uid = _doc_unit(conn_with, root, "screenshot", "2022-03-04T10:00:00")
    (m,) = plan_apply(conn, root, [uid], [])
    assert m.files[0].dst_rel == "Arsip/2022/2022-03/DOC_1.jpg"


def test_compute_plans_stores_normal_archive_dir_for_documents(conn_with, root):
    # The document folder is chosen at move time from the current category, never stored.
    conn, uid = _doc_unit(conn_with, root, "dokumen-identitas", "2022-03-04T10:00:00")
    assert conn.execute("select planned_archive_dir from units").fetchone()[0] == "Arsip/2022/2022-03"


@pytest.mark.parametrize("new_category", ["download-meme", None])
def test_keep_after_category_leaves_documents_goes_to_normal_archive(conn_with, root, new_category):
    # e.g. a correction dokumen-kertas -> Grafis / Foto biasa after the plans were computed
    conn, uid = _doc_unit(conn_with, root, "dokumen-kertas", "2021-01-02T10:00:00")
    conn.execute("update units set primary_category=? where id=?", (new_category, uid))
    conn.commit()
    (m,) = plan_apply(conn, root, [uid], [])
    assert m.files[0].dst_rel == "Arsip/2021/2021-01/DOC_1.jpg"


def test_rapikan_source_unit_no_longer_document_goes_to_normal_archive(conn_with, root):
    conn = conn_with([dict(name="DOC_1.jpg", taken="2021-01-02T10:00:00", category="dokumen-kertas")])
    uid = conn.execute("select id from units").fetchone()[0]
    compute_plans(conn)
    conn.execute("update units set primary_category=null where id=?", (uid,))  # classify --head
    conn.commit()
    (m,) = plan_rapikan(conn, root)
    assert m.to_location == "archive"
    assert m.files[0].dst_rel == "Arsip/2021/2021-01/DOC_1.jpg"


def _flag(conn, uid, category):
    conn.execute(
        "insert into flags(id, unit_id, category, score, reason) values(?,?,?,NULL,'test')",
        (new_id(), uid, category),
    )
    conn.commit()


def test_keep_grouped_member_with_document_flag_goes_to_dokumen(conn_with, root):
    conn, uid = _doc_unit(conn_with, root, "duplikat-persis", "2022-03-04T10:00:00")
    _flag(conn, uid, "dokumen-kertas")
    _flag(conn, uid, "dokumen-keuangan")  # keuangan outranks kertas
    (m,) = plan_apply(conn, root, [uid], [])
    assert m.files[0].dst_rel == "Arsip/_Dokumen/Keuangan/2022/DOC_1.jpg"


def test_keep_grouped_member_without_document_flag_goes_to_normal_archive(conn_with, root):
    conn, uid = _doc_unit(conn_with, root, "duplikat-persis", "2022-03-04T10:00:00")
    (m,) = plan_apply(conn, root, [uid], [])
    assert m.files[0].dst_rel == "Arsip/2022/2022-03/DOC_1.jpg"


def test_rapikan_source_review_folder_follows_current_category(conn_with, root):
    conn = conn_with([dict(name="A_1.jpg", taken="2022-03-04T10:00:00", category="screenshot")])
    uid = conn.execute("select id from units").fetchone()[0]
    compute_plans(conn)
    conn.execute("update units set primary_category='download-meme' where id=?", (uid,))
    conn.commit()  # classify --head after the plans were computed
    (m,) = plan_rapikan(conn, root)
    assert m.to_location == "review"
    assert m.files[0].dst_rel.startswith("_review/download-meme/")


# --- Fase 2: effective dates -------------------------------------------------------


def _uid(conn, name):
    return conn.execute(
        "select unit_id from files where rel_path like ?", (f"%/{name}",)
    ).fetchone()[0]


def _manual(conn, name, taken_at, precision="day"):
    from arsipbersih.dating.store import set_override

    set_override(conn, _uid(conn, name), taken_at, precision, "manual", None, "owner")


def test_undated_archive_unit_moves_after_manual_date(conn_with, root):
    conn = conn_with([
        dict(name="DSC_1.jpg", path="Arsip/Tanpa-Tanggal/HP", loc="archive"),
        dict(name="DSC_2.jpg", path="Arsip/Tanpa-Tanggal/HP", loc="archive"),  # stays undated
        dict(name="DOC.jpg", path="Arsip/_Dokumen/Identitas/Tanpa-Tanggal", loc="archive"),
    ])
    _manual(conn, "DSC_1.jpg", "2019-05-01T12:00:00", "month")
    _manual(conn, "DOC.jpg", "2019-05-01T12:00:00", "month")
    (m,) = plan_rapikan(conn, root)
    assert m.unit_id == _uid(conn, "DSC_1.jpg")
    assert m.to_location == "archive"
    assert m.files[0].src_rel == "Arsip/Tanpa-Tanggal/HP/DSC_1.jpg"
    assert m.files[0].dst_rel == "Arsip/2019/2019-05/DSC_1.jpg"
    assert m.reason == "pindah karena tanggal dikoreksi"


def test_archive_event_unit_is_not_moved_when_date_changes(conn_with, root):
    conn = conn_with([
        dict(name="A.jpg", path="Arsip/2024/2024-07 Bali", loc="archive", taken="2024-07-10T10:00:00"),
        dict(name="B.jpg", path="Arsip/Tanpa-Tanggal/Pesta", loc="archive"),
    ])
    _set_event(conn, "A.jpg")
    _set_event(conn, "B.jpg", key="pesta", event="Pesta")
    _manual(conn, "A.jpg", "2023-01-05T10:00:00")
    _manual(conn, "B.jpg", "2023-01-05T10:00:00")
    assert plan_rapikan(conn, root) == []


def test_month_folder_unit_moves_when_month_changes(conn_with, root):
    conn = conn_with([
        dict(name="IMG-1.jpg", path="Arsip/2024/2024-03", loc="archive",
             taken="2024-03-02T00:00:00", date_source="filename"),
        dict(name="IMG-2.jpg", path="Arsip/2024/2024-03", loc="archive",
             taken="2024-03-02T00:00:00", date_source="filename"),
    ])
    _manual(conn, "IMG-1.jpg", "2023-12-24T10:00:00")
    _manual(conn, "IMG-2.jpg", "2024-03-20T10:00:00")  # same month: stays
    (m,) = plan_rapikan(conn, root)
    assert m.unit_id == _uid(conn, "IMG-1.jpg")
    assert m.files[0].dst_rel == "Arsip/2023/2023-12/IMG-1.jpg"
    assert m.reason == "pindah karena tanggal dikoreksi"


def test_event_month_uses_effective_dates(conn_with, root):
    conn = conn_with([
        dict(name="CAM.jpg", taken="2024-07-10T10:00:00", date_source="exif"),
        dict(name="WA.jpg", taken="2024-06-01T00:00:00", date_source="filename"),
    ])
    _set_event(conn, "CAM.jpg")
    _set_event(conn, "WA.jpg")
    from arsipbersih.dating.store import set_override

    set_override(conn, _uid(conn, "WA.jpg"), "2024-07-10T11:00:00", "second", "event", "bali", "owner")
    compute_plans(conn)
    dirs = {r[0] for r in conn.execute("select planned_archive_dir from units")}
    assert dirs == {"Arsip/2024/2024-07 Bali"}


def test_plan_uses_effective_date_for_source_unit(conn_with, root):
    conn = conn_with([dict(name="DSC_9.jpg")])
    _manual(conn, "DSC_9.jpg", "2018-02-01T12:00:00", "month")
    (m,) = plan_rapikan(conn, root)
    assert m.files[0].dst_rel == "Arsip/2018/2018-02/DSC_9.jpg"
    assert m.reason is None


def test_summary_lists_date_corrected_moves_separately(conn_with, root):
    from arsipbersih.pipeline import summarize_rapikan

    conn = conn_with([
        dict(name="DSC_1.jpg", path="Arsip/Tanpa-Tanggal/HP", loc="archive"),
        dict(name="IMG_2.jpg", taken="2023-08-12T10:00:00"),
    ])
    _manual(conn, "DSC_1.jpg", "2019-05-01T12:00:00", "month")
    s = summarize_rapikan(conn, root)
    assert s.archive_units == 1
    assert list(s.by_reason) == ["pindah karena tanggal dikoreksi"]
    assert s.by_reason["pindah karena tanggal dikoreksi"][0] == 1
