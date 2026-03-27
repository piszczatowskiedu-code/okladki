"""
demo_data.py — Dane demonstracyjne do testowania aplikacji bez Power Automate.
Używa publicznych obrazów dostępnych w internecie.
"""

from __future__ import annotations

import random
import time

# ── Demo EAN-y ─────────────────────────────────────────────────────────────
DEMO_EANS = [
    "5901234123457",
    "5901234123458",
    "5901234123459",
    "5901234123460",
    "5901234123461",
    "5901234123462",
    "5901234123463",
    "5901234123464",
    "5901234123465",
    "5901234123466",
]

# ── Publiczne obrazy do testów ─────────────────────────────────────────────
# Źródła: Unsplash (wolne), Picsum (wolne), pliki testowe
_PUBLIC_IMAGES = [
    # Picsum Photos — losowe, bezpłatne
    "https://picsum.photos/id/1/800/600.jpg",
    "https://picsum.photos/id/10/1200/900.jpg",
    "https://picsum.photos/id/20/640/480.jpg",
    "https://picsum.photos/id/30/1920/1080.jpg",
    "https://picsum.photos/id/40/400/400.jpg",
    "https://picsum.photos/id/50/800/1200.jpg",
    "https://picsum.photos/id/60/1024/768.jpg",
    "https://picsum.photos/id/70/500/500.jpg",
    "https://picsum.photos/id/80/2048/1536.jpg",
    "https://picsum.photos/id/90/300/200.jpg",
    # Testowe pliki PNG z różnymi rozmiarami
    "https://www.w3schools.com/css/img_5terre.jpg",
    "https://upload.wikimedia.org/wikipedia/commons/thumb/4/47/PNG_transparency_demonstration_1.png/280px-PNG_transparency_demonstration_1.png",
    # Celowo zepsute URL-e do testowania obsługi błędów
    "https://picsum.photos/id/9999999/800/600.jpg",   # 404
    "https://this-domain-does-not-exist-xyz.com/img.jpg",  # brak hosta
]

# ── Przypisanie URL-i do EAN-ów ─────────────────────────────────────────────

def get_demo_ean_url_map(eans: list[str]) -> dict[str, dict]:
    """
    Zwraca demo mapowanie EAN → {"urls": [...], "name": "..."}.

    Scenariusze (dla realizmu):
    - Większość EAN-ów: 1–3 poprawne obrazy
    - ~15%: brak grafik (pusta lista)
    - ~15%: mix poprawnych i błędnych URL-i
    """
    random.seed(42)  # deterministyczne wyniki dla tych samych EAN-ów
    good_urls = _PUBLIC_IMAGES[:12]
    bad_urls  = _PUBLIC_IMAGES[12:]

    _DEMO_NAMES = [
        "Wiedźmin: Ostatnie życzenie",
        "Lalka",
        "Pan Tadeusz",
        "Zbrodnia i kara",
        "Mistrz i Małgorzata",
        "Sto lat samotności",
        "Duma i uprzedzenie",
        "Mały Książę",
        "Władca Pierścieni",
        "Harry Potter i Kamień Filozoficzny",
    ]

    result: dict[str, dict] = {}

    for i, ean in enumerate(eans):
        roll = i % 7  # 0-6 → różne scenariusze
        name = _DEMO_NAMES[i % len(_DEMO_NAMES)]

        if roll == 0:
            result[ean] = {"urls": [], "name": name}
        elif roll == 1:
            result[ean] = {"urls": [good_urls[i % len(good_urls)]], "name": name}
        elif roll == 2:
            result[ean] = {"urls": [
                good_urls[i % len(good_urls)],
                good_urls[(i + 3) % len(good_urls)],
            ], "name": name}
        elif roll == 3:
            result[ean] = {"urls": [
                good_urls[i % len(good_urls)],
                good_urls[(i + 2) % len(good_urls)],
                good_urls[(i + 5) % len(good_urls)],
            ], "name": name}
        elif roll == 4:
            result[ean] = {"urls": [
                good_urls[i % len(good_urls)],
                bad_urls[i % len(bad_urls)],
            ], "name": name}
        elif roll == 5:
            result[ean] = {"urls": [bad_urls[i % len(bad_urls)]], "name": name}
        else:
            result[ean] = {"urls": [good_urls[(i + 4) % len(good_urls)]], "name": name}

    return result


def get_demo_eans_text() -> str:
    """Zwraca demo EAN-y jako tekst gotowy do wklejenia w textarea."""
    return "\n".join(DEMO_EANS)


def simulate_webhook_delay() -> None:
    """Symuluje opóźnienie webhooka (~0.5s)."""
    time.sleep(0.5)
