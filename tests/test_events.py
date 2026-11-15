import pytest

from arsipbersih.config import DEFAULT_GENERIC_FOLDERS, Settings
from arsipbersih.events import add_override, resolve_event, run_events

G = set(map(str.casefold, DEFAULT_GENERIC_FOLDERS))


@pytest.mark.parametrize("segs,expected", [
    (["DCIM", "Camera"], (None, None)),
    (["DCIM", "100ANDRO"], (None, None)),
    (["Rinjani"], ("Rinjani", None)),
    (["Pictures", "Rinjani", "Hari-1", "DCIM"], ("Rinjani", "Hari-1")),
    (["2023-08-12", "Argo"], ("Argo", None)),
    (["WhatsApp", "Media", "WhatsApp Images", "Sent"], (None, None)),
])
def test_resolve_event(segs, expected):
    assert resolve_event(segs, G, set()) == expected


def test_not_event_override_skips_segment():
    assert resolve_event(["Users", "daffi", "Bali"], G, {"users", "daffi"}) == ("Bali", None)


def test_same_event_merges_across_sources_and_uses_earliest_month(conn_with):
    conn = conn_with([
        dict(name="a.jpg", path="Sumber/HP-Lama/Rinjani", taken="2023-08-01T06:00:00"),
        dict(name="b.jpg", path="Sumber/HP-Xiaomi/rinjani ", taken="2023-07-31T21:00:00"),
        dict(name="c.jpg", path="Sumber/HP-Xiaomi/Rinjani 2023", taken="2023-08-02T08:00:00"),
    ])
    ev = {e.key: e for e in run_events(conn, Settings())}
    assert ev["rinjani"].month == "2023-07" and ev["rinjani"].file_count == 2
    assert sorted(ev["rinjani"].sources) == ["HP-Lama", "HP-Xiaomi"]
    assert "rinjani 2023" in ev


def test_rename_and_merge_overrides(conn_with):
    conn = conn_with([dict(name="a.jpg", path="Sumber/L/Argo"), dict(name="b.jpg", path="Sumber/L/Argo Parahyangan")])
    add_override(conn, "merge", "Argo Parahyangan", "Argo")
    add_override(conn, "rename", "Argo", "Kereta Argo")
    names = {e.name for e in run_events(conn, Settings())}
    assert names == {"Kereta Argo"}
