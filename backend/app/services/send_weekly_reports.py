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


async def send_weekly_reports():
    today       = datetime.now(timezone.utc)
    last_monday = today - timedelta(days=today.weekday() + 7)
    week_start  = last_monday.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end    = week_start + timedelta(days=7)

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
            if not dealership.manager_user_id:
                continue

            try:
                manager = await session.get(User, dealership.manager_user_id)
                if not manager or not manager.email:
                    continue

                staff_result = await session.exec(
                    select(User).where(User.dealership_id == dealership.id)
                )
                staff = staff_result.all()

                if not staff:
                    continue

                staff_stats = []
                for member in staff:
                    stats = await get_user_weekly_stats(
                        session    = session,
                        user_id    = member.id,
                        week_start = week_start,
                        week_end   = week_end,
                    )
                    staff_stats.append({'user': member, 'stats': stats})

                html = format_manager_report_email(
                    manager     = manager,
                    dealership  = dealership,
                    staff_stats = staff_stats,
                    week_start  = week_start,
                )

                resend.Emails.send({
                    'from':    'DealersOrbit <reports@mail.dealersorbit.com>',
                    'to':      [manager.email],
                    'subject': f'Team Report — {dealership.dealership_name} — {week_start.strftime("%b %d")}',
                    'html':    html,
                })

                print(f'Sent manager report to {manager.email} for {dealership.dealership_name}')

            except Exception as e:
                print(f'Failed to send manager report for dealership {dealership.id}: {e}')

        print(f'Weekly reports complete. Sent {sent_count} individual reports.')


if __name__ == '__main__':
    asyncio.run(send_weekly_reports())
