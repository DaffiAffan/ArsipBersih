"""Owner-facing names and one-line explanations for each review category."""

from __future__ import annotations

CATEGORY_LABELS = {
    "rusak": "Rusak (corrupt)",
    "duplikat-persis": "Duplikat identik",
    "duplikat-versi": "Duplikat beda versi",
    "mirip-beruntun": "Burst / mirip",
    "dokumen-identitas": "Dokumen · Identitas",
    "dokumen-keuangan": "Dokumen · Keuangan",
    "dokumen-kertas": "Dokumen · Kertas & catatan",
    "screenshot": "Screenshot",
    "kiriman-chat": "Chat (WA/Telegram)",
    "download-meme": "Download / meme / grafis",
    "pendamping-gopro": "File GoPro (LRV/THM)",
    "sampah-hp": "Sampah HP",
    "rekaman-tidak-sengaja": "Rekaman tidak sengaja",
    "video-pendek-gelap": "Video pendek / gelap",
    "kepencet": "Kepencet (tidak sengaja)",
    "burem": "Blur",
    "gelap-terang": "Terlalu gelap / terang",
}

CATEGORY_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("duplikat", "Duplikat", ("duplikat-persis", "duplikat-versi", "mirip-beruntun")),
    ("dokumen", "Dokumen", ("dokumen-identitas", "dokumen-keuangan", "dokumen-kertas")),
    ("kiriman", "Kiriman & layar", ("kiriman-chat", "screenshot", "download-meme")),
    (
        "kualitas",
        "Kualitas",
        ("burem", "gelap-terang", "kepencet", "video-pendek-gelap", "rekaman-tidak-sengaja"),
    ),
    ("sistem", "File sistem", ("rusak", "pendamping-gopro", "sampah-hp")),
)

CATEGORY_SHORT_LABELS = {
    **CATEGORY_LABELS,
    "duplikat-persis": "Identik",
    "duplikat-versi": "Beda versi",
    "dokumen-identitas": "Identitas",
    "dokumen-keuangan": "Keuangan",
    "dokumen-kertas": "Kertas & catatan",
    "kiriman-chat": "Chat WA/Telegram",
    "download-meme": "Download / meme",
}

_GRID_TAIL = (
    "Saat Terapkan, yang kamu Simpan masuk Arsip dan yang kamu Buang masuk Sampah;"
    " sisanya tetap di sini."
)
_GROUP_TAIL = "Saran kami: simpan satu, buang sisanya."

CATEGORY_HELP = {
    "rusak": f"File yang tidak bisa dibuka atau rusak. {_GRID_TAIL}",
    "duplikat-persis": f"Foto atau video yang isinya sama persis. {_GROUP_TAIL}",
    "duplikat-versi": f"Foto yang sama dalam versi berbeda, misalnya ukuran atau kompresi lain. {_GROUP_TAIL}",
    "mirip-beruntun": f"Jepretan beruntun yang hampir sama (burst). {_GROUP_TAIL}",
    "dokumen-identitas": (
        "Foto yang ditebak sebagai KTP, SIM, paspor, atau kartu identitas lain. Tebakan ini harus"
        " dicek satu per satu. Yang kamu Simpan masuk terpisah ke Arsip/_Dokumen/Identitas, tidak"
        f" bercampur dengan foto kenangan. {_GRID_TAIL}"
    ),
    "dokumen-keuangan": (
        "Foto yang ditebak sebagai struk, nota, tagihan, atau bukti transfer. Yang kamu Simpan masuk"
        f" terpisah di Arsip/_Dokumen/Keuangan. {_GRID_TAIL}"
    ),
    "dokumen-kertas": (
        "Foto yang ditebak sebagai kertas atau catatan: dokumen, papan tulis, buku, atau tulisan"
        f" tangan. Yang kamu Simpan masuk terpisah di Arsip/_Dokumen/Kertas. {_GRID_TAIL}"
    ),
    "screenshot": f"Tangkapan layar HP. {_GRID_TAIL}",
    "kiriman-chat": f"Foto dan video kiriman WhatsApp/Telegram. {_GRID_TAIL}",
    "download-meme": f"Gambar hasil download, meme, atau grafis dari internet. {_GRID_TAIL}",
    "pendamping-gopro": f"File pendamping GoPro (LRV/THM) yang bukan video utama. {_GRID_TAIL}",
    "sampah-hp": f"File dari folder sampah atau cache HP. {_GRID_TAIL}",
    "rekaman-tidak-sengaja": (
        "Video yang hampir semuanya gelap, atau buram dan nyaris tidak bergerak. Kemungkinan besar"
        f" terekam tidak sengaja, misalnya di dalam kantong. Cek dulu sebelum dibuang. {_GRID_TAIL}"
    ),
    "video-pendek-gelap": f"Video yang sangat pendek atau gelap, biasanya tidak sengaja terekam. {_GRID_TAIL}",
    "kepencet": f"Foto yang kemungkinan terjepret tidak sengaja. {_GRID_TAIL}",
    "burem": f"Foto yang buram atau tidak fokus. {_GRID_TAIL}",
    "gelap-terang": f"Foto yang terlalu gelap atau terlalu terang. {_GRID_TAIL}",
}
