import io
import json
import unittest

from trace_reconstruction.lambda_handler import handler
from tests.helpers import fixture
from tests.test_worker import FakeStore


class Store(FakeStore):
    def __init__(self):
        super().__init__()
        self.ingested = []
        self.created = []

    def create_run(self, run_id, **kwargs):
        self.created.append(run_id)

    def ingest_many(self, records):
        self.ingested.extend(records)


class S3:
    def get_object(self, **kwargs):
        return {'Body': io.BytesIO(('\n'.join(json.dumps(s) for s in fixture())).encode())}


def message(mid, key):
    return {'messageId': mid, 'body': json.dumps({'Records': [
        {'s3': {'bucket': {'name': 'test'}, 'object': {'key': key}}}]})}


class LambdaTests(unittest.TestCase):
    def test_consumes_normalized_records_and_reports_failed_messages(self):
        store = Store()
        response = handler({'Records': [message('good', 'normalized/spans.jsonl'),
                                        message('bad', 'uploads/raw.csv')]}, s3=S3(), store=store)
        self.assertEqual(len(store.ingested), 3)
        self.assertEqual(len(store.created), 1)
        self.assertEqual(response['batchItemFailures'], [{'itemIdentifier': 'bad'}])

    def test_scheduled_poll_settles_without_new_upload(self):
        response = handler({'source': 'aws.events'}, store=Store())
        self.assertEqual(response['analyzed'], 1)

    def test_invalid_body_is_retried(self):
        response = handler({'Records': [{'messageId': 'bad', 'body': 'not json'}]}, s3=S3(), store=Store())
        self.assertEqual(response['batchItemFailures'], [{'itemIdentifier': 'bad'}])

    def test_invalid_canonical_message_does_not_discard_valid_message(self):
        class MixedS3(S3):
            def get_object(self, **kwargs):
                if kwargs['Key'].endswith('invalid.jsonl'):
                    return {'Body': io.BytesIO(b'{"schema_version":"invalid"}\n')}
                return super().get_object(**kwargs)
        store = Store()
        response = handler({'Records': [message('invalid', 'normalized/invalid.jsonl'),
                                        message('valid', 'normalized/spans.jsonl')]}, s3=MixedS3(), store=store)
        self.assertEqual(response['batchItemFailures'], [{'itemIdentifier': 'invalid'}])
        self.assertEqual(len(store.ingested), 3)
