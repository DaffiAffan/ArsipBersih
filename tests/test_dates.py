from datetime import datetime

import pytest

from arsipbersih.dates import parse_exif_datetime, parse_filename_date, pick_taken_at


@pytest.mark.parametrize("name,expected", [
    ("IMG_20230812_143022.jpg", datetime(2023, 8, 12, 14, 30, 22)),
    ("IMG-20230812-WA0001.jpg", datetime(2023, 8, 12)),
    ("VID-20230812-WA0007.mp4", datetime(2023, 8, 12)),
    ("VID_20220304_201544.mp4", datetime(2022, 3, 4, 20, 15, 44)),
    ("PXL_20230812_073015123.jpg", datetime(2023, 8, 12, 7, 30, 15)),
    ("MVIMG_20230812_073015.jpg", datetime(2023, 8, 12, 7, 30, 15)),
    ("Screenshot_2023-08-12-14-30-22-123_com.whatsapp.jpg", datetime(2023, 8, 12, 14, 30, 22)),
    ("Screenshot_20230812-143022.png", datetime(2023, 8, 12, 14, 30, 22)),
    ("20230812_143022.jpg", datetime(2023, 8, 12, 14, 30, 22)),
    ("DSC_0001.JPG", None), ("GX010123.MP4", None),
])
def test_parse_filename_date(name, expected):
    assert parse_filename_date(name) == expected


@pytest.mark.parametrize("s,expected", [
    ("2023:08:12 14:30:22", datetime(2023, 8, 12, 14, 30, 22)),
    ("2023:08:12 14:30:22.45", datetime(2023, 8, 12, 14, 30, 22)),
    ("2023:08:12 14:30:22+07:00", datetime(2023, 8, 12, 14, 30, 22)),
    ("2023-08-12 14:30:22", datetime(2023, 8, 12, 14, 30, 22)),
    ("2023:08:12 14:30:22Z", datetime(2023, 8, 12, 14, 30, 22)),
    ("2023-08-31T20:00:00.000000Z", datetime(2023, 8, 31, 20, 0, 0)),
    ("0000:00:00 00:00:00", None), ("garbage", None), ("", None), (None, None),
])
def test_parse_exif_datetime(s, expected):
    assert parse_exif_datetime(s) == expected


NOW = datetime(2026, 11, 15, 12, 0)


def test_pick_prefers_exif_then_quicktime_utc_then_filename():
    assert pick_taken_at("2023:08:12 14:30:22", None, "x.jpg", "Asia/Jakarta", NOW) == (datetime(2023, 8, 12, 14, 30, 22), "exif")
    assert pick_taken_at(None, "2023:08:31 20:00:00", "x.mp4", "Asia/Jakarta", NOW) == (datetime(2023, 9, 1, 3, 0, 0), "quicktime")
    assert pick_taken_at(None, "2023-08-31T20:00:00.000000Z", "x.mp4", "Asia/Jakarta", NOW) == (datetime(2023, 9, 1, 3, 0, 0), "quicktime")
    assert pick_taken_at(None, None, "IMG_20230812_143022.jpg", "Asia/Jakarta", NOW) == (datetime(2023, 8, 12, 14, 30, 22), "filename")


def test_pick_rejects_invalid_and_falls_through():
    assert pick_taken_at("0000:00:00 00:00:00", None, "IMG_20230812_143022.jpg", "Asia/Jakarta", NOW)[1] == "filename"
    assert pick_taken_at("1980:01:01 00:00:00", None, "DSC_1.JPG", "Asia/Jakarta", NOW) == (None, None)
    assert pick_taken_at("2031:01:01 00:00:00", None, "DSC_1.JPG", "Asia/Jakarta", NOW) == (None, None)
