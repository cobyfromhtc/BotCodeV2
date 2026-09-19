# -*- coding: utf-8 -*-
'''
TicketTool.scheduling — Ticket Scheduling / Business Hours (Tier 1 Feature #4).

Per-panel availability windows. A panel can be open only during configured
periods (e.g. Mon-Fri 16:00-22:00, Sat-Sun 12:00-23:00). Roles can bypass the
schedule. Outside the schedule, the Create Ticket button shows a custom
"unavailable" message instead of opening a ticket.

Integration:
  * TicketPanelView.create_button checks scheduling.is_panel_open_now(...)
    BEFORE showing the questions modal / creating the channel. When closed,
    replies with the panel's unavailable_message.
  * Premium wiring re-evaluates panel messages on startup so the button state
    can be toggled if desired (optional; the check at click-time is the
    authoritative gate).
'''

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .db import PremiumDB, _json_loads_list


# Day-of-week mapping. 0=Monday ... 6=Sunday (Python weekday()).
# Schedules are stored with day names for human readability.
DAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday',
             'friday', 'saturday', 'sunday']
DAY_INDEX = {name: i for i, name in enumerate(DAY_NAMES)}
WEEKDAYS = {'monday', 'tuesday', 'wednesday', 'thursday', 'friday'}
WEEKEND = {'saturday', 'sunday'}


# =====================================================================
# TIMEZONE HANDLING
# =====================================================================

# A small built-in offset table so users can pick a friendly timezone name
# without needing pytz (which may not be installed). Falls back to UTC.
# Offsets are in hours from UTC.
COMMON_TIMEZONES = {
    'UTC': 0.0,
    'ET': -5.0, 'EST': -5.0, 'EDT': -4.0,
    'CT': -6.0, 'CST': -6.0, 'CDT': -5.0,
    'MT': -7.0, 'MST': -7.0, 'MDT': -6.0,
    'PT': -8.0, 'PST': -8.0, 'PDT': -7.0,
    'GMT': 0.0, 'BST': 1.0, 'CET': 1.0, 'CEST': 2.0,
    'EET': 2.0, 'EEST': 3.0,
    'AEST': 10.0, 'AEDT': 11.0,
    'IST': 5.5, 'PKT': 5.0, 'JST': 9.0,
}


def tz_offset_hours(tz_name: str) -> float:
    if not tz_name:
        return 0.0
    return COMMON_TIMEZONES.get(tz_name.strip(), 0.0)


def now_in_tz(tz_name: str) -> datetime:
    '''Current time in the given timezone (as a naive local datetime).'''
    offset = tz_offset_hours(tz_name)
    return datetime.now(timezone.utc) + timedelta(hours=offset)


# =====================================================================
# PERIOD NORMALIZATION
# =====================================================================

def normalize_period(period: Dict) -> Optional[Dict]:
    '''Validate + normalize one availability period.

    Accepted shapes:
      {'day': 'monday', 'start': '16:00', 'end': '22:00'}
      {'day': 'weekday', ...}   -> expands to Mon-Fri
      {'day': 'weekend', ...}   -> expands to Sat-Sun
      {'day': 'daily', ...}     -> all 7 days

    Returns the normalized {'day','start','end'} dict, or None if invalid.
    '''
    if not isinstance(period, dict):
        return None
    day = str(period.get('day', '')).strip().lower()
    start = str(period.get('start', '')).strip()
    end = str(period.get('end', '')).strip()
    if not (start and end):
        return None
    if not _valid_time(start) or not _valid_time(end):
        return None
    if day in DAY_INDEX:
        return {'day': day, 'start': start, 'end': end}
    if day in ('weekday', 'weekdays'):
        return {'day': 'weekday', 'start': start, 'end': end}
    if day in ('weekend',):
        return {'day': 'weekend', 'start': start, 'end': end}
    if day in ('daily', 'everyday', 'all'):
        return {'day': 'daily', 'start': start, 'end': end}
    return None


def expand_periods(periods: List[Dict]) -> List[Dict]:
    '''Expand 'weekday'/'weekend'/'daily' shorthand into per-day periods.'''
    out: List[Dict] = []
    for p in periods or []:
        norm = normalize_period(p)
        if not norm:
            continue
        day = norm['day']
        if day in DAY_INDEX:
            out.append(norm)
        elif day == 'weekday':
            for d in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday']:
                out.append({'day': d, 'start': norm['start'], 'end': norm['end']})
        elif day == 'weekend':
            for d in ['saturday', 'sunday']:
                out.append({'day': d, 'start': norm['start'], 'end': norm['end']})
        elif day == 'daily':
            for d in DAY_NAMES:
                out.append({'day': d, 'start': norm['start'], 'end': norm['end']})
    return out


def _valid_time(t: str) -> bool:
    '''Accept 'HH:MM' (24h).'''
    if not t or ':' not in t:
        return False
    parts = t.split(':')
    if len(parts) != 2:
        return False
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 0 <= h <= 23 and 0 <= m <= 59


def _time_to_minutes(t: str) -> int:
    h, m = t.split(':')
    return int(h) * 60 + int(m)


# =====================================================================
# AVAILABILITY CHECK
# =====================================================================

def is_open_at(periods: List[Dict], tz_name: str, when: Optional[datetime] = None) -> bool:
    '''True if any expanded period covers `when` (in the panel's tz).'''
    if not periods:
        return True  # no periods configured = always open
    expanded = expand_periods(periods)
    if not expanded:
        return True  # all periods were invalid; treat as always-open
    now = when or now_in_tz(tz_name)
    today = DAY_NAMES[now.weekday()]
    now_min = now.hour * 60 + now.minute
    for p in expanded:
        if p['day'] != today:
            continue
        start_min = _time_to_minutes(p['start'])
        end_min = _time_to_minutes(p['end'])
        if end_min <= start_min:
            # overnight window (e.g. 22:00-02:00): open if now >= start OR now < end
            if now_min >= start_min or now_min < end_min:
                return True
        else:
            if start_min <= now_min < end_min:
                return True
    return False


def is_panel_open_now(pdb: PremiumDB, panel: Dict, member_role_ids: Optional[List[int]] = None) -> Tuple[bool, Optional[str]]:
    '''Check whether a panel is currently open for ticket creation.

    Returns (is_open, unavailable_message). When the panel has no schedule
    or the schedule is disabled, returns (True, None). When a bypass role
    matches the member's roles, returns (True, None).
    '''
    panel_id = panel.get('panel_id') if panel else None
    if not panel_id:
        return True, None
    sched = pdb.get_schedule(panel_id)
    if not sched or not sched.get('enabled'):
        return True, None
    tz_name = sched.get('timezone') or 'UTC'
    periods = _json_loads_list(sched.get('periods'))
    if not periods:
        return True, None
    # Bypass roles
    bypass = _json_loads_list(sched.get('bypass_role_ids'))
    if bypass and member_role_ids:
        if any(int(rid) in [int(r) for r in bypass] for rid in member_role_ids):
            return True, None
    open_now = is_open_at(periods, tz_name)
    if open_now:
        return True, None
    msg = sched.get('unavailable_message') or 'Tickets are currently unavailable for this panel.'
    return False, msg


def next_open_time(pdb: PremiumDB, panel: Dict) -> Optional[str]:
    '''Human-readable 'next opens at' string, or None if always open.'''
    panel_id = panel.get('panel_id') if panel else None
    if not panel_id:
        return None
    sched = pdb.get_schedule(panel_id)
    if not sched or not sched.get('enabled'):
        return None
    tz_name = sched.get('timezone') or 'UTC'
    periods = expand_periods(_json_loads_list(sched.get('periods')))
    if not periods:
        return None
    now = now_in_tz(tz_name)
    # Search the next 7 days for the first matching open window.
    for day_offset in range(0, 8):
        candidate = now + timedelta(days=day_offset)
        today = DAY_NAMES[candidate.weekday()]
        for p in periods:
            if p['day'] != today:
                continue
            start = _time_to_minutes(p['start'])
            cand_min = candidate.hour * 60 + candidate.minute
            # If same day and start is in the future, that's our window.
            if day_offset == 0 and cand_min >= start:
                continue
            window_start = candidate.replace(
                hour=start // 60, minute=start % 60, second=0, microsecond=0
            )
            return f"{window_start.strftime('%A %H:%M')} ({tz_name})"
    return None


# =====================================================================
# CONFIG ACCESSORS (for the !schedule command)
# =====================================================================

def get_config(pdb: PremiumDB, panel_id: str) -> Optional[Dict]:
    return pdb.get_schedule_raw(panel_id)


def save_config(pdb: PremiumDB, panel_id: str, guild_id: int, *,
                timezone: str = 'UTC', periods: Optional[List[Dict]] = None,
                unavailable_message: Optional[str] = None,
                bypass_role_ids: Optional[List[int]] = None,
                enabled: bool = True) -> str:
    return pdb.upsert_schedule({
        'panel_id': panel_id,
        'guild_id': guild_id,
        'timezone': timezone,
        'periods': periods or [],
        'unavailable_message': unavailable_message,
        'bypass_role_ids': bypass_role_ids or [],
        'enabled': enabled,
    })


def disable(pdb: PremiumDB, panel_id: str) -> bool:
    return pdb.delete_schedule(panel_id)
