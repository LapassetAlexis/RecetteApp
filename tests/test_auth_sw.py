"""Auth active : le service worker reste public, sinon il ne s'enregistre pas
(le navigateur reçoit une redirection vers /login au lieu du script)."""

from tests.test_auth import _cleanup_env, _client  # noqa: F401  (fixture autouse réutilisée)


def test_sw_js_public_avec_auth(tmp_path):
    with _client(tmp_path, "chef", "secret") as c:
        r = c.get("/sw.js", follow_redirects=False)
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/javascript")
        assert r.headers["service-worker-allowed"] == "/"


def test_pages_toujours_protegees_avec_auth(tmp_path):
    with _client(tmp_path, "chef", "secret") as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 303 and "/login" in r.headers["location"]
