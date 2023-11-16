import argparse
from email.message import EmailMessage
import json
import kubernetes.client as k8s_client
import kubernetes.config as k8s_config
import logging
import os
import prometheus_client
import smtplib
import time


__version__ = '0.1.2'


METADATA_PREFIX = 'k8s-emailer.hpc.nyu.edu/'

LABEL_MODE = METADATA_PREFIX + 'mode'
ANNOTATION_EMAIL = METADATA_PREFIX + 'addresses'
ANNOTATION_LAST_NOTIFIED = METADATA_PREFIX + 'last-notified'


logger = logging.getLogger('k8s_emailer')


PROM_BAD_ANNOTATIONS = prometheus_client.Gauge(
    'bad_annotations',
    "Number of jobs with incorrect annotations/labels",
    ['namespace'],
)

PROM_ANNOTATED = prometheus_client.Gauge(
    'annotated',
    "Number of jobs found with email annotations",
    ['namespace'],
)

PROM_EMAILS = prometheus_client.Counter(
    'emails',
    "Number of emails sent",
    ['namespace'],
)

PROM_SEND_ERRORS = prometheus_client.Counter(
    'email_errors',
    "Number of errors sending emails",
)


if 'FULL_SYNC_INTERVAL' in os.environ:
    FULL_SYNC_INTERVAL = int(os.environ['FULL_SYNC_INTERVAL'], 10)
else:
    FULL_SYNC_INTERVAL = 120


class Emailer(object):
    def __init__(self):
        self.subject_template = os.environ.get(
            'EMAIL_SUBJECT_TEMPLATE',
            "[{tag}] {status}: {name}",
        )
        self.tag = os.environ.get('EMAIL_TAG', 'Kubernetes')

        if os.environ.get('EMAIL_SSL', '0') not in ('0', 'no', 'false', 'off'):
            self.cls = smtplib.SMTP_SSL
            default_port = 465
        else:
            self.cls = smtplib.SMTP
            default_port = 587

        self.host = os.environ.get('EMAIL_HOST')
        assert self.host, "EMAIL_HOST is not set"

        if 'EMAIL_PORT' in os.environ:
            self.port = int(os.environ['EMAIL_PORT'], 10)
        else:
            self.port = default_port

        self.from_address = os.environ.get('EMAIL_FROM')
        assert self.from_address, "EMAIL_FROM is not set"

        if 'EMAIL_USERNAME' in os.environ or 'EMAIL_PASSWORD' in os.environ:
            self.credentials = (
                os.environ['EMAIL_USERNAME'],
                os.environ['EMAIL_PASSWORD'],
            )
        else:
            self.credentials = None

    def send(self, addresses, message, ns, name):
        fullname = ns + '/' + name
        subject = (
            self.subject_template
            .replace('{tag}', self.tag)
            .replace('{status}', message)
            .replace('{name}', fullname)
        )
        body = f"{message}: {fullname}"

        with self.cls(self.host, self.port) as smtp:
            if self.credentials is not None:
                smtp.login(*self.credentials)

            for address in addresses:
                msg = EmailMessage()
                msg['Subject'] = subject
                msg['From'] = self.from_address
                msg['To'] = address
                msg.set_content(body)

                smtp.send_message(msg)

        PROM_EMAILS.labels(ns).inc(len(addresses))


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        'ceph-backup',
        description="Backup up Ceph volumes on a Kubernetes cluster",
    )
    parser.add_argument('--kubeconfig', nargs=1)
    parser.add_argument('--cleanup-only', action='store_true', default=False)
    args = parser.parse_args()

    prometheus_client.start_http_server(8080)

    if args.kubeconfig:
        logger.info("Using specified config file")
        k8s_config.load_kube_config(args.kubeconfig[0])
    else:
        logger.info("Using in-cluster config")
        k8s_config.load_incluster_config()

    api = k8s_client.ApiClient()

    emailer = Emailer()

    # TODO: Use the watch API
    while True:
        do_sync(api, emailer)
        time.sleep(FULL_SYNC_INTERVAL)


def do_sync(api, emailer):
    bad_annotations = {}
    annotations = {}

    batchv1 = k8s_client.BatchV1Api(api)

    # Find jobs with the label
    jobs = batchv1.list_job_for_all_namespaces(label_selector=LABEL_MODE).items
    for job in jobs:
        # Read metadata
        meta = job.metadata
        ns = meta.namespace
        mode = meta.labels[LABEL_MODE]
        addresses_annotation = meta.annotations.get(ANNOTATION_EMAIL, '')
        addresses = set()
        for address in addresses_annotation.split(','):
            address = address.strip()
            if address:
                addresses.add(address)
        addresses = sorted(addresses)

        annotations[ns] = annotations.get(ns, 0) + 1

        # Select what to notify based on mode
        if mode == 'failure':
            send_on_failure = True
            send_on_success = False
            send_on_retry = False
        elif mode == 'complete':
            send_on_failure = True
            send_on_success = True
            send_on_retry = False
        else:
            # mode=all is also the default
            if mode != 'all':
                bad_annotations[ns] = bad_annotations.get(ns, 0) + 1
            send_on_failure = True
            send_on_success = True
            send_on_retry = True

        # Determine job status
        is_success = is_failure = False
        if any(
            condition.type.lower() == 'failed'
            and condition.status.lower() == 'true'
            for condition in job.status.conditions or ()
        ):
            is_failure = True
        elif job.status.completion_time:
            is_success = True
        retries = job.status.failed or 0

        # Read last notified state
        last_annotation = {}
        if ANNOTATION_LAST_NOTIFIED in meta.annotations:
            last_annotation = meta.annotations[ANNOTATION_LAST_NOTIFIED]
            try:
                last_annotation = json.loads(last_annotation)
            except json.JSONDecodeError:
                pass

        # Build email
        message = None
        if (
            is_failure
            and not last_annotation.get('is_failure', False)
            and send_on_failure
        ):
            message = "Job failed"
        elif (
            is_success
            and not last_annotation.get('is_success', False)
            and send_on_success
        ):
            message = "Job succeeded"
        elif (
            retries != 0
            and retries != last_annotation.get('retries', 0)
            and send_on_retry
        ):
            message = "Job was retried"

        if message:
            logger.info(
                "Sending email to %d addresses: %s %s/%s",
                len(addresses),
                message,
                ns,
                meta.name,
            )
            try:
                emailer.send(
                    addresses,
                    message,
                    ns,
                    meta.name,
                )
            except Exception:
                logger.exception(
                    "Error sending emails for %s/%s",
                    ns,
                    meta.name,
                )
                PROM_SEND_ERRORS.inc()
            else:
                # Update last notification annotation
                last_annotation = {
                    'is_failure': is_failure,
                    'is_success': is_success,
                    'retries': retries,
                }
                batchv1.patch_namespaced_job(
                    meta.name,
                    ns,
                    {
                        'metadata': {
                            'annotations': {
                                ANNOTATION_LAST_NOTIFIED: json.dumps(
                                    last_annotation,
                                    separators=(',', ':'),
                                ),
                            },
                        },
                    },
                )

    PROM_ANNOTATED.clear()
    for count_ns, count in annotations.items():
        PROM_ANNOTATED.labels(count_ns).set(count)

        # This makes sure the time series exist, enabling rate() to work
        PROM_EMAILS.inc(0)

    PROM_BAD_ANNOTATIONS.clear()
    for count_ns, count in bad_annotations.items():
        PROM_BAD_ANNOTATIONS.labels(count_ns).set(count)
