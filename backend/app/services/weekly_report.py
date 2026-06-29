from __future__ import annotations
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from app.models.ad_event import AdEvent
from app.models.user import User
from app.models.dealership import Dealership


async def get_user_weekly_stats(
    session:    AsyncSession,
    user_id:    int,
    week_start: datetime,
    week_end:   datetime,
) -> dict:
    result = await session.exec(
        select(AdEvent).where(
            AdEvent.user_id    == user_id,
            AdEvent.created_at >= week_start,
            AdEvent.created_at <  week_end,
        )
    )
    events = result.all()

    generated    = [e for e in events if e.event_type == 'generated']
    failed       = [e for e in events if e.event_type == 'generation_failed']
    mp_posts     = [e for e in events if e.event_type == 'posted_marketplace']
    fb_posts     = [e for e in events if e.event_type == 'posted_fb_post']
    group_posts  = [e for e in events if e.event_type == 'posted_fb_groups']
    sold         = [e for e in events if e.event_type == 'sold_detected']

    total_generated = len(generated)
    total_posted    = len(mp_posts) + len(fb_posts) + len(group_posts)
    post_rate       = round(total_posted / total_generated * 100) if total_generated > 0 else 0

    themes = [e.theme_used for e in generated if e.theme_used]
    top_theme = max(set(themes), key=themes.count) if themes else 'N/A'

    formats = [e.video_format for e in generated if e.video_format]
    top_format = max(set(formats), key=formats.count) if formats else 'N/A'

    voice_types = [e.voice_type for e in generated if e.voice_type]
    top_voice_type = max(set(voice_types), key=voice_types.count) if voice_types else 'N/A'

    post_days = [e.day_of_week for e in (mp_posts + fb_posts + group_posts) if e.day_of_week is not None]
    day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    best_day = day_names[max(set(post_days), key=post_days.count)] if post_days else 'N/A'

    render_times = [e.render_time_seconds for e in generated if e.render_time_seconds]
    avg_render   = round(sum(render_times) / len(render_times)) if render_times else 0

    return {
        'total_generated':    total_generated,
        'total_failed':       len(failed),
        'total_posted':       total_posted,
        'marketplace_posts':  len(mp_posts),
        'fb_posts':           len(fb_posts),
        'group_posts':        len(group_posts),
        'post_rate_pct':      post_rate,
        'vehicles_sold':      len(sold),
        'top_theme':          top_theme,
        'top_format':         top_format,
        'top_voice_type':     top_voice_type,
        'best_posting_day':   best_day,
        'avg_render_seconds': avg_render,
    }


def format_user_report_email(
    user:       User,
    stats:      dict,
    week_start: datetime,
) -> str:
    week_str = week_start.strftime('%B %d, %Y')

    if stats['post_rate_pct'] >= 80:
        feedback = "🔥 Outstanding week! You're posting almost everything you generate."
    elif stats['post_rate_pct'] >= 50:
        feedback = "👍 Good week! Try to post more of the ads you generate for maximum reach."
    elif stats['total_generated'] > 0:
        feedback = "💡 You generated some great ads this week — try to post more of them to Facebook!"
    else:
        feedback = "📱 Quiet week! Try importing some vehicles and generating ads to boost your reach."

    format_labels = {
        'listing':    '📸 Photos + Listing',
        'slideshow':  '🎬 Slideshow Video',
        'with_outro': '🎥 Slideshow + Outro',
    }

    sold_row = (
        f"<tr><td style='padding:10px 12px;font-size:13px;color:#374151'>🚗 Vehicles Sold</td>"
        f"<td style='padding:10px 12px;font-size:13px;font-weight:700;color:#16a34a;text-align:right'>"
        f"{stats['vehicles_sold']}</td></tr>"
        if stats['vehicles_sold'] > 0 else ''
    )

    return f'''
<div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto;padding:20px">
  <div style="background:#1a56db;color:white;padding:24px;border-radius:12px 12px 0 0;text-align:center">
    <h1 style="margin:0;font-size:24px">OrbitAds Weekly Report</h1>
    <p style="margin:8px 0 0;opacity:0.8">Week of {week_str}</p>
  </div>

  <div style="background:white;border:1px solid #e5e7eb;border-top:none;padding:24px;border-radius:0 0 12px 12px">
    <p style="font-size:16px;color:#374151">Hi {user.full_name or 'there'} 👋</p>
    <p style="color:#6b7280">{feedback}</p>

    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin:20px 0">
      <div style="background:#f8faff;border:1px solid #dbeafe;border-radius:8px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:#1a56db">{stats['total_generated']}</div>
        <div style="font-size:12px;color:#6b7280;margin-top:4px">Ads Generated</div>
      </div>
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:#16a34a">{stats['total_posted']}</div>
        <div style="font-size:12px;color:#6b7280;margin-top:4px">Ads Posted</div>
      </div>
      <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:#ea580c">{stats['post_rate_pct']}%</div>
        <div style="font-size:12px;color:#6b7280;margin-top:4px">Post Rate</div>
      </div>
    </div>

    <h3 style="color:#374151;margin:20px 0 12px">Where You Posted</h3>
    <table style="width:100%;border-collapse:collapse">
      <tr style="background:#f9fafb">
        <td style="padding:10px 12px;font-size:13px;color:#374151">🏪 Facebook Marketplace</td>
        <td style="padding:10px 12px;font-size:13px;font-weight:700;color:#1a56db;text-align:right">{stats['marketplace_posts']}</td>
      </tr>
      <tr>
        <td style="padding:10px 12px;font-size:13px;color:#374151">📘 Facebook Posts</td>
        <td style="padding:10px 12px;font-size:13px;font-weight:700;color:#1a56db;text-align:right">{stats['fb_posts']}</td>
      </tr>
      <tr style="background:#f9fafb">
        <td style="padding:10px 12px;font-size:13px;color:#374151">👥 Facebook Groups</td>
        <td style="padding:10px 12px;font-size:13px;font-weight:700;color:#1a56db;text-align:right">{stats['group_posts']}</td>
      </tr>
      {sold_row}
    </table>

    <h3 style="color:#374151;margin:20px 0 12px">Your Preferences This Week</h3>
    <table style="width:100%;border-collapse:collapse">
      <tr style="background:#f9fafb">
        <td style="padding:10px 12px;font-size:13px;color:#6b7280">Top Theme</td>
        <td style="padding:10px 12px;font-size:13px;font-weight:600;color:#374151;text-align:right">{stats['top_theme'].title()}</td>
      </tr>
      <tr>
        <td style="padding:10px 12px;font-size:13px;color:#6b7280">Top Format</td>
        <td style="padding:10px 12px;font-size:13px;font-weight:600;color:#374151;text-align:right">{format_labels.get(stats['top_format'], stats['top_format'])}</td>
      </tr>
      <tr style="background:#f9fafb">
        <td style="padding:10px 12px;font-size:13px;color:#6b7280">Best Posting Day</td>
        <td style="padding:10px 12px;font-size:13px;font-weight:600;color:#374151;text-align:right">{stats['best_posting_day']}</td>
      </tr>
      <tr>
        <td style="padding:10px 12px;font-size:13px;color:#6b7280">Voice Type</td>
        <td style="padding:10px 12px;font-size:13px;font-weight:600;color:#374151;text-align:right">{stats['top_voice_type'].title() if stats['top_voice_type'] != 'N/A' else 'N/A'}</td>
      </tr>
    </table>

    <div style="margin-top:24px;padding:16px;background:#f8faff;border-radius:8px;text-align:center">
      <p style="margin:0;font-size:13px;color:#6b7280">Keep up the great work! The more you post, the more reach you get.</p>
    </div>

    <p style="margin-top:20px;font-size:12px;color:#9ca3af;text-align:center">
      OrbitAds by DealersOrbit ·
      <a href="https://dealersorbit.com" style="color:#1a56db">dealersorbit.com</a>
    </p>
  </div>
</div>
'''


def format_manager_report_email(
    manager:     User,
    dealership:  Dealership,
    staff_stats: list,
    week_start:  datetime,
) -> str:
    week_str = week_start.strftime('%B %d, %Y')

    staff_stats.sort(key=lambda x: x['stats']['total_posted'], reverse=True)

    total_generated = sum(s['stats']['total_generated'] for s in staff_stats)
    total_posted    = sum(s['stats']['total_posted']    for s in staff_stats)
    total_sold      = sum(s['stats']['vehicles_sold']   for s in staff_stats)

    leaderboard_rows = ''
    for i, item in enumerate(staff_stats):
        u     = item['user']
        stats = item['stats']
        medal = ['🥇', '🥈', '🥉'][i] if i < 3 else f'{i+1}.'
        bg    = 'background:#f9fafb;' if i % 2 == 0 else ''
        rate_color = '#16a34a' if stats['post_rate_pct'] >= 70 else '#ea580c'
        leaderboard_rows += (
            f"<tr style='{bg}'>"
            f"<td style='padding:12px;font-size:13px;color:#374151'>{medal} {u.full_name or u.email}</td>"
            f"<td style='padding:12px;font-size:13px;text-align:center'>{stats['total_generated']}</td>"
            f"<td style='padding:12px;font-size:13px;text-align:center'>{stats['total_posted']}</td>"
            f"<td style='padding:12px;font-size:13px;text-align:center;color:{rate_color}'>{stats['post_rate_pct']}%</td>"
            f"<td style='padding:12px;font-size:13px;text-align:center'>{stats['vehicles_sold']}</td>"
            f"</tr>"
        )

    return f'''
<div style="font-family:-apple-system,sans-serif;max-width:650px;margin:0 auto;padding:20px">
  <div style="background:#111827;color:white;padding:24px;border-radius:12px 12px 0 0;text-align:center">
    <h1 style="margin:0;font-size:22px">Manager Report — {dealership.dealership_name}</h1>
    <p style="margin:8px 0 0;opacity:0.6">Week of {week_str}</p>
  </div>

  <div style="background:white;border:1px solid #e5e7eb;border-top:none;padding:24px;border-radius:0 0 12px 12px">
    <p style="font-size:16px;color:#374151">Hi {manager.full_name or 'there'} 👋</p>
    <p style="color:#6b7280">Here's how your team performed on OrbitAds this week.</p>

    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;margin:20px 0">
      <div style="background:#f8faff;border:1px solid #dbeafe;border-radius:8px;padding:12px;text-align:center">
        <div style="font-size:24px;font-weight:700;color:#1a56db">{len(staff_stats)}</div>
        <div style="font-size:11px;color:#6b7280">Active Staff</div>
      </div>
      <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:8px;padding:12px;text-align:center">
        <div style="font-size:24px;font-weight:700;color:#16a34a">{total_generated}</div>
        <div style="font-size:11px;color:#6b7280">Ads Generated</div>
      </div>
      <div style="background:#fff7ed;border:1px solid #fed7aa;border-radius:8px;padding:12px;text-align:center">
        <div style="font-size:24px;font-weight:700;color:#ea580c">{total_posted}</div>
        <div style="font-size:11px;color:#6b7280">Ads Posted</div>
      </div>
      <div style="background:#fdf4ff;border:1px solid #e9d5ff;border-radius:8px;padding:12px;text-align:center">
        <div style="font-size:24px;font-weight:700;color:#7c3aed">{total_sold}</div>
        <div style="font-size:11px;color:#6b7280">Vehicles Sold</div>
      </div>
    </div>

    <h3 style="color:#374151;margin:20px 0 12px">🏆 Team Leaderboard</h3>
    <table style="width:100%;border-collapse:collapse;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
      <thead>
        <tr style="background:#f3f4f6">
          <th style="padding:10px 12px;font-size:12px;color:#6b7280;text-align:left">Salesperson</th>
          <th style="padding:10px 12px;font-size:12px;color:#6b7280;text-align:center">Generated</th>
          <th style="padding:10px 12px;font-size:12px;color:#6b7280;text-align:center">Posted</th>
          <th style="padding:10px 12px;font-size:12px;color:#6b7280;text-align:center">Post Rate</th>
          <th style="padding:10px 12px;font-size:12px;color:#6b7280;text-align:center">Sold</th>
        </tr>
      </thead>
      <tbody>
        {leaderboard_rows}
      </tbody>
    </table>

    <p style="margin-top:20px;font-size:12px;color:#9ca3af;text-align:center">
      OrbitAds Manager Dashboard ·
      <a href="https://dealersorbit.com" style="color:#1a56db">dealersorbit.com</a>
    </p>
  </div>
</div>
'''
