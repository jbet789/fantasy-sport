"""End to end: the installable-app pieces (manifest, icons, service worker, offline shell, install hint)."""
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_lineup import AppCase, URL, ROOT


class Installable(AppCase):
    def test_manifest_and_icons_are_complete(self):
        m = json.loads((ROOT / "app/manifest.webmanifest").read_text())
        self.assertEqual(m["display"], "standalone")
        self.assertIn("maskable", [i["purpose"] for i in m["icons"]])
        self.assertTrue({"192x192", "512x512"} <= {i["sizes"] for i in m["icons"]})
        for i in m["icons"]:
            self.assertTrue((ROOT / "app" / i["src"]).exists(), i["src"])
        p = self.page; p.goto(URL)
        for href in ("manifest.webmanifest", "apple-touch-icon.png", "icon-512.png"):
            self.assertEqual(p.evaluate("u => fetch(u).then(r => r.status)", href), 200)

    def test_service_worker_serves_the_shell_offline_but_never_data(self):
        p = self.login()
        p.wait_for_function("navigator.serviceWorker.ready.then(() => true)")
        p.reload(); p.wait_for_selector("#app:not([hidden])")      # now controlled by the worker
        self.assertTrue(p.evaluate("!!navigator.serviceWorker.controller"))
        self.ctx.set_offline(True)
        p.reload(wait_until="domcontentloaded")
        p.wait_for_selector("#login, #app", state="attached")
        self.assertIn("League", p.title() or "League")
        cached = p.evaluate("caches.keys().then(ks => Promise.all(ks.map(k => caches.open(k).then(c => c.keys())))).then(a => a.flat().map(r => new URL(r.url).pathname))")
        self.assertFalse([u for u in cached if u.startswith("/rest/") or u.startswith("/auth/")], cached)
        self.assertTrue([u for u in cached if u.endswith("app.js")])
        self.ctx.set_offline(False)

    def test_iphone_gets_the_add_to_home_screen_hint(self):
        ctx = self.browser.new_context(viewport={"width": 390, "height": 844}, user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1")
        p = ctx.new_page(); p.route("**/fonts.g*apis.com/**", lambda r: r.abort())
        p.goto(URL); p.fill("#email", "u1@example.test"); p.fill("#password", "test"); p.click("#login button[type=submit]")
        p.wait_for_selector("#app:not([hidden])")
        self.assertTrue(p.is_visible("#installBtn")); self.assertFalse(p.is_visible("#installHelp"))
        p.click("#installBtn"); self.assertIn("Add to Home Screen", p.inner_text("#installHelp"))
        ctx.close()

    def test_desktop_chrome_without_install_event_hides_the_button(self):
        self.login(); self.assertFalse(self.page.is_visible("#installBtn"))


if __name__ == "__main__":
    unittest.main()
