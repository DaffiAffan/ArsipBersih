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
