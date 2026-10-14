import unittest

from traceingest import rcaeval
from traceingest.spans import STATUS_ERROR, STATUS_OK, STATUS_UNSET

from .helpers import fx, load


class RCAEvalTests(unittest.TestCase):
    def setUp(self):
        self.spans = {s.service: s for s in load("rcaeval_small.csv")}

    def test_root_has_no_parent(self):
        self.assertIsNone(self.spans["frontend"].parent_span_id)

    def test_status_codes(self):
        self.assertEqual(self.spans["paymentservice"].status, STATUS_ERROR)          # gRPC 13
        self.assertEqual(self.spans["checkoutservice"].status, STATUS_OK)            # gRPC 0
        self.assertEqual(self.spans["cartservice"].status, STATUS_UNSET)             # blank
        self.assertEqual(self.spans["productcatalogservice"].status, STATUS_OK)

    def test_http_5xx_is_error(self):
        self.assertEqual(rcaeval.status_from("503", None), STATUS_ERROR)
        self.assertEqual(rcaeval.status_from("404", None), STATUS_OK)

    def test_method_name_preferred(self):
        s = self.spans["paymentservice"]
        self.assertEqual(s.operation, "hipstershop.PaymentService/Charge")
        self.assertEqual(s.attributes["rcaeval.operation_name"], "Charge")

    def test_microseconds(self):
        s = self.spans["checkoutservice"]
        self.assertEqual((s.start_us, s.duration_us), (1_712_000_000_010_000, 230_000))

    def test_time_unit_heuristics(self):
        self.assertEqual(rcaeval.to_us(1_712_000_000_010), 1_712_000_000_010_000)        # ms
        self.assertEqual(rcaeval.to_us(1_712_000_000_010_000_000), 1_712_000_000_010_000)  # ns

    def test_case_label(self):
        spans, label = rcaeval.read_case(fx("re2ob_checkoutservice_delay_1"))
        self.assertEqual(len(spans), 6)
        self.assertEqual((label.benchmark, label.root_cause_service, label.fault, label.instance),
                         ("re2ob", "checkoutservice", "delay", "1"))
        self.assertEqual(label.inject_time_s, 1712000000)

    def test_case_name_with_underscores_in_service(self):
        label = rcaeval.parse_case_name("re2tt_ts_auth_service_cpu_3")
        self.assertEqual(label.root_cause_service, "ts_auth_service")


if __name__ == "__main__":
    unittest.main()
