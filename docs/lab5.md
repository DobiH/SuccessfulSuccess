# Lab 5 — A weekly report, built by events

```
EventBridge Scheduler ──┐  {"week": null, "source": "schedule"}       (command)
make report-now ────────┴─► SQS report-requests ──(3 failures)──► report-requests-dlq
                                 │ batch of 1                         └─ CloudWatch alarm
                                 ▼
                       Lambda report-builder  (VPC, backend image, other handler)
                         │ :5432 Aurora            │ PutObject via S3 gateway endpoint
                         ▼                         ▼
                                        S3 reports/2026-W40.csv
                                                   │ ObjectCreated            (event)
                                                   ▼
                                     Lambda report-mailer (outside the VPC) ──► SES ──► inbox
```

| What | Where |
|---|---|
| The report (no AWS in it) | `backend/app/reports/weekly.py` — `build_weekly_report(week) -> bytes` |
| Builder handler | `backend/app/reports/lambda_handler.py` |
| Mailer handler | inline in `infra/reports.yml` (`MailerFunction`) |
| Infrastructure | `infra/reports.yml` (one stack, tagged `PROJECT_NAME`) |
| Make targets | `report-local`, `aws-deploy-reports`, `report-now`, `report-direct`, `report-list`, `report-triggers`, `report-logs`, `report-queues` |
| Tests | `backend/tests/test_weekly_report.py` |

## The report

For one ISO week in `APP_TIMEZONE` (a meeting belongs to the week it **starts** in):
number of meetings and total hours, the same for the previous week and the change,
the five longest meetings (title, start, duration, participants) and totals per
organiser (the meeting's owner).

- The week is always a parameter; only the Lambda handler decides "last week".
- `reports/<week>.csv` is the only name a week can have, so building it twice
  overwrites one file. For the same data the bytes are identical, too.

## Running it

`.env` (on top of the lab 4 settings):

```
REPORTS_SENDER=you@gmail.com          # a verified SES identity
REPORTS_RECIPIENTS=you@gmail.com      # optional, defaults to the sender
REPORTS_DOMAIN=                       # optional, creates the SES domain identity
```

```
make report-local WEEK=2026-W41         # step 1: the CSV from the compose database
make aws-deploy-reports                 # push the image, deploy infra/reports.yml
make report-now WEEK=2026-W41           # on-demand report through the queue
make aws-reports-stack REPORTS_SCHEDULE="rate(5 minutes)"   # one scheduled run...
make aws-reports-stack                                       # ...and back to Monday 07:00
make report-list                        # submission item 3
make report-triggers SINCE=3h           # submission item 4
make aws-reports-stack REPORTS_FAIL_WEEK=2026-W30 && make report-now WEEK=2026-W30
make report-queues                      # after ~20 min: waiting=1 in the DLQ
make aws-reports-stack                  # "fix the code", then SQS console → DLQ → Start DLQ redrive
```

Step 3 (schedule invokes the builder directly, no queue):
`make aws-reports-stack REPORTS_SCHEDULE_TARGET=function`, or `make report-direct`.

Log lines: `trigger` is who asked for the report, `via` is how the request arrived.
Once the queue is in place, the schedule's request arrives through SQS:

```
report-builder triggered trigger=schedule via=sqs source=schedule week=2026-W41
report-builder triggered trigger=sqs via=sqs source=manual week=2026-W40
report-mailer triggered trigger=s3 event=ObjectCreated:Put key=s3://…/reports/2026-W41.csv
```

## Step 7 — observation

Sending the same week twice gives **one file and two emails**. The builder's write
is idempotent: the second `PutObject` overwrites `reports/2026-W40.csv`. But every
`PutObject` is a new `ObjectCreated` event, even with identical content, and
sending an email is not idempotent. So the mailer, which only reacts to events,
sends again.

## Step 7 — answers

**1. The mailer got the same ObjectCreated event twice. Where would you store "this report was already sent", and what would the key be?**

In a small DynamoDB table (on-demand, effectively free at this volume), written
with a conditional `PutItem` (`attribute_not_exists(pk)`) **before** calling SES.
If the condition fails, the report was already handled and the mailer stops.

The key: `pk = <bucket>/<key>#<ETag>`, for example
`successfulsuccess-reports-123/reports/2026-W40.csv#9b2cf5…`, with the
`sequencer` from the event as a tie-breaker if versioning is on. The ETag is what
makes it right:

- the same event delivered twice, or the same report rebuilt from unchanged data,
  has the same ETag (the bytes are identical) → no second email;
- a report rebuilt after someone edited last week's meetings has new bytes and a
  new ETag → the corrected report is sent.

A key of just the week would suppress corrected reports. A key of the request id
would not catch S3 delivering the same event twice.

Writing the marker before sending gives at-most-once delivery: if SES fails after
the write, the email is lost. To get both, store `status=sending` with a TTL,
switch it to `sent` after SES returns, and let a retry proceed only when it finds
an expired `sending`. Not S3: an object tag or a marker file is not an atomic
"check and set", and a marker under `reports/` would trigger the mailer itself.

**2. The builder in the VPC could not reach SES. Three ways to fix it, with their monthly cost.** (us-east-1 list prices, approximate.)

| Option | Fixed cost / month | Per use |
|---|---|---|
| NAT gateway (one AZ) | $0.045/h ≈ **$33**; ≈ $66 with one per AZ for availability | $0.045 per GB processed |
| Interface VPC endpoint for SES | $0.01/h per AZ ≈ **$7.30 per AZ**, ≈ $15 for two | $0.01 per GB |
| The split we built (mailer outside the VPC, connected by an S3 event) | **$0** | a free-tier Lambda invocation per email |

SES itself ($0.10 per 1,000 emails) is the same in all three.

- **Ten more emails a week:** the split. Every other option is a fixed monthly
  bill for a few kilobytes of traffic, and the split also keeps the database-facing
  function without any internet path.
- **A million a week:** still the split. Network is no longer the question at that
  volume. SES is ~$100/week (~$430/month) and dominates every option. What changes
  is the mailer: it needs a queue in front of it so it can respect the SES sending
  rate, retry throttled sends, and keep a dead-letter queue, plus SES production
  access, bounce/complaint handling and an unsubscribe path. A NAT gateway only
  becomes the answer if other code in the VPC needs the internet too; then its $33
  is shared.

**3. A presigned URL signed with the role's temporary credentials expires with them. How would you send a link that works for a week?**

A SigV4 presigned URL is only valid while both its own expiry (7 days at most)
and the credentials that signed it are valid. A Lambda role's credentials last
hours, so its URLs die early whatever `ExpiresIn` says. Three ways out, best last:

1. **Sign with long-term credentials:** a dedicated IAM user whose only permission
   is `s3:GetObject` on `reports/*`, its access key in Secrets Manager, and
   `ExpiresIn=604800`. It works, for exactly seven days, but it is a long-lived key
   to rotate, and a forwarded link works for anyone.
2. **CloudFront signed URL:** a distribution with origin access control in front of
   the bucket. The URL is signed with a CloudFront key pair, and its expiry is
   whatever we choose, independent of IAM sessions.
3. **A link to our own API**, e.g. `GET /api/v1/reports/2026-W40`. The API checks
   the Cognito token, then answers `302` with a fresh presigned URL valid for 5
   minutes. The emailed link never expires, can be revoked by removing access, and
   only works for people allowed to see it. Presigning is a local computation, so
   the API in the VPC without NAT can do it. I would choose this one.

## What we discuss in class — short notes

- **Command vs event:** the schedule's `{"week": null, "source": "schedule"}` and
  `make report-now`'s message are commands (one addressee, the builder). The S3
  `ObjectCreated` for `reports/2026-W40.csv` is the event: the builder does not
  know who listens.
- **Idempotent steps:** building the CSV and writing `reports/<week>.csv` are
  idempotent. Sending the email is not.
- **Slack notification:** add a second subscriber to the bucket's `reports/`
  events (or fan out through SNS/EventBridge); the builder does not change.
- **Edited at 07:01:** the report is a snapshot as of 07:00 and stays that way
  until rebuilt (`make report-now WEEK=…`). For a weekly report that is acceptable
  eventual consistency. "Recomputed within 5 minutes of a calendar change" would
  instead need the change itself to emit an event that triggers recomputation.
- **Payments: Redis or RabbitMQ?** RabbitMQ (or SQS). Acks plus durable queues
  mean a worker crash redelivers the message; with a Redis list, a popped message
  is gone if the worker dies.
- **Renaming an RQ task:** queued jobs reference the old dotted path and fail
  after the deploy. Messages between services should be data (a named event with
  a versioned schema), not serialized calls into one codebase.
- **Two Celery beats:** every task is scheduled twice. EventBridge Scheduler is a
  managed single scheduler, so there is no second copy to start.
- **Visibility timeout shorter than the function timeout:** the message becomes
  visible while the first builder is still working, a second builder starts on the
  same week, and if both fail the receive count climbs twice as fast toward the DLQ.
- **DLQ on Sunday night:** `DlqNotEmptyAlarm` (in the template) fires when the DLQ
  holds more than zero messages. Give it an SNS topic with an email subscription;
  the on-call person redrives after fixing the cause.
- **When it starts to cost money:** Scheduler, SQS, S3 events and Lambda stay in
  the free tier for a very long time. SES ($0.10 / 1,000 emails) is the first
  line item to grow with recipients, while Aurora waking up is the larger fixed
  cost (that bill depends on Lab 4's capacity settings).
