"""The weekly meetings report: one ISO week in, one CSV out. No AWS in here.

The week is always a parameter ("2026-W40"); this module never decides which
week is "last week". Whoever calls it does, so an old report can be rebuilt by
asking for its week again. The same week always maps to the same file name
(report_key), so building it twice overwrites one file instead of adding two.

A meeting belongs to the week in which it starts, in APP_TIMEZONE, the same
timezone the "today" page uses.

Run locally against the compose database:
    python -m app.reports.weekly 2026-W40 > 2026-W40.csv
"""

import asyncio
import csv
import io
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.models import Meeting, Participant, User

WEEK_PATTERN = re.compile(r"^(\d{4})-W(\d{2})$")
TOP_N = 5
NO_OWNER = "(no owner)"


def parse_week(week: str) -> tuple[int, int]:
    """'2026-W40' -> (2026, 40). Raises ValueError for anything else, including W54."""
    match = WEEK_PATTERN.match(week or "")
    if not match:
        raise ValueError(f"week must look like 2026-W40, got {week!r}")
    year, number = int(match[1]), int(match[2])
    date.fromisocalendar(year, number, 1)  # rejects weeks the year does not have
    return year, number


def iso_week(day: date) -> str:
    year, number, _ = day.isocalendar()
    return f"{year}-W{number:02d}"


def previous_iso_week(now: datetime, tz: ZoneInfo) -> str:
    """The ISO week before the one `now` falls in, in the given timezone."""
    return iso_week(now.astimezone(tz).date() - timedelta(days=7))


def week_bounds(week: str, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """[Monday 00:00, next Monday 00:00) of that week, in the given timezone."""
    year, number = parse_week(week)
    monday = date.fromisocalendar(year, number, 1)
    start = datetime.combine(monday, datetime.min.time(), tzinfo=tz)
    end = datetime.combine(monday + timedelta(days=7), datetime.min.time(), tzinfo=tz)
    return start, end


def report_key(week: str) -> str:
    """The S3 key of a week's report. Deterministic: one week, one file."""
    parse_week(week)
    return f"reports/{week}.csv"


@dataclass(frozen=True)
class Totals:
    meetings: int
    hours: float


@dataclass(frozen=True)
class LongMeeting:
    title: str
    starts_at: datetime
    hours: float
    participants: int


@dataclass(frozen=True)
class OrganiserTotals:
    organiser: str
    meetings: int
    hours: float


@dataclass(frozen=True)
class WeeklyReport:
    week: str
    previous_week: str
    period_start: date
    period_end: date  # inclusive: the Sunday
    timezone: str
    this_week: Totals
    last_week: Totals
    longest: list[LongMeeting]
    by_organiser: list[OrganiserTotals]


def _hours(seconds: float | None) -> float:
    return round(float(seconds or 0) / 3600, 2)


_DURATION = func.extract("epoch", Meeting.ends_at - Meeting.starts_at)


async def _totals(session: AsyncSession, start: datetime, end: datetime) -> Totals:
    count, seconds = (
        await session.execute(
            select(func.count(Meeting.id), func.coalesce(func.sum(_DURATION), 0)).where(
                Meeting.starts_at >= start, Meeting.starts_at < end
            )
        )
    ).one()
    return Totals(meetings=int(count), hours=_hours(seconds))


async def collect_week(session: AsyncSession, week: str, tz: ZoneInfo) -> WeeklyReport:
    """Query everything the report shows for one ISO week."""
    start, end = week_bounds(week, tz)
    previous = iso_week(start.date() - timedelta(days=7))
    prev_start, prev_end = week_bounds(previous, tz)
    in_week = (Meeting.starts_at >= start, Meeting.starts_at < end)

    participant_count = (
        select(func.count(Participant.id))
        .where(Participant.meeting_id == Meeting.id)
        .correlate(Meeting)
        .scalar_subquery()
    )
    longest_rows = await session.execute(
        select(Meeting.name, Meeting.starts_at, _DURATION, participant_count)
        .where(*in_week)
        .order_by(_DURATION.desc(), Meeting.starts_at.asc(), Meeting.name.asc())
        .limit(TOP_N)
    )

    organiser = func.coalesce(User.email, User.name, Meeting.owner_id, NO_OWNER)
    organiser_rows = await session.execute(
        select(organiser, func.count(Meeting.id), func.sum(_DURATION))
        .select_from(Meeting)
        .outerjoin(User, User.id == Meeting.owner_id)
        .where(*in_week)
        .group_by(organiser)
        .order_by(func.sum(_DURATION).desc(), organiser.asc())
    )

    return WeeklyReport(
        week=week,
        previous_week=previous,
        period_start=start.date(),
        period_end=end.date() - timedelta(days=1),
        timezone=str(tz),
        this_week=await _totals(session, start, end),
        last_week=await _totals(session, prev_start, prev_end),
        longest=[
            LongMeeting(
                title=name,
                starts_at=starts_at.astimezone(tz),
                hours=_hours(seconds),
                participants=int(people),
            )
            for name, starts_at, seconds, people in longest_rows
        ],
        by_organiser=[
            OrganiserTotals(organiser=who, meetings=int(count), hours=_hours(seconds))
            for who, count, seconds in organiser_rows
        ],
    )


def _signed(value: float) -> str:
    text = f"{value:+.2f}".rstrip("0").rstrip(".")
    return "0" if text in ("+0", "-0") else text


def render_csv(report: WeeklyReport) -> bytes:
    """One CSV with four sections separated by blank rows; opens cleanly in Excel."""
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["Weekly meetings report", report.week])
    w.writerow(["Period", report.period_start.isoformat(), report.period_end.isoformat()])
    w.writerow(["Timezone", report.timezone])
    w.writerow([])
    w.writerow(["Summary", report.week, report.previous_week, "Change"])
    this, last = report.this_week, report.last_week
    w.writerow(["Meetings", this.meetings, last.meetings, _signed(this.meetings - last.meetings)])
    w.writerow(["Hours", this.hours, last.hours, _signed(round(this.hours - last.hours, 2))])
    w.writerow([])
    w.writerow([f"Top {TOP_N} longest meetings"])
    w.writerow(["Title", "Start", "Duration (h)", "Participants"])
    for m in report.longest:
        w.writerow([m.title, m.starts_at.strftime("%Y-%m-%d %H:%M"), m.hours, m.participants])
    w.writerow([])
    w.writerow(["By organiser"])
    w.writerow(["Organiser", "Meetings", "Hours"])
    for o in report.by_organiser:
        w.writerow([o.organiser, o.meetings, o.hours])
    # utf-8-sig: Excel otherwise mangles Ukrainian meeting titles.
    return out.getvalue().encode("utf-8-sig")


async def build_weekly_report_async(
    week: str, session_factory: async_sessionmaker | None = None, tz: ZoneInfo | None = None
) -> bytes:
    tz = tz or settings.tz
    if session_factory is not None:
        async with session_factory() as session:
            return render_csv(await collect_week(session, week, tz))
    # A private engine without a pool: every call gets its own event loop
    # (asyncio.run), and pooled asyncpg connections cannot outlive their loop.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine)() as session:
            return render_csv(await collect_week(session, week, tz))
    finally:
        await engine.dispose()


def build_weekly_report(week: str) -> bytes:
    """Query the meetings of one ISO week (e.g. "2026-W40") and return the CSV."""
    parse_week(week)
    return asyncio.run(build_weekly_report_async(week))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python -m app.reports.weekly 2026-W40")
    sys.stdout.buffer.write(build_weekly_report(sys.argv[1]))
