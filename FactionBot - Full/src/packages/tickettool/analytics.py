# -*- coding: utf-8 -*-
'''
TicketTool.analytics — Ticket Analytics + CSAT (Tier 1 Features #7 + #15).

Read-only analytics computed from the existing tickets / ticket_transcripts /
ticket_staff_stats tables. The underlying data already exists in Bot.py —
this module adds the aggregation queries Ticket Tool Pro ships:
  * Ticket trends over time (created/closed per day)
  * Tickets by panel
  * Active-ticket statistics
  * Claimed-ticket statistics
  * Staff participation percentages
  * Historical statistics (no retention limit, unlike the free tier)
  * CSAT: average rating, by staff, by panel, over time, pos/neg %, feedback list
  * Data export (CSV-shaped string)

Integration:
  * /analytics  command -> build_overview(pdb, guild_id)
  * /csat       command -> build_csat_report(pdb, guild_id, by='staff'|'panel'|'time')
  * /staffstats command -> build_staff_report(pdb, guild_id)
  * /export     command -> export_csv(pdb, guild_id)

All queries run on the shared DataManager connection. We rebuild the
ticket_staff_stats cache on demand rather than maintaining it incrementally
(cheap enough; a few hundred tickets is trivial).
'''

from __future__ import annotations

import csv
import io
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from .db import PremiumDB


# =====================================================================
# LOW-LEVEL QUERY HELPERS
# =====================================================================

def _fetchall(conn, sql, args=()):
    cur = conn.cursor()
    cur.execute(sql, args)
    return [dict(r) for r in cur.fetchall()]


def _fetchone(conn, sql, args=()):
    cur = conn.cursor()
    cur.execute(sql, args)
    row = cur.fetchone()
    return dict(row) if row else None


def _parse_iso(s) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None


# =====================================================================
# OVERVIEW ANALYTICS
# =====================================================================

def build_overview(pdb: PremiumDB, guild_id: int, *, days: int = 30) -> Dict:
    '''High-level ticket analytics for the last `days` days.'''
    conn = pdb.dm._connection
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    total = _fetchone(conn, 'SELECT COUNT(*) AS c FROM tickets WHERE guild_id = ?', (guild_id,)) or {}
    open_count = _fetchone(conn, 'SELECT COUNT(*) AS c FROM tickets WHERE guild_id = ? AND status IN ("open","pending")', (guild_id,)) or {}
    closed = _fetchone(conn, 'SELECT COUNT(*) AS c FROM tickets WHERE guild_id = ? AND status = "closed"', (guild_id,)) or {}

    # Created-per-day for the trend chart.
    created_rows = _fetchall(conn,
        'SELECT created_at FROM tickets WHERE guild_id = ? AND created_at >= ?',
        (guild_id, since))
    closed_rows = _fetchall(conn,
        'SELECT closed_at FROM tickets WHERE guild_id = ? AND closed_at >= ?',
        (guild_id, since))

    trend: Dict[str, Dict[str, int]] = defaultdict(lambda: {'created': 0, 'closed': 0})
    for r in created_rows:
        d = (_parse_iso(r.get('created_at')) or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
        trend[d]['created'] += 1
    for r in closed_rows:
        d = (_parse_iso(r.get('closed_at')) or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
        trend[d]['closed'] += 1
    trend_sorted = [{'date': k, **trend[k]} for k in sorted(trend.keys())]

    # By panel.
    panel_rows = _fetchall(conn,
        '''SELECT panel_id, COUNT(*) AS total,
                  SUM(CASE WHEN status IN ('open','pending') THEN 1 ELSE 0 END) AS open_n,
                  SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_n
           FROM tickets WHERE guild_id = ?
           GROUP BY panel_id''',
        (guild_id,))
    # Resolve panel names.
    panels_map = {}
    for p in pdb.dm.load_all_ticket_panels():
        panels_map[p.get('panel_id')] = p.get('name', 'Unknown')
    by_panel = []
    for r in panel_rows:
        by_panel.append({
            'panel_id': r.get('panel_id'),
            'panel_name': panels_map.get(r.get('panel_id'), 'Unknown'),
            'total': r.get('total', 0),
            'open': r.get('open_n', 0),
            'closed': r.get('closed_n', 0),
        })

    # Active tickets breakdown by priority.
    prio_rows = _fetchall(conn,
        '''SELECT COALESCE(priority,'normal') AS priority, COUNT(*) AS c
           FROM tickets WHERE guild_id = ? AND status IN ('open','pending')
           GROUP BY priority''',
        (guild_id,))
    by_priority = {r['priority']: r['c'] for r in prio_rows}

    # Claimed-ticket stats.
    claimed_rows = _fetchall(conn,
        '''SELECT COUNT(*) AS c FROM tickets
           WHERE guild_id = ? AND claimed_by IS NOT NULL AND status IN ('open','pending')''',
        (guild_id,))
    claimed_open = claimed_rows[0]['c'] if claimed_rows else 0
    claimed_closed_rows = _fetchall(conn,
        '''SELECT COUNT(*) AS c FROM tickets
           WHERE guild_id = ? AND claimed_by IS NOT NULL AND status = 'closed' ''',
        (guild_id,))
    claimed_closed = claimed_closed_rows[0]['c'] if claimed_closed_rows else 0

    return {
        'days': days,
        'total': total.get('c', 0),
        'open': open_count.get('c', 0),
        'closed': closed.get('c', 0),
        'trend': trend_sorted,
        'by_panel': by_panel,
        'by_priority': by_priority,
        'claimed_open': claimed_open,
        'claimed_closed': claimed_closed,
    }


# =====================================================================
# CSAT ANALYTICS
# =====================================================================

def build_csat_report(pdb: PremiumDB, guild_id: int, *, by: str = 'staff') -> Dict:
    '''CSAT report. `by` is one of: 'staff', 'panel', 'time'.'''
    conn = pdb.dm._connection
    rows = _fetchall(conn,
        '''SELECT ticket_id, panel_id, creator_id, claimed_by, rating, rating_feedback,
                  created_at, closed_at, status
           FROM tickets WHERE guild_id = ? AND rating IS NOT NULL''',
        (guild_id,))

    if not rows:
        return {'by': by, 'avg': None, 'count': 0, 'positive_pct': None,
                'negative_pct': None, 'breakdown': [], 'feedback': []}

    ratings = [int(r['rating']) for r in rows if r.get('rating') is not None]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    positive = sum(1 for r in ratings if r >= 4)
    negative = sum(1 for r in ratings if r <= 2)
    pos_pct = round(100 * positive / len(ratings), 1) if ratings else None
    neg_pct = round(100 * negative / len(ratings), 1) if ratings else None

    # Resolve panel names.
    panels_map = {p.get('panel_id'): p.get('name', 'Unknown') for p in pdb.dm.load_all_ticket_panels()}

    breakdown: List[Dict] = []
    feedback: List[Dict] = []

    if by == 'staff':
        groups: Dict[int, List[int]] = defaultdict(list)
        feedback_by_staff: Dict[int, List[Dict]] = defaultdict(list)
        for r in rows:
            cb = r.get('claimed_by')
            if cb is None:
                continue
            groups[int(cb)].append(int(r['rating']))
            feedback_by_staff[int(cb)].append({
                'ticket_id': r.get('ticket_id'),
                'rating': int(r['rating']),
                'feedback': r.get('rating_feedback') or '',
                'closed_at': r.get('closed_at'),
            })
        for staff_id, rs in groups.items():
            breakdown.append({
                'staff_id': staff_id,
                'count': len(rs),
                'avg': round(sum(rs)/len(rs), 2),
                'positive_pct': round(100 * sum(1 for x in rs if x >= 4)/len(rs), 1),
            })
            feedback.extend(feedback_by_staff[staff_id][:20])
    elif by == 'panel':
        groups_p: Dict[str, List[int]] = defaultdict(list)
        for r in rows:
            pid = r.get('panel_id') or 'unknown'
            groups_p[pid].append(int(r['rating']))
        for pid, rs in groups_p.items():
            breakdown.append({
                'panel_id': pid,
                'panel_name': panels_map.get(pid, 'Unknown'),
                'count': len(rs),
                'avg': round(sum(rs)/len(rs), 2),
                'positive_pct': round(100 * sum(1 for x in rs if x >= 4)/len(rs), 1),
            })
    elif by == 'time':
        # Group by week.
        groups_t: Dict[str, List[int]] = defaultdict(list)
        for r in rows:
            d = _parse_iso(r.get('closed_at') or r.get('created_at'))
            if not d:
                continue
            iso = d.strftime('%G-W%V')
            groups_t[iso].append(int(r['rating']))
        for iso, rs in sorted(groups_t.items()):
            breakdown.append({
                'week': iso,
                'count': len(rs),
                'avg': round(sum(rs)/len(rs), 2),
                'positive_pct': round(100 * sum(1 for x in rs if x >= 4)/len(rs), 1),
            })

    # Recent feedback (always).
    recent_feedback = [
        {
            'ticket_id': r.get('ticket_id'),
            'rating': int(r['rating']),
            'feedback': r.get('rating_feedback') or '',
            'claimed_by': r.get('claimed_by'),
            'closed_at': r.get('closed_at'),
        }
        for r in sorted(rows, key=lambda x: x.get('closed_at') or '', reverse=True)
    ][:25]

    return {
        'by': by,
        'avg': avg,
        'count': len(ratings),
        'positive_pct': pos_pct,
        'negative_pct': neg_pct,
        'breakdown': breakdown,
        'feedback': recent_feedback,
    }


# =====================================================================
# STAFF PERFORMANCE
# =====================================================================

def build_staff_report(pdb: PremiumDB, guild_id: int) -> List[Dict]:
    '''Per-staff ticket stats: claimed, closed, avg rating, avg response/resolution.

    Computed live from the tickets table (the ticket_staff_stats cache is
    optional and refreshed lazily).
    '''
    conn = pdb.dm._connection
    rows = _fetchall(conn,
        '''SELECT claimed_by, rating, first_response_at, created_at, closed_at, status,
                  staff_responded_at
           FROM tickets WHERE guild_id = ? AND claimed_by IS NOT NULL''',
        (guild_id,))
    if not rows:
        return []

    stats: Dict[int, Dict] = defaultdict(lambda: {
        'tickets_claimed': 0, 'tickets_closed': 0,
        'ratings_count': 0, 'ratings_sum': 0,
        'first_response_minutes': [], 'resolution_minutes': [],
    })
    for r in rows:
        sid = int(r['claimed_by'])
        stats[sid]['tickets_claimed'] += 1
        if r.get('status') == 'closed':
            stats[sid]['tickets_closed'] += 1
        if r.get('rating') is not None:
            stats[sid]['ratings_count'] += 1
            stats[sid]['ratings_sum'] += int(r['rating'])
        created = _parse_iso(r.get('created_at'))
        fr = _parse_iso(r.get('first_response_at') or r.get('staff_responded_at'))
        if created and fr:
            stats[sid]['first_response_minutes'].append((fr - created).total_seconds()/60.0)
        closed = _parse_iso(r.get('closed_at'))
        if created and closed:
            stats[sid]['resolution_minutes'].append((closed - created).total_seconds()/60.0)

    out = []
    for sid, s in stats.items():
        avg_r = round(s['ratings_sum']/s['ratings_count'], 2) if s['ratings_count'] else None
        avg_fr = round(sum(s['first_response_minutes'])/len(s['first_response_minutes']), 2) if s['first_response_minutes'] else None
        avg_res = round(sum(s['resolution_minutes'])/len(s['resolution_minutes']), 2) if s['resolution_minutes'] else None
        out.append({
            'staff_id': sid,
            'tickets_claimed': s['tickets_claimed'],
            'tickets_closed': s['tickets_closed'],
            'ratings_count': s['ratings_count'],
            'avg_rating': avg_r,
            'avg_first_response_minutes': avg_fr,
            'avg_resolution_minutes': avg_res,
        })
    # Cache the aggregates for fast dashboard reads.
    for entry in out:
        sid = entry['staff_id']
        s = stats.get(sid, {})
        pdb.upsert_staff_stats({**entry, 'guild_id': guild_id,
                                'ratings_sum': int(s.get('ratings_sum', 0))})
    out.sort(key=lambda e: (e['avg_rating'] or 0, e['tickets_closed']), reverse=True)
    return out


# =====================================================================
# CSV EXPORT
# =====================================================================

def export_tickets_csv(pdb: PremiumDB, guild_id: int) -> str:
    '''Return a CSV string of every ticket in the guild (for /export).'''
    conn = pdb.dm._connection
    rows = _fetchall(conn,
        '''SELECT ticket_id, panel_id, creator_id, claimed_by, status, priority,
                  subject, created_at, closed_at, closed_by, close_reason,
                  rating, rating_feedback, first_response_at, escalation_count
           FROM tickets WHERE guild_id = ?
           ORDER BY created_at DESC''',
        (guild_id,))
    if not rows:
        return ''
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    for r in rows:
        writer.writerow({k: ('' if v is None else v) for k, v in r.items()})
    return buf.getvalue()


# =====================================================================
# ADVANCED STAFF ANALYTICS (Tier 2 Feature #25)
# =====================================================================

def build_advanced_staff_report(pdb: PremiumDB, guild_id: int,
                                  *, days: int = 30) -> List[Dict]:
    '''Per-staff analytics with activity-over-time and response distributions.

    Extends build_staff_report with:
      * Tickets handled per day (for a trend chart)
      * Response time distribution (percentiles)
      * Resolution time distribution (percentiles)
      * Active vs idle hours (when the staff member is most active)
      * Ticket handling patterns (avg messages per ticket, escalation rate)
    '''
    conn = pdb.dm._connection
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = _fetchall(conn,
        '''SELECT ticket_id, claimed_by, created_at, closed_at, status,
                  priority, first_response_at, staff_responded_at, escalation_count,
                  close_reason
           FROM tickets WHERE guild_id = ? AND claimed_by IS NOT NULL
           AND created_at >= ?''',
        (guild_id, since))
    if not rows:
        return []

    stats: Dict[int, Dict] = defaultdict(lambda: {
        'tickets_claimed': 0, 'tickets_closed': 0,
        'first_response_minutes': [], 'resolution_minutes': [],
        'by_day': defaultdict(int),
        'by_hour': defaultdict(int),
        'messages_per_ticket': [],
        'escalations': 0,
    })
    # We need message counts per ticket; load from ticket_messages.
    # SQLite caps the number of bound parameters (999 by default), so the ID
    # list is queried in chunks of 500 and the results merged — identical
    # output, no parameter-limit crash on large guilds.
    msg_counts: Dict[str, int] = {}
    cur = conn.cursor()
    all_ids = [r['ticket_id'] for r in rows]
    for i in range(0, len(all_ids), 500):
        chunk = all_ids[i:i + 500]
        cur.execute(
            'SELECT ticket_id, COUNT(*) AS c FROM ticket_messages WHERE ticket_id IN '
            f'({",".join(["?"] * len(chunk))}) GROUP BY ticket_id',
            chunk,
        )
        for mr in cur.fetchall():
            msg_counts[mr['ticket_id']] = int(mr['c'])

    for r in rows:
        sid = int(r['claimed_by'])
        stats[sid]['tickets_claimed'] += 1
        if r.get('status') == 'closed':
            stats[sid]['tickets_closed'] += 1
        created = _parse_iso(r.get('created_at'))
        if created:
            stats[sid]['by_day'][created.strftime('%Y-%m-%d')] += 1
            stats[sid]['by_hour'][created.hour] += 1
        fr = _parse_iso(r.get('first_response_at') or r.get('staff_responded_at'))
        if created and fr:
            stats[sid]['first_response_minutes'].append((fr - created).total_seconds() / 60.0)
        closed = _parse_iso(r.get('closed_at'))
        if created and closed:
            stats[sid]['resolution_minutes'].append((closed - created).total_seconds() / 60.0)
        if int(r.get('escalation_count') or 0) > 0:
            stats[sid]['escalations'] += 1
        mc = msg_counts.get(r['ticket_id'], 0)
        stats[sid]['messages_per_ticket'].append(mc)

    def _percentile(values, pct):
        if not values:
            return None
        s = sorted(values)
        k = int(round((len(s) - 1) * pct / 100.0))
        return round(s[k], 2)

    out = []
    for sid, s in stats.items():
        fr_vals = s['first_response_minutes']
        res_vals = s['resolution_minutes']
        out.append({
            'staff_id': sid,
            'tickets_claimed': s['tickets_claimed'],
            'tickets_closed': s['tickets_closed'],
            'escalations': s['escalations'],
            'avg_first_response_minutes': round(sum(fr_vals)/len(fr_vals), 2) if fr_vals else None,
            'p50_first_response_minutes': _percentile(fr_vals, 50),
            'p90_first_response_minutes': _percentile(fr_vals, 90),
            'avg_resolution_minutes': round(sum(res_vals)/len(res_vals), 2) if res_vals else None,
            'p50_resolution_minutes': _percentile(res_vals, 50),
            'p90_resolution_minutes': _percentile(res_vals, 90),
            'avg_messages_per_ticket': round(sum(s['messages_per_ticket'])/len(s['messages_per_ticket']), 1) if s['messages_per_ticket'] else 0,
            'active_days': len(s['by_day']),
            'peak_hour': max(s['by_hour'].items(), key=lambda x: x[1])[0] if s['by_hour'] else None,
            'by_day': dict(s['by_day']),
        })
    out.sort(key=lambda e: (e['tickets_closed'], e['avg_first_response_minutes'] or 9999), reverse=True)
    return out


def build_ticket_trend_report(pdb: PremiumDB, guild_id: int,
                                *, days: int = 90) -> Dict:
    '''Long-term ticket trend: created/closed per day over `days` days.

    Useful for capacity planning (Ticket Tool Pro historical statistics).
    '''
    conn = pdb.dm._connection
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    created_rows = _fetchall(conn,
        'SELECT created_at FROM tickets WHERE guild_id = ? AND created_at >= ?',
        (guild_id, since))
    closed_rows = _fetchall(conn,
        'SELECT closed_at FROM tickets WHERE guild_id = ? AND closed_at >= ?',
        (guild_id, since))
    trend = defaultdict(lambda: {'created': 0, 'closed': 0})
    for r in created_rows:
        d = (_parse_iso(r.get('created_at')) or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
        trend[d]['created'] += 1
    for r in closed_rows:
        d = (_parse_iso(r.get('closed_at')) or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
        trend[d]['closed'] += 1
    sorted_trend = [{'date': k, **trend[k]} for k in sorted(trend.keys())]
    total_created = sum(t['created'] for t in sorted_trend)
    total_closed = sum(t['closed'] for t in sorted_trend)
    # Busiest day.
    busiest = max(sorted_trend, key=lambda t: t['created']) if sorted_trend else None
    return {
        'days': days,
        'total_created': total_created,
        'total_closed': total_closed,
        'avg_per_day_created': round(total_created / max(1, days), 2),
        'avg_per_day_closed': round(total_closed / max(1, days), 2),
        'busiest_day': busiest,
        'trend': sorted_trend,
    }


def build_response_distribution(pdb: PremiumDB, guild_id: int) -> Dict:
    '''Distribution of first-response times across all tickets.

    Returns buckets: <5min, 5-15min, 15-60min, 1-4h, 4-24h, >24h.
    Useful for SLA tuning.
    '''
    conn = pdb.dm._connection
    rows = _fetchall(conn,
        '''SELECT created_at, first_response_at, staff_responded_at
           FROM tickets WHERE guild_id = ? AND first_response_at IS NOT NULL''',
        (guild_id,))
    buckets = {'<5min': 0, '5-15min': 0, '15-60min': 0,
               '1-4h': 0, '4-24h': 0, '>24h': 0}
    for r in rows:
        created = _parse_iso(r.get('created_at'))
        fr = _parse_iso(r.get('first_response_at') or r.get('staff_responded_at'))
        if not (created and fr):
            continue
        mins = (fr - created).total_seconds() / 60.0
        if mins < 5: buckets['<5min'] += 1
        elif mins < 15: buckets['5-15min'] += 1
        elif mins < 60: buckets['15-60min'] += 1
        elif mins < 240: buckets['1-4h'] += 1
        elif mins < 1440: buckets['4-24h'] += 1
        else: buckets['>24h'] += 1
    return buckets
