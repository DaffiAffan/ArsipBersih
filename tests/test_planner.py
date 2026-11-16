import pytest

from arsipbersih.planner import (
    NameReserver,
    archive_dir_for,
    compute_plans,
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
