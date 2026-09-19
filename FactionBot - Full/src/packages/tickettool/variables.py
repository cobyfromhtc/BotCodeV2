# -*- coding: utf-8 -*-
'''
TicketTool.variables — Ticket Tool-style variable engine.

Supports variables like:
    {ticket.id}          -> short ticket id
    {ticket.count}       -> guild-wide ticket counter (padded)
    {ticket.user}        -> creator display name
    {ticket.owner}       -> creator mention
    {ticket.subject}
    {ticket.priority}
    {ticket.category}
    {claim.user}         -> claimer display name
    {claim.id}           -> claimer user id
    {panel.name}
    {panel.id}
    {guild.name}
    {user.name}          -> the acting user (e.g. the closer)
    {user.mention}
    {random.number:min:max}  -> random int in [min, max]
    {random.choice:a,b,c}    -> random item
    {date}               -> YYYY-MM-DD
    {time}               -> HH:MM:SS
    {datetime}           -> YYYY-MM-DD HH:MM:SS

Variable MODIFIERS (pipe syntax, like Ticket Tool):
    {ticket.user|lower}
    {ticket.user|upper}
    {ticket.user|capitalize}
    {ticket.count|pad:4}        -> zero-pad to 4 digits (#0057)
    {ticket.user|truncate:20}   -> truncate to N chars

The engine is pure (no discord imports) so it can be unit-tested and used
anywhere (naming templates, automation messages, transcript messages).
'''

from __future__ import annotations

import json
import random as _random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional


# Matches {var.path|mod:arg|mod2:arg2} where the modifier chain is optional.
_VAR_RE = re.compile(r'\{([a-zA-Z0-9_.]+)((?:\|[a-zA-Z0-9_]+(?::[^}|]*)?)*)\}')


# =====================================================================
# CONTEXT
# =====================================================================

class VariableContext:
    '''Bundles everything a template render might need.

    All fields optional — a template that only references {ticket.user} works
    fine with everything else None.
    '''

    def __init__(
        self,
        *,
        ticket: Optional[Dict] = None,
        ticket_count: Optional[int] = None,
        panel: Optional[Dict] = None,
        claim_user: Optional[Dict] = None,   # {'id','name','mention'}
        acting_user: Optional[Dict] = None, # the closer / claimer / etc.
        guild: Optional[Dict] = None,       # {'id','name'}
        extra: Optional[Dict[str, Any]] = None,
        tz_offset_hours: float = 0.0,
    ):
        self.ticket = ticket or {}
        self.ticket_count = ticket_count
        self.panel = panel or {}
        self.claim_user = claim_user or {}
        self.acting_user = acting_user or {}
        self.guild = guild or {}
        self.extra = extra or {}
        self.tz_offset_hours = tz_offset_hours


# =====================================================================
# VALUE RESOLVERS
# =====================================================================

def _safe_str(value) -> str:
    if value is None:
        return ''
    return str(value)


def _lookup_ticket(ctx: VariableContext, subpath: str) -> str:
    t = ctx.ticket or {}
    if subpath in ('id',):
        return _safe_str(t.get('ticket_id'))
    if subpath in ('count', 'number'):
        return _safe_str(ctx.ticket_count)
    if subpath in ('user', 'creator'):
        # Prefer a resolved display name if the caller provided one, else fall
        # back to whatever is in the ticket row.
        name = (ctx.extra.get('ticket_creator_name')
                or _safe_str(t.get('creator_name'))
                or _safe_str(t.get('creator_id')))
        return name
    if subpath in ('owner', 'creator_mention'):
        cid = t.get('creator_id')
        return f"<@{cid}>" if cid else ''
    if subpath == 'subject':
        return _safe_str(t.get('subject'))
    if subpath == 'priority':
        return _safe_str(t.get('priority') or 'normal')
    if subpath in ('priority_emoji', 'priority_icon'):
        p = str(t.get('priority') or 'normal').lower()
        return {'low': '🟢', 'normal': '🔵', 'high': '🟠', 'urgent': '🔴'}.get(p, '🔵')
    if subpath in ('category',):
        return _safe_str(t.get('category'))
    if subpath == 'channel_id':
        return _safe_str(t.get('channel_id'))
    if subpath == 'status':
        return _safe_str(t.get('status'))
    if subpath == 'created_at':
        return _safe_str((t.get('created_at') or '')[:19])
    if subpath == 'closed_at':
        return _safe_str((t.get('closed_at') or '')[:19])
    if subpath == 'duration':
        # Human-readable duration since ticket creation
        created_raw = t.get('created_at')
        if not created_raw:
            return ''
        try:
            created = datetime.fromisoformat(str(created_raw).replace('Z', '+00:00'))
            delta = datetime.now(timezone.utc) - created
            total_sec = int(delta.total_seconds())
            h, rem = divmod(total_sec, 3600)
            m, s = divmod(rem, 60)
            if h > 0:
                return f"{h}h {m}m"
            if m > 0:
                return f"{m}m {s}s"
            return f"{s}s"
        except (TypeError, ValueError):
            return ''
    if subpath == 'message_count':
        # Fetched from extra context if the caller provided it
        return _safe_str(ctx.extra.get('ticket_message_count') or t.get('message_count') or '0')
    if subpath == 'claim_count':
        return _safe_str(t.get('claim_count') or '0')
    if subpath == 'is_claimed':
        return 'yes' if t.get('claimed_by') else 'no'
    if subpath == 'is_closed':
        return 'yes' if t.get('status') == 'closed' else 'no'
    if subpath == 'escalation_count':
        return _safe_str(t.get('escalation_count') or '0')
    if subpath == 'close_reason':
        return _safe_str(t.get('close_reason') or '')
    if subpath == 'rating':
        return _safe_str(t.get('rating') or '')
    if subpath == 'rating_feedback':
        return _safe_str(t.get('rating_feedback') or '')
    if subpath == 'panel_id':
        return _safe_str(t.get('panel_id') or '')
    # Unknown sub-path -> empty (never raise during render)
    return ''


def _lookup_claim(ctx: VariableContext, subpath: str) -> str:
    c = ctx.claim_user or {}
    cid = ctx.ticket.get('claimed_by') if ctx.ticket else None
    if subpath in ('user', 'name', 'display_name'):
        return c.get('name') or _safe_str(cid)
    if subpath in ('id',):
        return _safe_str(cid)
    if subpath in ('mention',):
        return f"<@{cid}>" if cid else ''
    if subpath in ('at', 'time'):
        return _safe_str((ctx.ticket.get('claimed_at') or '')[:19] if ctx.ticket else '')
    return ''


def _lookup_panel(ctx: VariableContext, subpath: str) -> str:
    p = ctx.panel or {}
    if subpath in ('name',):
        return _safe_str(p.get('name'))
    if subpath in ('id',):
        return _safe_str(p.get('panel_id'))
    if subpath == 'category_id':
        return _safe_str(p.get('category_id'))
    return ''


def _lookup_user(ctx: VariableContext, subpath: str) -> str:
    '''{user.*} = the ACTING user (closer / claimer / note author).'''
    u = ctx.acting_user or {}
    if subpath in ('name', 'display_name'):
        return u.get('name', '')
    if subpath in ('id',):
        return _safe_str(u.get('id'))
    if subpath in ('mention',):
        uid = u.get('id')
        return f"<@{uid}>" if uid else ''
    return ''


def _lookup_guild(ctx: VariableContext, subpath: str) -> str:
    g = ctx.guild or {}
    if subpath in ('name',):
        return g.get('name', '')
    if subpath in ('id',):
        return _safe_str(g.get('id'))
    return ''


def _lookup_random(arg: str) -> str:
    '''{random.number:min:max} or {random.choice:a,b,c}'''
    if arg.startswith('number:'):
        rest = arg[len('number:'):]
        parts = rest.split(':')
        try:
            lo = int(parts[0]) if len(parts) > 0 and parts[0] else 0
            hi = int(parts[1]) if len(parts) > 1 and parts[1] else lo
            return str(_random.randint(lo, hi))
        except ValueError:
            return ''
    if arg.startswith('choice:'):
        rest = arg[len('choice:'):]
        items = [x for x in rest.split(',') if x]
        return _random.choice(items) if items else ''
    return ''


def _lookup_date(ctx: VariableContext, subpath: str) -> str:
    '''{date} / {time} / {datetime} respecting tz_offset_hours.'''
    now = datetime.now(timezone.utc)
    if ctx.tz_offset_hours:
        try:
            from datetime import timedelta
            now = now + timedelta(hours=float(ctx.tz_offset_hours))
        except Exception:
            pass
    if subpath == 'date':
        return now.strftime('%Y-%m-%d')
    if subpath == 'time':
        return now.strftime('%H:%M:%S')
    if subpath in ('datetime', ''):
        return now.strftime('%Y-%m-%d %H:%M:%S')
    return ''


# Root variable lookup: {namespace.path|mods}
def _resolve_root(ctx: VariableContext, root: str, path: str) -> str:
    if root == 'ticket':
        return _lookup_ticket(ctx, path)
    if root == 'claim':
        return _lookup_claim(ctx, path)
    if root == 'panel':
        return _lookup_panel(ctx, path)
    if root == 'user':
        return _lookup_user(ctx, path)
    if root == 'guild':
        return _lookup_guild(ctx, path)
    if root == 'random':
        return _lookup_random(path)
    if root in ('date', 'time', 'datetime'):
        return _lookup_date(ctx, root)
    # Unknown root: check extra context
    if root in ctx.extra:
        val = ctx.extra[root]
        # extra values can be dicts (support {extra.foo.bar})
        if isinstance(val, dict) and path:
            return _safe_str(val.get(path))
        return _safe_str(val)
    return ''


# =====================================================================
# MODIFIERS
# =====================================================================

def _apply_modifier(value: str, mod: str) -> str:
    '''Apply a single |modifier or |modifier:arg to a string value.'''
    if ':' in mod:
        name, _, arg = mod.partition(':')
    else:
        name, arg = mod, ''
    name = name.strip().lower()

    # --- Case modifiers ---
    if name in ('lower',):
        return value.lower()
    if name in ('upper',):
        return value.upper()
    if name in ('capitalize', 'cap'):
        return value[:1].upper() + value[1:]
    if name in ('title',):
        return value.title()
    if name in ('swapcase',):
        return value.swapcase()

    # --- Padding / alignment ---
    if name in ('pad', 'zeropad', 'zfill'):
        try:
            width = int(arg) if arg else 4
            if value.lstrip('-').isdigit():
                return value.zfill(width)
            return value.rjust(width, '0')
        except ValueError:
            return value
    if name in ('lpad', 'padleft'):
        try:
            width = int(arg) if arg else 4
            return value.rjust(width, ' ')
        except ValueError:
            return value
    if name in ('rpad', 'padright'):
        try:
            width = int(arg) if arg else 4
            return value.ljust(width, ' ')
        except ValueError:
            return value
    if name in ('center', 'centre'):
        try:
            width = int(arg) if arg else 4
            return value.center(width, ' ')
        except ValueError:
            return value

    # --- Truncation ---
    if name in ('truncate', 'trunc', 'limit'):
        try:
            width = int(arg) if arg else 50
        except ValueError:
            width = 50
        if len(value) <= width:
            return value
        return value[:max(0, width - 1)] + '…'

    # --- String operations ---
    if name in ('strip', 'trim'):
        return value.strip()
    if name in ('lstrip',):
        return value.lstrip()
    if name in ('rstrip',):
        return value.rstrip()
    if name in ('replace',):
        # {x|replace:a:b}  replace 'a' with 'b'
        try:
            old, new = arg.split(':', 1)
            return value.replace(old, new)
        except ValueError:
            return value
    if name in ('reverse', 'rev'):
        return value[::-1]
    if name in ('repeat', 'rep'):
        try:
            n = int(arg) if arg else 1
            return value * max(0, min(n, 100))
        except ValueError:
            return value
    if name in ('split',):
        # {x|split:sep:idx} — split on sep, take element idx
        try:
            parts = arg.split(':', 1)
            sep = parts[0] if parts else ','
            idx = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            return value.split(sep)[idx]
        except (ValueError, IndexError):
            return value
    if name in ('join',):
        # {x|join:sep} — split on comma, join with sep
        try:
            sep = arg if arg else ', '
            return sep.join(v.strip() for v in value.split(','))
        except Exception:
            return value
    if name in ('count',):
        # {x|count:substr} — count occurrences of substr
        return str(value.count(arg)) if arg else str(len(value))
    if name in ('len', 'length'):
        return str(len(value))

    # --- Conditional ---
    if name in ('default', 'or'):
        return value if value else arg
    if name in ('ifempty',):
        # {x|ifempty:replacement} — if value is empty, use replacement
        return arg if not value else value
    if name in ('ifnotempty',):
        # {x|ifnotempty:suffix} — if value is non-empty, append suffix
        return (value + arg) if value else ''
    if name in ('prefix',):
        return (arg + value) if value else value
    if name in ('suffix',):
        return (value + arg) if value else value

    # --- Numeric operations ---
    if name in ('abs',):
        try:
            return str(abs(float(value)))
        except ValueError:
            return value
    if name in ('add',):
        try:
            return str(float(value) + float(arg))
        except (ValueError, TypeError):
            return value
    if name in ('sub',):
        try:
            return str(float(value) - float(arg))
        except (ValueError, TypeError):
            return value
    if name in ('mul',):
        try:
            return str(float(value) * float(arg))
        except (ValueError, TypeError):
            return value
    if name in ('div',):
        try:
            d = float(arg)
            return str(float(value) / d) if d != 0 else value
        except (ValueError, TypeError, ZeroDivisionError):
            return value
    if name in ('round',):
        try:
            n = int(arg) if arg else 0
            return str(round(float(value), n))
        except (ValueError, TypeError):
            return value

    # --- Date/time modifiers ---
    if name in ('ago',):
        # Interpret value as an ISO timestamp and return "Xh Ym ago"
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            delta = datetime.now(timezone.utc) - dt
            total_sec = int(delta.total_seconds())
            if total_sec < 0:
                total_sec = 0
            d, rem = divmod(total_sec, 86400)
            h, rem = divmod(rem, 3600)
            m, s = divmod(rem, 60)
            if d > 0:
                return f"{d}d {h}h ago"
            if h > 0:
                return f"{h}h {m}m ago"
            if m > 0:
                return f"{m}m ago"
            return f"{s}s ago"
        except (TypeError, ValueError):
            return value
    if name in ('date',):
        # Format an ISO timestamp as a date
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            fmt = arg if arg else '%Y-%m-%d'
            return dt.strftime(fmt)
        except (TypeError, ValueError):
            return value
    if name in ('time',):
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            fmt = arg if arg else '%H:%M:%S'
            return dt.strftime(fmt)
        except (TypeError, ValueError):
            return value

    # --- Encoding / escaping ---
    if name in ('urlencode', 'url'):
        try:
            from urllib.parse import quote as _quote
            return _quote(value, safe='')
        except Exception:
            return value
    if name in ('escape', 'esc', 'html_escape'):
        import html as _html
        return _html.escape(value, quote=True)
    if name in ('json',):
        try:
            return json.dumps(value)
        except Exception:
            return value
    if name in ('hash',):
        import hashlib as _hashlib
        return _hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]
    if name in ('base64', 'b64'):
        try:
            import base64 as _b64
            return _b64.b64encode(value.encode('utf-8')).decode('ascii')
        except Exception:
            return value

    return value


def _apply_modifier_chain(value: str, chain: str) -> str:
    '''chain looks like '|lower|pad:4' (leading |, zero or more modifiers).'''
    if not chain:
        return value
    # split on | but keep empty-string-safe; first element is '' (before the |)
    parts = chain.split('|')
    for mod in parts[1:]:  # skip the empty first element
        if mod:
            value = _apply_modifier(value, mod)
    return value


# =====================================================================
# PUBLIC RENDER ENTRY POINT
# =====================================================================

def render(template: Optional[str], ctx: VariableContext) -> str:
    '''Render a template string against a VariableContext.

    Unknown variables resolve to '' (never raise). Modifier chains apply
    left-to-right. A None template returns ''.
    '''
    if not template:
        return ''
    def _repl(match: 're.Match') -> str:
        full_root = match.group(1)            # e.g. 'ticket.user'
        mod_chain = match.group(2) or ''      # e.g. '|lower|pad:4'
        # Split root into namespace + path. 'ticket.user' -> ('ticket','user')
        if '.' in full_root:
            root, _, path = full_root.partition('.')
        else:
            root, path = full_root, ''
        value = _resolve_root(ctx, root, path)
        return _apply_modifier_chain(value, mod_chain)
    return _VAR_RE.sub(_repl, template)


def list_variables(template: Optional[str]) -> list:
    '''Return the list of distinct {var.path} tokens found in a template.

    Useful for documentation / showing the user which variables their template
    references.
    '''
    if not template:
        return []
    seen = []
    for m in _VAR_RE.finditer(template):
        token = '{' + m.group(0)[1:]  # reconstruct without the modifiers
        # m.group(0) is the whole match including mods; rebuild plain token
        plain = '{' + m.group(1) + '}'
        if plain not in seen:
            seen.append(plain)
    return seen
