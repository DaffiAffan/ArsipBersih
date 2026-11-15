from __future__ import annotations

from arsipbersih.db import open_db
from arsipbersih.formats import Kind, detect_kind
from arsipbersih.inventory import run_inventory


def test_detect_kind_uses_magic_bytes(root, make_image):
    heic_as_jpg = make_image(root / "Sumber/HP/x.jpg", fmt="HEIF")
    upper = make_image(root / "Sumber/HP/A.JPG")
    png_named_jpeg = make_image(root / "Sumber/HP/b.jpeg", fmt="PNG")
    (root / "Sumber/HP/notes.txt").write_text("hi")
    (root / "Sumber/HP/empty.jpg").write_bytes(b"")
    assert detect_kind(heic_as_jpg) is Kind.PHOTO
    assert detect_kind(upper) is Kind.PHOTO
    assert detect_kind(png_named_jpeg) is Kind.PHOTO
    assert detect_kind(root / "Sumber/HP/notes.txt") is Kind.OTHER
    assert detect_kind(root / "Sumber/HP/empty.jpg") is Kind.PHOTO


def test_inventory_labels_skips_and_counts(root, make_image):
    make_image(root / "Sumber/HP-Lama/DCIM/Camera/IMG_1.jpg")
    make_image(root / "Sumber/HP-Lama/.thumbnails/t1.jpg")
    make_image(root / "Sumber/Laptop/Rinjani/IMG_2.jpg")
    (root / "Sumber/HP-Lama/.nomedia").write_bytes(b"")
    (root / "Sumber/HP-Lama/app.apk").write_bytes(b"PK\x03\x04")
    (root / "_review/burem").mkdir(parents=True)
    make_image(root / "_review/burem/old.jpg")
    conn = open_db(root)
    r = run_inventory(conn, root)
    rows = {x["rel_path"]: x["source_label"] for x in conn.execute("select rel_path, source_label from files")}
    assert rows == {"Sumber/HP-Lama/DCIM/Camera/IMG_1.jpg": "HP-Lama", "Sumber/Laptop/Rinjani/IMG_2.jpg": "Laptop"}
    assert (r.added, r.non_media, r.skipped_dirs) == (2, 1, 1)


def test_inventory_is_incremental(root, make_image):
    p = make_image(root / "Sumber/HP/IMG_1.jpg")
    conn = open_db(root)
    run_inventory(conn, root)
    r2 = run_inventory(conn, root)
    assert (r2.added, r2.unchanged) == (0, 1)
    p.unlink()
    assert run_inventory(conn, root).missing == 1


def test_inventory_unicode_names(root, make_image):
    make_image(root / "Sumber/HP/Foto Liburan 🏝️ Bali/ÖZİL_çay.jpg")
    conn = open_db(root)
    run_inventory(conn, root)
    assert conn.execute("select count(*) from files").fetchone()[0] == 1


def test_gif_with_ts_sync_byte_is_photo(root):
    p = root / "Sumber/HP/a.gif"
    p.parent.mkdir(parents=True)
    data = bytearray(b"GIF89a" + bytes(300))
    data[188] = 0x47
    p.write_bytes(bytes(data))
    assert detect_kind(p) is Kind.PHOTO
