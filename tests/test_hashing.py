from __future__ import annotations

import shutil

from arsipbersih.config import Settings
from arsipbersih.db import open_db
from arsipbersih.hashing import run_hashing, sha256_file
from arsipbersih.inventory import run_inventory


def test_only_size_collisions_are_hashed(root, make_image):
    a = make_image(root / "Sumber/HP/a.jpg", seed=1)
    (root / "Sumber/HP2").mkdir()
    shutil.copy(a, root / "Sumber/HP2/a_copy.jpg")
    make_image(root / "Sumber/HP/b.jpg", seed=2, size=(640, 480))
    conn = open_db(root); run_inventory(conn, root)
    assert run_hashing(conn, root, Settings()) == 2
    hashes = {r[0] for r in conn.execute("select sha256 from files where sha256 is not null")}
    assert hashes == {sha256_file(a)}


def test_hashing_resumes(root, make_image):
    a = make_image(root / "Sumber/HP/a.jpg"); (root / "Sumber/X").mkdir(); shutil.copy(a, root / "Sumber/X/a.jpg")
    conn = open_db(root); run_inventory(conn, root); run_hashing(conn, root, Settings())
    assert run_hashing(conn, root, Settings()) == 0
