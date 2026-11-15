import pytest

from arsipbersih.classify import run_classify
from arsipbersih.config import Settings


@pytest.mark.parametrize("row,expected", [
    (dict(name="x.jpg", decode_ok=0), "rusak"),
    (dict(name="Screenshot_20230812-143022.png"), "screenshot"),
    (dict(name="a.png", w=1080, h=2400), "screenshot"),
    (dict(name="IMG-20230812-WA0001.jpg", path="Sumber/HP/WhatsApp/Media/WhatsApp Images"), "kiriman-chat"),
    (dict(name="meme.webp"), "download-meme"),
    (dict(name="GL010123.LRV", kind="gopro_aux"), "pendamping-gopro"),
    (dict(name=".trashed-1690000000-IMG_1.jpg"), "sampah-hp"),
    (dict(name="VID_1.mp4", kind="video", duration=1.4), "video-pendek-gelap"),
    (dict(name="IMG_1.jpg", make="samsung", sharp=6, luma=12), "kepencet"),
    (dict(name="IMG_2.jpg", make="samsung", sharp=18, luma=120, std=50), "burem"),
    (dict(name="IMG_3.jpg", make="samsung", sharp=80, luma=8, std=30), "gelap-terang"),
    (dict(name="IMG_4.jpg", make="samsung", sharp=80, luma=120, std=50), None),
])
def test_primary_category(conn_with, row, expected):
    conn = conn_with([row]); run_classify(conn, Settings())
    assert conn.execute("select primary_category from units").fetchone()[0] == expected


def test_priority_and_secondary_flags(conn_with):
    conn = conn_with([dict(name="IMG-20230812-WA0001.jpg", path="Sumber/HP/WhatsApp", sharp=5, luma=120, std=50)])
    run_classify(conn, Settings())
    assert conn.execute("select primary_category from units").fetchone()[0] == "kiriman-chat"
    cats = {r[0] for r in conn.execute("select category from flags")}
    assert cats == {"kiriman-chat", "burem"}


def test_calibrated_threshold_overrides_fallback(conn_with):
    conn = conn_with([dict(name="IMG_2.jpg", make="samsung", sharp=30, luma=120, std=50)])
    run_classify(conn, Settings(blur_threshold=35.0))
    assert conn.execute("select primary_category from units").fetchone()[0] == "burem"
