from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta
from sqlmodel import select
import resend

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.user import User
from app.models.dealership import Dealership
from app.services.weekly_report import (
    get_user_weekly_stats,
    format_user_report_email,
    format_manager_report_email,
)

settings = get_settings()
resend.api_key = settings.resend_api_key


def last_week_window(now: datetime = None) -> tuple:
    """
    The trailing Mon 00:00 → next Mon 00:00 (UTC) window the weekly report
    covers. Extracted so the cron script and the on-demand admin route share
    exactly one definition of "last week".

    Returns NAIVE-UTC datetimes: week_start/week_end are bound as filter params
    against AdEvent.created_at (TIMESTAMP WITHOUT TIME ZONE) in
    get_user_weekly_stats — asyncpg rejects AWARE datetimes there in prod
    (CLAUDE.md bug #24; dev SQLite tolerated the old aware value, which is why
    this was never caught until the cron actually ran).
    """
    today       = now or datetime.utcnow()
    last_monday = today - timedelta(days=today.weekday() + 7)
    week_start  = last_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end    = week_start + timedelta(days=7)
    return week_start, week_end


async def send_dealership_weekly_report(
    session,
    dealership,
    week_start: datetime,
    week_end:   datetime,
) -> dict:
    """
    Build + send the manager (team leaderboard) report for a SINGLE dealership
    and return a summary of what happened. This is the reusable unit — both
    the full cron loop (send_weekly_reports) and the on-demand admin route call
    it, so the query/email-building logic lives in exactly one place.

    Returns a summary dict: sent flag, recipient, date range, and the counts
    the report itself contains (so the admin route can echo them back). Never
    raises — email/query failures are captured into the summary.
    """
    summary = {
        'dealership_id':   dealership.id,
        'dealership_name': dealership.dealership_name,
        'week_start':      week_start.isoformat(),
        'week_end':        week_end.isoformat(),
        'sent':            False,
        'recipient':       None,
        'skipped_reason':  None,
        'staff_count':     0,
        'total_generated': 0,
        'total_posted':    0,
        'total_sold':      0,
    }

    if not dealership.manager_user_id:
        summary['skipped_reason'] = 'no_manager'
        return summary

    manager = await session.get(User, dealership.manager_user_id)
    if not manager or not manager.email:
        summary['skipped_reason'] = 'no_manager_email'
        return summary

    staff_result = await session.exec(
        select(User).where(User.dealership_id == dealership.id)
    )
    staff = staff_result.all()
    if not staff:
        summary['skipped_reason'] = 'no_staff'
        summary['recipient'] = manager.email
        return summary

    staff_stats = []
    for member in staff:
        stats = await get_user_weekly_stats(
            session    = session,
            user_id    = member.id,
            week_start = week_start,
            week_end   = week_end,
        )
        staff_stats.append({'user': member, 'stats': stats})

    summary['staff_count']     = len(staff_stats)
    summary['total_generated'] = sum(s['stats']['total_generated'] for s in staff_stats)
    summary['total_posted']    = sum(s['stats']['total_posted']    for s in staff_stats)
    summary['total_sold']      = sum(s['stats']['vehicles_sold']   for s in staff_stats)
    summary['recipient']       = manager.email

    html = format_manager_report_email(
        manager     = manager,
        dealership  = dealership,
        staff_stats = staff_stats,
        week_start  = week_start,
    )

    try:
        resend.Emails.send({
            'from':    'DealersOrbit <reports@mail.dealersorbit.com>',
            'to':      [manager.email],
            'subject': f'Team Report — {dealership.dealership_name} — {week_start.strftime("%b %d")}',
            'html':    html,
        })
        summary['sent'] = True
        print(f'Sent manager report to {manager.email} for {dealership.dealership_name}')
    except Exception as e:
        summary['error'] = str(e)
        print(f'Failed to send manager report for dealership {dealership.id}: {e}')

    return summary


async def send_weekly_reports():
    week_start, week_end = last_week_window()

    print(f'Sending weekly reports for {week_start.date()} to {week_end.date()}')

    async with AsyncSessionLocal() as session:
        result = await session.exec(select(User).where(User.is_active == True))
        users  = result.all()

        sent_count = 0

        for user in users:
            if not user.email:
                continue

            try:
                stats = await get_user_weekly_stats(
                    session    = session,
                    user_id    = user.id,
                    week_start = week_start,
                    week_end   = week_end,
                )

                if stats['total_generated'] == 0 and stats['total_posted'] == 0:
                    continue

                html = format_user_report_email(user, stats, week_start)

                resend.Emails.send({
                    'from':    'DealersOrbit <reports@mail.dealersorbit.com>',
                    'to':      [user.email],
                    'subject': f'Your DealersOrbit Week in Review — {week_start.strftime("%b %d")}',
                    'html':    html,
                })

                sent_count += 1
                print(f'Sent report to {user.email}')

                await asyncio.sleep(0.5)

            except Exception as e:
                print(f'Failed to send report to {user.email}: {e}')

        dealerships_result = await session.exec(select(Dealership))
        dealerships        = dealerships_result.all()

        for dealership in dealerships:
            # All the per-dealership logic now lives in one reusable function
            # (also called by the on-demand admin route) — never fork it here.
            await send_dealership_weekly_report(session, dealership, week_start, week_end)

        print(f'Weekly reports complete. Sent {sent_count} individual reports.')


if __name__ == '__main__':
    asyncio.run(send_weekly_reports())
