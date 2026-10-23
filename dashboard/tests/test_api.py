import os
import unittest

from fastapi.testclient import TestClient

from app import main
from app.store import JsonlStore

FX = os.path.join(os.path.dirname(__file__), "fixtures")


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        main.use_store(JsonlStore(FX))
        cls.c = TestClient(main.app)

    def test_health(self):
        self.assertEqual(self.c.get("/api/health").json(), {"ok": True, "store": "jsonl", "traces": 3})

    def test_requests_are_logged_with_timing(self):
        with self.assertLogs("dashboard", level="DEBUG") as cm:
            self.c.get("/api/health")
        self.assertTrue(any("GET /api/health 200" in line and "ms" in line for line in cm.output), cm.output)

    def test_database_url_is_logged_without_password(self):
        os.environ["DATABASE_URL"] = "postgresql://trace:secret@db.example:5432/trace"
        try:
            text = main._describe(type("S", (), {"name": "trace_v1", "run_id": lambda self: "abc"})())
        finally:
            del os.environ["DATABASE_URL"]
        self.assertNotIn("secret", text)
        self.assertIn("db.example", text)
        self.assertIn("run=abc", text)

    def test_services_shape(self):
        s = self.c.get("/api/services").json()[0]
        self.assertEqual(set(s), {"service", "span_count", "trace_count", "error_rate", "p50_ms", "p99_ms"})

    def test_operations(self):
        self.assertEqual(self.c.get("/api/operations", params={"service": "redis"}).json(), ["HGETALL"])

    def test_search_and_validation(self):
        r = self.c.get("/api/traces", params={"min_duration_ms": 100, "sort": "duration_desc"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([t["trace_id"] for t in r.json()["items"]], ["t-slow"])
        self.assertEqual(self.c.get("/api/traces", params={"status": "bogus"}).status_code, 422)
        self.assertEqual(self.c.get("/api/traces", params={"limit": 0}).status_code, 422)
        self.assertEqual(self.c.get("/api/traces", params={"limit": 10000}).status_code, 422)
        self.assertEqual(self.c.get("/api/traces", params={"sort": "sideways"}).status_code, 422)

    def test_trace_detail(self):
        r = self.c.get("/api/traces/t-slow")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["summary"]["span_count"], 4)
        self.assertEqual(len(d["spans"]), 4)
        self.assertEqual(self.c.get("/api/traces/missing").status_code, 404)

    def test_rankings(self):
        ws = self.c.get("/api/rankings", params={"limit": 1}).json()
        self.assertEqual(ws[0]["rankings"][0]["service"], "paymentservice")
        self.assertEqual(len(ws[0]["rankings"]), 1)

    def test_upload_not_configured(self):
        os.environ.pop("RAW_BUCKET", None)
        self.assertEqual(self.c.post("/api/uploads", json={"filename": "a.json"}).status_code, 503)

    def test_upload_ticket(self):
        os.environ["RAW_BUCKET"] = "sta-raw-dev"
        real = main._presign_put
        main._presign_put = lambda bucket, key, ct, exp: f"https://{bucket}.s3.amazonaws.com/{key}?sig=1"
        try:
            r = self.c.post("/api/uploads", json={"filename": "my trace (1).json", "content_type": "application/json"})
            self.assertEqual(r.status_code, 200, r.text)
            t = r.json()
            self.assertTrue(t["key"].startswith("uploads/"))
            self.assertTrue(t["key"].endswith("-my_trace_1_.json"))
            self.assertEqual(t["headers"], {"Content-Type": "application/json"})
            self.assertTrue(t["url"].startswith("https://sta-raw-dev.s3.amazonaws.com/uploads/"))
            bad = self.c.post("/api/uploads", json={"filename": "x.exe", "content_type": "application/x-msdownload"})
            self.assertEqual(bad.status_code, 400)
        finally:
            main._presign_put = real
            os.environ.pop("RAW_BUCKET", None)

    def test_compare(self):
        c = self.c.get("/api/compare", params={"threshold_ms": 100}).json()
        self.assertEqual((c["slow_traces"], c["normal_traces"]), (1, 2))
        self.assertEqual(c["services"][0]["service"], "paymentservice")
        self.assertEqual(self.c.get("/api/compare", params={"endpoint": "GET /cart"}).json()["normal_traces"], 1)

    def test_flame(self):
        f = self.c.get("/api/flame", params={"endpoint": "POST /checkout"}).json()
        self.assertEqual(f["count"], 1)
        self.assertEqual(f["children"][0]["name"], "frontend POST /checkout")
        self.assertEqual(self.c.get("/api/flame", params={"subset": "sideways"}).status_code, 422)

    def test_pipeline(self):
        p = self.c.get("/api/pipeline").json()
        self.assertEqual(p["traces"], 3)
        self.assertEqual(p["incomplete_traces"], 1)
        self.assertEqual(p["spans_with_stamps"], 0)      # fixture spans carry no ingest stamps

    def test_index_page(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Trace Dashboard", r.text)


if __name__ == "__main__":
    unittest.main()
