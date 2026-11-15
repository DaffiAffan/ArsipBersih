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
