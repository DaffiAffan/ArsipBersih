from datetime import datetime

from arsipbersih.dating.clock import device_warnings

NOW = datetime(2026, 11, 17, 12, 0)


def _rows(n, make="Samsung", model="A52", taken="2024-05-01T10:00:00", name="IMG_{i}.jpg", src="HP"):
    return [
        {"name": name.format(i=i), "make": make, "model": model, "taken": taken,
         "date_source": "exif", "src": src, "path": f"Sumber/{src}/DCIM"}
        for i in range(n)
    ]


def test_device_with_year_1999_is_flagged(conn_with):
    conn = conn_with(_rows(1, taken="1999-01-01T00:00:00"))
    w = device_warnings(conn, NOW)
    assert len(w) == 1 and w[0].startswith("Jam Samsung A52 mungkin salah:") and "1999" in w[0]


def test_device_with_future_dates_is_flagged(conn_with):
    conn = conn_with(_rows(1, taken="2030-01-01T00:00:00"))
    w = device_warnings(conn, NOW)
    assert len(w) == 1 and "masa depan" in w[0]


def test_device_far_from_filename_dates_is_flagged(conn_with):
    conn = conn_with(_rows(6, make=None, model=None, src="Redmi",
                           name="IMG-20200101-WA{i:04d}.jpg"))
    w = device_warnings(conn, NOW)
    assert len(w) == 1 and w[0].startswith("Jam Redmi mungkin salah:")


def test_healthy_device_is_quiet(conn_with):
    rows = _rows(6, name="IMG_20240501_{i}.jpg") + _rows(3, make="Apple", model="iPhone", name="x{i}.jpg")
    assert device_warnings(conn_with(rows), NOW) == []
