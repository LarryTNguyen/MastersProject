"""Browser smoke test: every tab renders on the demo data with no JS errors.
Needs playwright; skipped otherwise.
"""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # pragma: no cover
    sync_playwright = None

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class UiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        subprocess.check_call([sys.executable, "tools/make_demo_data.py", cls.tmp, "--now"], cwd=HERE,
                              stdout=subprocess.DEVNULL)
        cls.port = free_port()
        cls.server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(cls.port)],
                                      cwd=HERE, env={**os.environ, "DATA_DIR": cls.tmp},
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.2)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.server.terminate()

    def page(self):
        pg = self.browser.new_page(viewport={"width": 1280, "height": 900})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        return pg, errors

    def test_every_view(self):
        pg, errors = self.page()
        base = f"http://127.0.0.1:{self.port}/"
        pg.goto(base)
        pg.wait_for_selector("#services-body tr.row")
        self.assertIn("200 traces", pg.inner_text("#status"))
        for view in ("traces", "rankings", "flame", "pipeline", "upload"):
            pg.click(f"nav button[data-view={view}]")
            pg.wait_for_timeout(600)
            self.assertFalse(pg.is_hidden(f"#view-{view}"), view)
        self.assertEqual(errors, [])

    def test_trace_detail_and_deep_link(self):
        pg, errors = self.page()
        pg.goto(f"http://127.0.0.1:{self.port}/#traces")
        pg.wait_for_selector("#traces-body tr.row")
        pg.click("#traces-body tr.row")
        pg.wait_for_selector("#trace-detail:not([hidden])")
        self.assertGreater(len(pg.query_selector_all(".wf-row")), 5)
        link = pg.url
        self.assertIn("#trace=", link)
        pg2, errors2 = self.page()
        pg2.goto(link)
        pg2.wait_for_selector("#trace-detail:not([hidden])")
        self.assertEqual(pg2.inner_text("#td-id"), pg.inner_text("#td-id"))
        self.assertEqual(errors + errors2, [])

    def test_compare_and_flame_show_payment(self):
        pg, errors = self.page()
        pg.goto(f"http://127.0.0.1:{self.port}/#rankings")
        pg.wait_for_selector("#cp-body tr")
        pg.select_option("#cp-endpoint", "HTTP POST /cart/checkout")
        pg.click("#cp-go")
        pg.wait_for_timeout(800)
        self.assertIn("paymentservice", pg.inner_text("#cp-body tr:first-child"))
        pg.click("nav button[data-view=flame]")
        pg.wait_for_selector(".flame .fb")
        self.assertIn("traces", pg.inner_text(".flame .fr:first-child .fb"))
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
