"""Indonesian user-facing messages for engine errors."""

from __future__ import annotations

from ..decisions import PurgeMismatch
from ..mover import InsufficientSpace, NotUndoable, PendingMoves, UnknownBatch
from ..pipeline import ScanIncomplete


class UserError(Exception):
    """Raised by routes; becomes JSON {"detail": message} with `status`."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_BY_CLASS = {
    PendingMoves: (
        "Ada perpindahan yang terputus dan belum bisa diselesaikan (harddisk terlepas?). "
        "Tidak ada file yang dipindah. Buka Riwayat untuk melihat dan melepaskannya."
    ),
    ScanIncomplete: "Pindai terakhir belum selesai. Jalankan pindai lagi sebelum merapikan.",
    InsufficientSpace: "Ruang disk tidak cukup untuk memindah file ini. Tidak ada file yang dipindah.",
    PurgeMismatch: "Isi _sampah berubah sejak kamu konfirmasi. Muat ulang halaman, lalu konfirmasi lagi.",
    UnknownBatch: "Batch tidak ditemukan.",
    NotUndoable: "Batch ini tidak bisa dibatalkan.",
}

# Classes from modules not importable here (yet) are matched by class name.
_BY_NAME = {
    "StillOnDisk": "File masih ada di disk, jadi baris ini tidak perlu dilepas. Coba jalankan lagi.",
    "EndpointOffline": "Folder file ini tidak bisa diakses (harddisk terlepas?). Sambungkan dulu, lalu coba lagi.",
    "HasPendingMove": (
        "File ini masih punya perpindahan yang tertunda. "
        "Lepaskan dulu di bagian Perpindahan terputus."
    ),
}


def explain(exc: BaseException) -> str:
    if isinstance(exc, UserError):
        return str(exc)
    for cls, msg in _BY_CLASS.items():
        if isinstance(exc, cls):
            return msg
    name = type(exc).__name__
    if name == "JobBusy":
        label = getattr(exc, "label", None) or (str(exc.args[0]) if exc.args else "")
        return f"Masih ada proses yang berjalan: {label}. Tunggu sampai selesai."
    if name in _BY_NAME:
        return _BY_NAME[name]
    return f"Terjadi kesalahan: {name}: {exc}"
