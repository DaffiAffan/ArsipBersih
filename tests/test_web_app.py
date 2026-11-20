from fastapi.testclient import TestClient

from arsipbersih.mover import PendingMoves
from arsipbersih.web.app import create_app
from arsipbersih.web.messages import explain


def test_home_renders_in_indonesian(web):
    r = web.get("/")
    assert r.status_code == 200 and "Beranda" in r.text and "Riwayat" in r.text


def test_foreign_host_is_rejected(root):
    app = create_app(root, start_jobs=False)
    with TestClient(app) as c:
        assert c.get("/", headers={"host": "evil.example"}).status_code == 400
        assert c.get("/", headers={"host": "127.0.0.1:8765"}).status_code == 200


def test_post_without_header_is_rejected(web):
    r = web.post("/api/anything", headers={"X-ArsipBersih": ""})
    assert r.status_code == 403


def test_static_files_are_local(web):
    assert web.get("/static/core/api.js").status_code == 200
    assert "http" not in web.get("/").text.split("<body", 1)[0].replace("http-equiv", "")


def test_explain_maps_known_and_unknown_errors():
    assert explain(PendingMoves("x")).startswith("Ada perpindahan yang terputus")
    assert explain(ValueError("boom")) == "Terjadi kesalahan: ValueError: boom"


def test_favicon_is_served_locally(web):
    assert '<link rel="icon" href="/static/favicon.svg"' in web.get("/").text
    r = web.get("/static/favicon.svg")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")


def test_shell_uses_token_stylesheets(web):
    html = web.get("/").text
    for css in ("tokens.css", "base.css", "components/button.css", "components/sidebar.css"):
        assert f'href="/static/css/{css}"' in html
    for gone in ("components.css", "review.css", "pages.css"):
        assert f'href="/static/css/{gone}"' not in html
    assert "/static/app.css" not in html
    assert 'id="nav-review"' in html
    assert web.get("/static/css/tokens.css").status_code == 200


def test_category_labels_are_embedded_for_the_viewer(web):
    html = web.get("/").text
    assert 'id="category-labels"' in html
    assert "Chat (WA/Telegram)" in html


def test_reduced_motion_is_respected(web):
    assert "prefers-reduced-motion" in web.get("/static/css/base.css").text


def test_sr_only_utility_lives_in_base_css_only(web):
    assert ".sr-only" in web.get("/static/css/base.css").text
    for page in ("home", "review", "dates", "plan", "history", "report", "settings", "viewer"):
        r = web.get(f"/static/css/pages/{page}.css")
        assert r.status_code == 200 and ".sr-only" not in r.text


def test_sidebar_has_groups_and_no_progress(web):
    html = web.get("/").text
    aside = html.split("<aside", 1)[1].split("</aside>", 1)[0]
    assert 'class="nav-group"' in aside
    assert "Lainnya" in aside
    assert 'role="progressbar"' not in aside
    for label in ("Beranda", "Rencana", "Tanggal", "Riwayat", "Laporan", "Pengaturan"):
        assert label in aside
