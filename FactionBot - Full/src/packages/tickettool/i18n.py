# -*- coding: utf-8 -*-
'''
TicketTool.i18n — Localization (Tier 2 Feature #28).

Per-guild language selection for ticket-system strings. Ships with built-in
translations for English, Spanish, French, and German. Server owners can
override any individual string with /locale string.

The strings that get localized are the ones the premium package itself emits
(ticket opened, closed, claimed, schedule-unavailable, etc.). The existing
Bot.py strings remain in English (to avoid touching the working code); the
premium hooks check the guild's locale and use translated strings where
available.

Built-in languages:
  en — English (default)
  es — Spanish
  fr — French
  de — German

Adding a language = add an entry to STRINGS below. No code changes needed.
'''

from __future__ import annotations

import logging
from typing import Dict, Optional

from .db import PremiumDB


# =====================================================================
# BUILT-IN TRANSLATIONS
# =====================================================================

# Each string key maps to {lang: text}. Missing languages fall back to 'en'.
STRINGS: Dict[str, Dict[str, str]] = {
    # Ticket lifecycle
    'ticket.opened': {
        'en': "Ticket opened",
        'es': "Ticket abierto",
        'fr': "Ticket ouvert",
        'de': "Ticket eröffnet",
    },
    'ticket.closed': {
        'en': "Ticket closed",
        'es': "Ticket cerrado",
        'fr': "Ticket fermé",
        'de': "Ticket geschlossen",
    },
    'ticket.claimed': {
        'en': "Ticket claimed",
        'es': "Ticket reclamado",
        'fr': "Ticket pris en charge",
        'de': "Ticket übernommen",
    },
    'ticket.unclaimed': {
        'en': "Ticket unclaimed",
        'es': "Ticket liberado",
        'fr': "Ticket libéré",
        'de': "Ticket freigegeben",
    },
    'ticket.escalated': {
        'en': "Ticket escalated",
        'es': "Ticket escalado",
        'fr': "Ticket escaladé",
        'de': "Ticket eskaliert",
    },
    'ticket.reopened': {
        'en': "Ticket reopened",
        'es': "Ticket reabierto",
        'fr': "Ticket rouvert",
        'de': "Ticket wiedereröffnet",
    },
    # Schedule
    'schedule.unavailable': {
        'en': "Tickets are currently unavailable for this panel.",
        'es': "Los tickets no están disponibles para este panel en este momento.",
        'fr': "Les tickets ne sont actuellement pas disponibles pour ce panneau.",
        'de': "Tickets sind für dieses Panel derzeit nicht verfügbar.",
    },
    # SLA
    'sla.warning': {
        'en': "⏰ **SLA Warning** — the SLA window is almost over.",
        'es': "⏰ **Advertencia SLA** — la ventana SLA está casi terminada.",
        'fr': "⏰ **Avertissement SLA** — la fenêtre SLA est presque terminée.",
        'de': "⏰ **SLA-Warnung** — das SLA-Fenster ist fast abgelaufen.",
    },
    'sla.breach': {
        'en': "🚨 **SLA Breach** — please respond immediately.",
        'es': "🚨 **Infracción SLA** — por favor responda inmediatamente.",
        'fr': "🚨 **Infraction SLA** — veuillez répondre immédiatement.",
        'de': "🚨 **SLA-Verstoß** — bitte sofort antworten.",
    },
    # Claiming
    'claim.only_claimer_unclaim': {
        'en': "Only the claimer can unclaim this ticket (or an admin).",
        'es': "Solo el reclamante puede liberar este ticket (o un admin).",
        'fr': "Seul le claimer peut libérer ce ticket (ou un admin).",
        'de': "Nur der Übernehmer kann dieses Ticket freigeben (oder ein Admin).",
    },
    # KB
    'kb.no_results': {
        'en': "No matching articles found.",
        'es': "No se encontraron artículos.",
        'fr': "Aucun article trouvé.",
        'de': "Keine Artikel gefunden.",
    },
    'kb.suggestion_intro': {
        'en': "📚 While you wait, these articles might help:",
        'es': "📚 Mientras esperas, estos artículos pueden ayudar:",
        'fr': "📚 En attendant, ces articles pourraient aider :",
        'de': "📚 Während Sie warten, könnten diese Artikel helfen:",
    },
    # Branded replies
    'branded.staff_label': {
        'en': "Support",
        'es': "Soporte",
        'fr': "Support",
        'de': "Support",
    },
    # Flow builder
    'flow.step_prompt': {
        'en': "Please answer the following question:",
        'es': "Por favor responda la siguiente pregunta:",
        'fr': "Veuillez répondre à la question suivante :",
        'de': "Bitte beantworten Sie die folgende Frage:",
    },
    'flow.completed': {
        'en': "✅ All questions answered. A staff member will be with you shortly.",
        'es': "✅ Todas las preguntas respondidas. Un miembro del personal te atenderá pronto.",
        'fr': "✅ Toutes les questions répondues. Un membre du personnel vous aidera bientôt.",
        'de': "✅ Alle Fragen beantwortet. Ein Mitarbeiter wird gleich bei Ihnen sein.",
    },
}


SUPPORTED_LANGUAGES = {
    'en': '🇺🇸 English',
    'es': '🇪🇸 Español',
    'fr': '🇫🇷 Français',
    'de': '🇩🇪 Deutsch',
}


# =====================================================================
# PUBLIC API
# =====================================================================

def get_language(pdb: PremiumDB, guild_id: int) -> str:
    loc = pdb.get_locale(guild_id)
    lang = loc.get('language', 'en')
    return lang if lang in SUPPORTED_LANGUAGES else 'en'


def set_language(pdb: PremiumDB, guild_id: int, language: str,
                 timezone: Optional[str] = None) -> bool:
    if language not in SUPPORTED_LANGUAGES:
        return False
    loc = pdb.get_locale(guild_id)
    tz = timezone or loc.get('timezone', 'UTC')
    pdb.upsert_locale(guild_id, language, tz)
    return True


def get_timezone(pdb: PremiumDB, guild_id: int) -> str:
    return pdb.get_locale(guild_id).get('timezone', 'UTC')


def t(pdb: PremiumDB, guild_id: int, key: str, **kwargs) -> str:
    '''Translate a string for the guild's language.

    Falls back to English if the key/language is missing. Supports {var}
    substitution via kwargs.
    '''
    lang = get_language(pdb, guild_id)
    # Custom override first.
    custom = pdb.get_custom_string(guild_id, key)
    if custom:
        text = custom
    else:
        translations = STRINGS.get(key)
        if not translations:
            return key  # unknown key — return the key itself as a safe fallback
        text = translations.get(lang) or translations.get('en') or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass
    return text


# =====================================================================
# CUSTOM STRING OVERRIDES
# =====================================================================

def set_custom_string(pdb: PremiumDB, guild_id: int, key: str, value: str) -> None:
    pdb.set_custom_string(guild_id, key, value)


def delete_custom_string(pdb: PremiumDB, guild_id: int, key: str) -> bool:
    return pdb.delete_custom_string(guild_id, key)


def list_custom_strings(pdb: PremiumDB, guild_id: int):
    return pdb.list_custom_strings(guild_id)


def list_available_strings() -> Dict[str, Dict[str, str]]:
    '''Return all string keys with their available translations (for /locale list).'''
    return STRINGS
