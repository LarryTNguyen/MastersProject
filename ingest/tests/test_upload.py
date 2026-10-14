import json
import unittest

from traceingest.upload import presign_from_api, upload

from .helpers import FakeResponse, fx


class UploadTests(unittest.TestCase):
    def test_presigned_put(self):
        seen = []

        def opener(req, timeout):
            seen.append(req)
            return FakeResponse(200)

        r = upload(fx("otlp_small.json"), presigned_url="https://bucket.s3.amazonaws.com/x?sig=1", opener=opener)
        self.assertEqual(seen[0].get_method(), "PUT")
        self.assertEqual(seen[0].get_header("Content-type"), "application/json")
        self.assertEqual(r["status"], 200)

    def test_presign_endpoint(self):
        class Resp(FakeResponse):
            def read(self):
                return json.dumps({"url": "https://s3/put", "key": "uploads/a.json"}).encode()

        url, key = presign_from_api("https://api/uploads", "a.json", "application/json",
                                    opener=lambda req, timeout: Resp(200))
        self.assertEqual((url, key), ("https://s3/put", "uploads/a.json"))

    def test_needs_a_destination(self):
        with self.assertRaises(ValueError):
            upload(fx("otlp_small.json"))


if __name__ == "__main__":
    unittest.main()
