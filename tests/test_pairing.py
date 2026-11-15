from __future__ import annotations

from arsipbersih.config import Settings
from arsipbersih.db import open_db
from arsipbersih.inventory import run_inventory
from arsipbersih.pairing import build_units, confirm_live_photos, gopro_clip_id


def _files(root, conn, *names):
    for n in names:
        p = root / "Sumber/HP/DCIM" / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    run_inventory(conn, root)


def test_live_photo_raw_jpg_and_sidecars(root):
    conn = open_db(root)
    _files(root, conn, "IMG_1.HEIC", "IMG_1.MOV", "IMG_1.AAE", "DSC_2.NEF", "DSC_2.JPG", "DSC_2.xmp", "IMG_3.JPG.xmp")
    build_units(conn)
    kinds = sorted(r[0] for r in conn.execute("select pair_kind from units"))
    assert kinds == ["live_photo", "raw_jpg"]
    assert conn.execute("select unit_id from files where rel_path like '%IMG_3.JPG.xmp'").fetchone()[0] is None
    sizes = sorted(r[0] for r in conn.execute("select count(*) from files where unit_id is not null group by unit_id"))
    assert sizes == [3, 3]


def test_gopro_chapters_and_aux_are_separate_units(root):
    conn = open_db(root)
    _files(root, conn, "GX010123.MP4", "GX020123.MP4", "GL010123.LRV", "GX010123.THM")
    build_units(conn)
    assert conn.execute("select count(*) from units").fetchone()[0] == 4
    assert gopro_clip_id("GX020123") == "0123" and gopro_clip_id("IMG_0123") is None


def test_confirm_live_photos_splits_long_mov(root):
    conn = open_db(root)
    _files(root, conn, "IMG_9.JPG", "IMG_9.MOV")
    build_units(conn)
    mov_id = conn.execute("select id from files where ext='mov'").fetchone()[0]
    conn.execute("insert into media_info(file_id, duration_s) values (?, 12.0)", (mov_id,))
    assert confirm_live_photos(conn, Settings()) == 1
    assert conn.execute("select count(*) from units where pair_kind='single'").fetchone()[0] == 2


def test_pairs_stay_intact_with_extra_media(root):
    conn = open_db(root)
    _files(root, conn, "DSC_5.NEF", "DSC_5.JPG", "DSC_5.xmp", "DSC_5.MOV", "IMG_6.HEIC", "IMG_6.JPG", "IMG_6.MOV")
    build_units(conn)

    def unit_of(name):
        return conn.execute("select unit_id from files where rel_path like ?", (f"%{name}",)).fetchone()[0]

    def pair_kind(uid):
        return conn.execute("select pair_kind from units where id=?", (uid,)).fetchone()[0]

    assert unit_of("DSC_5.NEF") == unit_of("DSC_5.JPG") == unit_of("DSC_5.xmp")
    assert pair_kind(unit_of("DSC_5.NEF")) == "raw_jpg"
    assert pair_kind(unit_of("DSC_5.MOV")) == "single"
    assert unit_of("DSC_5.MOV") != unit_of("DSC_5.NEF")
    assert unit_of("IMG_6.HEIC") == unit_of("IMG_6.MOV")
    assert pair_kind(unit_of("IMG_6.HEIC")) == "live_photo"
    assert pair_kind(unit_of("IMG_6.JPG")) == "single"
