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
    "sampah-hp": "Trash HP",
    "video-pendek-gelap": "Video pendek / gelap",
    "kepencet": "Kepencet (tidak sengaja)",
    "burem": "Blur",
    "gelap-terang": "Terlalu gelap / terang",
}

_GRID_TAIL = (
    "Saat Apply, yang pindah ke _sampah hanya yang di-Delete atau sudah dibuka di detail"
    " dan tidak di-Keep; sisanya tetap di review."
)
_GROUP_TAIL = "Saran kami: Keep satu, sisanya pindah ke _sampah saat Apply."

CATEGORY_HELP = {
    "rusak": f"File yang tidak bisa dibuka atau rusak. {_GRID_TAIL}",
    "duplikat-persis": f"Foto atau video yang isinya sama persis. {_GROUP_TAIL}",
    "duplikat-versi": f"Foto yang sama dalam versi berbeda, misalnya ukuran atau kompresi lain. {_GROUP_TAIL}",
    "mirip-beruntun": f"Jepretan beruntun yang hampir sama (burst). {_GROUP_TAIL}",
    "dokumen-identitas": (
        "Foto yang ditebak sebagai KTP, SIM, paspor, atau kartu identitas lain. Tebakan ini harus"
        " dicek satu per satu. Yang di-Keep disimpan terpisah di Arsip/_Dokumen/Identitas, tidak"
        f" bercampur dengan foto kenangan. {_GRID_TAIL}"
    ),
    "dokumen-keuangan": (
        "Foto yang ditebak sebagai struk, nota, tagihan, atau bukti transfer. Yang di-Keep disimpan"
        f" terpisah di Arsip/_Dokumen/Keuangan. {_GRID_TAIL}"
    ),
    "dokumen-kertas": (
        "Foto yang ditebak sebagai kertas atau catatan: dokumen, papan tulis, buku, atau tulisan"
        f" tangan. Yang di-Keep disimpan terpisah di Arsip/_Dokumen/Kertas. {_GRID_TAIL}"
    ),
    "screenshot": f"Tangkapan layar HP. {_GRID_TAIL}",
    "kiriman-chat": f"Foto dan video kiriman WhatsApp/Telegram. {_GRID_TAIL}",
    "download-meme": f"Gambar hasil download, meme, atau grafis dari internet. {_GRID_TAIL}",
    "pendamping-gopro": f"File pendamping GoPro (LRV/THM) yang bukan video utama. {_GRID_TAIL}",
    "sampah-hp": f"File dari folder sampah atau cache HP. {_GRID_TAIL}",
    "video-pendek-gelap": f"Video yang sangat pendek atau gelap, biasanya tidak sengaja terekam. {_GRID_TAIL}",
    "kepencet": f"Foto yang kemungkinan terjepret tidak sengaja. {_GRID_TAIL}",
    "burem": f"Foto yang Blur atau tidak fokus. {_GRID_TAIL}",
    "gelap-terang": f"Foto yang terlalu gelap atau terlalu terang. {_GRID_TAIL}",
}
