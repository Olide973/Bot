"""Envoi de messages Telegram pour les notifications du bot.

Le bot envoie à un ou plusieurs destinataires :
- TELEGRAM_CHAT_ID : ton chat privé (toujours actif)
- TELEGRAM_GROUP_CHAT_ID : un groupe/chaîne optionnel, en plus, si configuré

Ajouter un groupe ne demande AUCUN accès à la config du bot pour ses membres :
ils reçoivent seulement les messages que le bot poste, rien d'autre.
"""

import os
import logging
import requests

logger = logging.getLogger("telegram_notifier")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_GROUP_CHAT_ID = os.environ.get("TELEGRAM_GROUP_CHAT_ID")  # optionnel


def _send_to_chat(chat_id: str, message: str):
    """Envoie un message texte (Markdown) à un chat_id donné."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Erreur envoi Telegram vers {chat_id} : {e}")


def send_telegram(message: str):
    """Envoie un message vers le chat privé, et vers le groupe si configuré."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant, message non envoyé.")
        return

    _send_to_chat(TELEGRAM_CHAT_ID, message)

    if TELEGRAM_GROUP_CHAT_ID:
        _send_to_chat(TELEGRAM_GROUP_CHAT_ID, message)
