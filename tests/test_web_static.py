import re
from pathlib import Path

IMPORT_RE = re.compile(r'''(?:import|from)\s*(?:[^'"]*?from\s*)?["'](/static/[^"']+|\.{1,2}/[^"']+)["']''')


def test_every_js_import_and_script_tag_resolves():
    static = Path("src/arsipbersih/web/static")
    for js in static.rglob("*.js"):
        for spec in IMPORT_RE.findall(js.read_text(encoding="utf-8")):
            target = (static / spec.removeprefix("/static/")) if spec.startswith("/static/") else (js.parent / spec)
            assert target.resolve().is_file(), f"{js}: {spec}"
    for tpl in Path("src/arsipbersih/web/templates").glob("*.html"):
        for src in re.findall(r'src="/static/([^"]+)"', tpl.read_text(encoding="utf-8")):
            assert (static / src).is_file(), f"{tpl.name}: {src}"


def test_static_root_holds_only_folders_and_assets():
    static = Path("src/arsipbersih/web/static")
    assert not list(static.glob("*.js")), "JS belongs in core/, components/ or pages/"


def test_inspector_never_passes_null_children():
    src = Path("src/arsipbersih/web/static/components/inspector.js").read_text(encoding="utf-8")
    assert ".filter(Boolean)" in src


CSS = Path("src/arsipbersih/web/static/css")


def test_font_sizes_only_from_tokens():
    for css in CSS.rglob("*.css"):
        if css.name == "tokens.css":
            continue
        assert not re.search(r"font-size:\s*[\d.]+(px|rem|em)", css.read_text(encoding="utf-8")), css


def test_colors_only_from_tokens():
    for css in CSS.rglob("*.css"):
        if css.name == "tokens.css":
            continue
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|oklch\(", css.read_text(encoding="utf-8")), css


def test_no_side_stripe_markers():
    for css in CSS.rglob("*.css"):
        text = css.read_text(encoding="utf-8")
        assert not re.search(r"border-(left|right):\s*([2-9]|\d{2,})px", text), css
        assert not re.search(r"inset\s+[2-9]px\s+0\s+0", text), css


def test_viewer_and_inspector_use_new_terms():
    base = Path("src/arsipbersih/web/static/components")
    text = "\n".join((base / n).read_text(encoding="utf-8") for n in ("viewer.js", "inspector.js"))
    strings = re.findall(r'"[^"\n]*"|`[^`\n]*`', text)
    for old in ("Keep", "Delete", "Apply", "_sampah", "Undo"):
        assert not [s for s in strings if old in s], old
    assert any("Simpan" in s for s in strings)
    assert any("Buang" in s for s in strings)
