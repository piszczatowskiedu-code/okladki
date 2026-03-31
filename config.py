"""
config.py — Centralna konfiguracja aplikacji EAN Image Manager.
Wartości są odczytywane w kolejności:
  1. Streamlit Secrets (st.secrets) — dla deployment na Streamlit Cloud
  2. Zmienne środowiskowe (os.getenv) — dla lokalnego developmentu
  3. Wartości domyślne
"""

import os


def _get(key: str, default: str = "") -> str:
    """Odczytuje wartość z st.secrets, potem z os.getenv, potem default."""
    try:
        import streamlit as st
        val = st.secrets.get(key)
        if val:
            return str(val)
    except Exception:
        pass
    return os.getenv(key, default)


# ── Webhooks Power Automate ────────────────────────────────────────────────
WEBHOOK_URL_FETCH: str = _get("WEBHOOK_URL_FETCH")
WEBHOOK_URL_ONEDRIVE: str = _get("WEBHOOK_URL_ONEDRIVE")

# ── Przetwarzanie ──────────────────────────────────────────────────────────
BATCH_SIZE: int = int(_get("BATCH_SIZE", "25"))
MAX_WORKERS: int = int(_get("MAX_WORKERS", "10"))
IMAGE_TIMEOUT: float = float(_get("IMAGE_TIMEOUT", "15"))
MAX_IMAGE_MB: float = float(_get("MAX_IMAGE_MB", "20"))
HTTP_TIMEOUT: float = float(_get("HTTP_TIMEOUT", "120"))

# ── Limity ─────────────────────────────────────────────────────────────────
MAX_EANS_TOTAL: int = int(_get("MAX_EANS_TOTAL", "5000"))

# ── UI ─────────────────────────────────────────────────────────────────────
PAGE_TITLE: str = "EAN Image Manager"