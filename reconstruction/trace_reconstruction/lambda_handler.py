"""SQS/S3 normalized-file consumer; scheduled invocations settle quiet traces."""
import json
import logging
import os
from urllib.parse import unquote_plus

from .store import PostgresStore
from .worker import ReconstructionWorker

log = logging.getLogger(__name__)


def handler(event, context=None, *, s3=None, store=None):
    owned = store is None
    if owned:
        from .__main__ import _validator
        store = PostgresStore.connect(os.environ['DATABASE_URL'], validator=_validator())
    try:
        worker = ReconstructionWorker(store, configuration={
            'assume_synchronous': os.environ.get('ASSUME_SYNCHRONOUS', '').lower() == 'true'})
        if event.get('source') == 'aws.events':
            return vars(worker.poll_once())
        if s3 is None:
            import boto3
            s3 = boto3.client('s3')
        failures = []
        for message in event.get('Records', []):
            try:
                notification = json.loads(message['body'])
                for item in notification.get('Records', []):
                    key = unquote_plus(item['s3']['object']['key'])
                    if not key.startswith(os.environ.get('NORMALIZED_PREFIX', 'normalized/')):
                        raise ValueError('expected a normalized object')
                    request = {'Bucket': item['s3']['bucket']['name'], 'Key': key}
                    if item['s3']['object'].get('versionId'):
                        request['VersionId'] = item['s3']['object']['versionId']
                    body = s3.get_object(**request)['Body']
                    try:
                        records = [json.loads(line) for line in body.read().decode('utf-8').splitlines() if line.strip()]
                    finally:
                        body.close()
                    runs = {}
                    for record in records:
                        runs.setdefault(record['run_id'], record)
                    for run_id, record in runs.items():
                        store.create_run(run_id, application_name=os.environ.get('APPLICATION_NAME', 'uploaded-traces'),
                                         environment=os.environ.get('ENVIRONMENT', 'aws'),
                                         source_format=record['source']['format'], identity_kind=record['identity_kind'])
                    store.ingest_many(records)
            except Exception:
                # Avoid logging payloads or connection strings. SQS retries retain the original data.
                log.warning('Normalized message failed: %s', message.get('messageId'))
                failures.append({'itemIdentifier': message['messageId']})
        # Quiet settlement is also scheduled: the last upload may have no later message.
        worker.poll_once()
        return {'batchItemFailures': failures}
    finally:
        if owned:
            store.close()
