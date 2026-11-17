"""Owner-facing names and one-line explanations for each review category."""

from __future__ import annotations

CATEGORY_LABELS = {
    "rusak": "Rusak (corrupt)",
    "duplikat-persis": "Duplikat identik",
    "duplikat-versi": "Duplikat beda versi",
    "mirip-beruntun": "Burst / mirip",
    "screenshot": "Screenshot",
    "kiriman-chat": "Chat (WA/Telegram)",
    "download-meme": "Download / meme",
    "pendamping-gopro": "File GoPro (LRV/THM)",
    "sampah-hp": "Trash HP",
    "video-pendek-gelap": "Video pendek / gelap",
    "kepencet": "Kepencet (tidak sengaja)",
    "burem": "Blur",
    "gelap-terang": "Terlalu gelap / terang",
}

_GRID_TAIL = "Yang tidak kamu Keep pindah ke _sampah saat Apply."
_GROUP_TAIL = "Saran kami: Keep satu, sisanya pindah ke _sampah saat Apply."

CATEGORY_HELP = {
    "rusak": f"File yang tidak bisa dibuka atau rusak. {_GRID_TAIL}",
    "duplikat-persis": f"Foto atau video yang isinya sama persis. {_GROUP_TAIL}",
    "duplikat-versi": f"Foto yang sama dalam versi berbeda, misalnya ukuran atau kompresi lain. {_GROUP_TAIL}",
    "mirip-beruntun": f"Jepretan beruntun yang hampir sama (burst). {_GROUP_TAIL}",
    "screenshot": f"Tangkapan layar HP. {_GRID_TAIL}",
    "kiriman-chat": f"Foto dan video kiriman WhatsApp/Telegram. {_GRID_TAIL}",
    "download-meme": f"Gambar hasil download atau meme dari internet. {_GRID_TAIL}",
    "pendamping-gopro": f"File pendamping GoPro (LRV/THM) yang bukan video utama. {_GRID_TAIL}",
    "sampah-hp": f"File dari folder sampah atau cache HP. {_GRID_TAIL}",
    "video-pendek-gelap": f"Video yang sangat pendek atau gelap, biasanya tidak sengaja terekam. {_GRID_TAIL}",
    "kepencet": f"Foto yang kemungkinan terjepret tidak sengaja. {_GRID_TAIL}",
    "burem": f"Foto yang Blur atau tidak fokus. {_GRID_TAIL}",
    "gelap-terang": f"Foto yang terlalu gelap atau terlalu terang. {_GRID_TAIL}",
}
