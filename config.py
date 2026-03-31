"""
config.py — Centralna konfiguracja aplikacji EAN Image Manager.
Ustaw tutaj swoje URL-e webhooków oraz parametry działania.
"""

import os

# ── Webhooks Power Automate ────────────────────────────────────────────────
# Wklej tutaj URL-e webhooków z Power Automate LUB ustaw zmienne środowiskowe.

WEBHOOK_URL_FETCH: str = os.getenv(
    "WEBHOOK_URL_FETCH",
    "https://default83d026fcd3794cb8ae0271954c599a.f0.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/0202453ec601428cb1376e75296a9f07/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=YiFFTfcQAt1FD6XbyOTjVYjoCOTQbil6Z0eeG0Aen10",
)

WEBHOOK_URL_ONEDRIVE: str = os.getenv(
    "WEBHOOK_URL_ONEDRIVE",
    "https://default83d026fcd3794cb8ae0271954c599a.f0.environment.api.powerplatform.com:443/powerautomate/automations/direct/workflows/b34e7ec6ba6d4c5baa43e253587f5218/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0&sig=iccC8vw-Iumo5w_nVGc9oTr2ZnjCY18DXW1k_HwoVek",
)

# ── Przetwarzanie ──────────────────────────────────────────────────────────
BATCH_SIZE: int = int(os.getenv("BATCH_SIZE", "50"))          # EAN-ów na jedno żądanie
MAX_WORKERS: int = int(os.getenv("MAX_WORKERS", "10"))         # wątki do pobierania obrazów
IMAGE_TIMEOUT: float = float(os.getenv("IMAGE_TIMEOUT", "15")) # sekundy timeout HTTP
MAX_IMAGE_MB: float = float(os.getenv("MAX_IMAGE_MB", "20"))   # maksymalny rozmiar obrazu MB
HTTP_TIMEOUT: float = float(os.getenv("HTTP_TIMEOUT", "120"))   # timeout do webhooków

# ── Limity ─────────────────────────────────────────────────────────────────
MAX_EANS_TOTAL: int = int(os.getenv("MAX_EANS_TOTAL", "5000")) # max EAN-ów jednorazowo

# ── UI ─────────────────────────────────────────────────────────────────────
PAGE_TITLE: str = "EAN Image Manager"

# Image optimization defaults
DEFAULT_MAX_RESOLUTION = 2000      # px (szerokość i wysokość)
DEFAULT_MAX_FILE_SIZE_KB = 2048    # 2 MB
DEFAULT_JPEG_QUALITY = 85
DEFAULT_WEBP_QUALITY = 82
DEFAULT_PNG_COMPRESS = 6           # 0-9