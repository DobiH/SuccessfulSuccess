"""The weekly report (lab 5): week arithmetic, the CSV, and the builder's event shapes."""

import csv
import io
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.db import SessionFactory
from app.models import Meeting, Participant, User
from app.reports import lambda_handler
from app.reports.weekly import (
    build_weekly_report_async,
    parse_week,
    previous_iso_week,
    report_key,
    week_bounds,
)

KYIV = ZoneInfo("Europe/Kyiv")


def test_week_bounds_are_monday_to_monday_in_kyiv() -> None:
    start, end = week_bounds("2026-W40", KYIV)
    assert start == datetime(2026, 9, 28, tzinfo=KYIV)
    assert end == datetime(2026, 10, 5, tzinfo=KYIV)


@pytest.mark.parametrize("bad", ["2026-40", "2026-W5", "W40", "", "2026-W54", "2026-W00"])
def test_parse_week_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_week(bad)


def test_previous_week_uses_the_app_timezone() -> None:
    # Sunday 22:30 UTC is already Monday 01:30 in Kyiv: "last week" is the one just ended.
    assert previous_iso_week(datetime(2026, 10, 11, 22, 30, tzinfo=UTC), KYIV) == "2026-W41"
    # Across a year boundary: Monday 2027-01-04 is 2027-W01, so last week is 2026-W53.
    assert previous_iso_week(datetime(2027, 1, 4, 7, 0, tzinfo=KYIV), KYIV) == "2026-W53"


def test_report_key_is_deterministic() -> None:
    assert report_key("2026-W40") == report_key("2026-W40") == "reports/2026-W40.csv"


def _meeting(owner: str | None, name: str, start: datetime, minutes: int, people: int) -> Meeting:
    from datetime import timedelta

    return Meeting(
        owner_id=owner,
        name=name,
        starts_at=start,
        ends_at=start + timedelta(minutes=minutes),
        participants=[Participant(name=f"P{i}", position=i) for i in range(people)],
    )


def _rows(body: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))


async def test_report_counts_the_week_and_compares_with_the_previous_one(session) -> None:
    session.add_all([User(id="u1", email="olena@example.com"), User(id="u2", name="Taras")])
    await session.commit()

    def at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, month, day, hour, minute, tzinfo=KYIV)

    session.add_all(
        [
            _meeting("u1", "Planning", at(9, 28, 10), 90, 3),  # Monday of 2026-W40
            _meeting("u1", "Standup", at(9, 28, 9), 15, 5),
            _meeting("u2", "Демо", at(9, 30, 14), 120, 2),
            _meeting(None, "Orphan", at(9, 29, 11), 30, 0),
            # Sunday 23:30 in Kyiv belongs to W40 even though it is Sunday 20:30 UTC.
            _meeting("u2", "Late", at(10, 4, 23, 30), 20, 1),
            # Previous week (W39) and next week (W41): only the first is counted, as "last week".
            _meeting("u1", "Old", at(9, 22, 10), 60, 1),
            _meeting("u1", "Next", at(10, 5, 0), 60, 1),
        ]
    )
    await session.commit()

    rows = _rows(await build_weekly_report_async("2026-W40", SessionFactory, KYIV))

    assert rows[0] == ["Weekly meetings report", "2026-W40"]
    assert rows[1] == ["Period", "2026-09-28", "2026-10-04"]
    assert rows[4] == ["Summary", "2026-W40", "2026-W39", "Change"]
    assert rows[5] == ["Meetings", "5", "1", "+4"]
    assert rows[6] == ["Hours", "4.58", "1.0", "+3.58"]

    top = rows[rows.index(["Title", "Start", "Duration (h)", "Participants"]) + 1 :][:5]
    assert [r[0] for r in top] == ["Демо", "Planning", "Orphan", "Late", "Standup"]
    assert top[0] == ["Демо", "2026-09-30 14:00", "2.0", "2"]

    organisers = rows[rows.index(["Organiser", "Meetings", "Hours"]) + 1 :]
    assert organisers == [
        ["Taras", "2", "2.33"],
        ["olena@example.com", "2", "1.75"],
        ["(no owner)", "1", "0.5"],
    ]


async def test_empty_week_still_produces_a_report(session) -> None:
    rows = _rows(await build_weekly_report_async("2026-W40", SessionFactory, KYIV))
    assert rows[5] == ["Meetings", "0", "0", "0"]
    assert rows[6] == ["Hours", "0.0", "0.0", "0"]


def test_builder_reads_both_event_shapes() -> None:
    via, payloads = lambda_handler.parse_event({"week": None, "source": "schedule"})
    assert (via, payloads) == ("direct", [{"week": None, "source": "schedule"}])
    assert lambda_handler.detect_trigger(via, payloads[0]) == "schedule"

    sqs = {
        "Records": [
            {"eventSource": "aws:sqs", "body": json.dumps({"week": "2026-W39", "source": "manual"})}
        ]
    }
    via, payloads = lambda_handler.parse_event(sqs)
    assert via == "sqs" and payloads[0]["week"] == "2026-W39"
    assert lambda_handler.detect_trigger(via, payloads[0]) == "sqs"

    scheduled_via_sqs = {
        "Records": [{"eventSource": "aws:sqs", "body": '{"week": null, "source": "schedule"}'}]
    }
    via, payloads = lambda_handler.parse_event(scheduled_via_sqs)
    assert lambda_handler.detect_trigger(via, payloads[0]) == "schedule"


def test_builder_writes_one_key_per_week_and_can_be_told_to_fail(monkeypatch) -> None:
    puts: list[dict] = []

    class FakeS3:
        def put_object(self, **kwargs):
            puts.append(kwargs)

    monkeypatch.setattr(lambda_handler, "_s3", FakeS3())
    monkeypatch.setattr(lambda_handler, "build_weekly_report", lambda week: b"csv")
    monkeypatch.setenv("REPORTS_BUCKET", "bucket")
    monkeypatch.setenv("FAIL_WEEK", "2026-W30")

    event = {"Records": [{"eventSource": "aws:sqs", "body": '{"week": "2026-W39"}'}]}
    lambda_handler.handler(event, None)
    lambda_handler.handler(event, None)
    assert [p["Key"] for p in puts] == ["reports/2026-W39.csv", "reports/2026-W39.csv"]

    with pytest.raises(RuntimeError):
        lambda_handler.handler({"week": "2026-W30"}, None)
