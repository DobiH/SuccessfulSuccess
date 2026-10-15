"""Lambda entry point of the report builder (infra/reports.yml).

Same container image as the API, different handler:
    ImageConfig: { Command: ["app.reports.lambda_handler.handler"] }

It accepts two event shapes:
  * a direct invocation, e.g. from EventBridge Scheduler targeting the function
    or `aws lambda invoke`:            {"week": null, "source": "schedule"}
  * an SQS event, whose records carry that same JSON in their body.

Every invocation logs one line that says which event started it. `trigger` is
who asked for the report (schedule, sqs, direct); `via` is how the request
arrived. With the queue in place the schedule's request arrives through SQS, so
its line reads `trigger=schedule via=sqs`, and `make report-now` reads
`trigger=sqs via=sqs`.

The builder never sends email and never knows the mailer exists: it only writes
reports/<week>.csv. S3 announces the new file; whoever listens reacts.
"""

import json
import logging
import os
from datetime import UTC, datetime

from app.config import settings
from app.reports.weekly import build_weekly_report, previous_iso_week, report_key

log = logging.getLogger("reports.builder")
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3 = None


def _s3_client():
    global _s3
    if _s3 is None:
        import boto3  # ships with the Lambda base image; not needed to run the report locally

        _s3 = boto3.client("s3")
    return _s3


def parse_event(event: dict) -> tuple[str, list[dict]]:
    """Return (how the event arrived, the request payloads it carries)."""
    records = event.get("Records") or []
    if records and records[0].get("eventSource") == "aws:sqs":
        return "sqs", [json.loads(record["body"] or "{}") for record in records]
    return "direct", [event]


def detect_trigger(via: str, payload: dict) -> str:
    """Who started this build: the schedule, an SQS message, or a direct invoke."""
    if payload.get("source") == "schedule":
        return "schedule"
    return via


def build_and_store(week: str) -> str:
    """Build one week's report and write it to S3. Idempotent: same week, same key."""
    key = report_key(week)
    if week == os.environ.get("FAIL_WEEK"):
        # Lab step 7: break one week on purpose and watch its message reach the DLQ.
        raise RuntimeError(f"FAIL_WEEK is set to {week}: refusing to build it")
    body = build_weekly_report(week)
    _s3_client().put_object(
        Bucket=os.environ["REPORTS_BUCKET"],
        Key=key,
        Body=body,
        ContentType="text/csv; charset=utf-8",
    )
    return key


def handler(event, context):
    via, payloads = parse_event(event)
    results = []
    for payload in payloads:
        trigger = detect_trigger(via, payload)
        week = payload.get("week") or previous_iso_week(datetime.now(UTC), settings.tz)
        log.info(
            "report-builder triggered trigger=%s via=%s source=%s week=%s",
            trigger,
            via,
            payload.get("source"),
            week,
        )
        # Raising lets SQS deliver the message again; after three receives it
        # moves to the dead-letter queue instead of disappearing.
        key = build_and_store(week)
        log.info("report-builder stored s3://%s/%s", os.environ["REPORTS_BUCKET"], key)
        results.append({"week": week, "key": key})
    return {"reports": results}
