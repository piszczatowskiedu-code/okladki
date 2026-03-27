"""
OneDrive Exporter — wysyła zaakceptowane grafiki do OneDrive przez Power Automate.

Strategia:
1. Używa _image_bytes z analizy (obrazy już pobrane w image_analyzer.py)
2. Fallback: curl_cffi z impersonacją Chrome (TLS fingerprint)
3. Fallback: httpx z nagłówkami przeglądarki
"""

import base64
import logging
import random
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import pandas as pd

# ── curl_cffi (opcjonalny, do fallback download) ──────────────────────────

try:
    from curl_cffi import requests as curl_requests

    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

# ── Constants ──────────────────────────────────────────────────────────────

TIMEOUT_SECONDS = 120
MAX_RETRIES = 2
BATCH_SIZE = 10
DOWNLOAD_TIMEOUT = 30

logger = logging.getLogger(__name__)

# ── Profile przeglądarki (reużywane z image_analyzer) ──────────────────────

_CURL_PROFILES = ["chrome120", "chrome124", "chrome131", "chrome133a"]

_USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
]

_ACCEPT_LANGUAGES = [
    "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "pl-PL,pl;q=0.9,en;q=0.8",
    "en-US,en;q=0.9,pl-PL;q=0.8,pl;q=0.7",
]


def _smart_referer(url: str) -> str:
    """img.example.com → https://example.com/"""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        parts = hostname.split(".")
        if len(parts) > 2 and parts[0] in (
            "img", "cdn", "images", "static", "media",
            "assets", "content", "files", "pics", "photos",
        ):
            main_domain = ".".join(parts[1:])
        else:
            main_domain = hostname
        return f"https://{main_domain}/"
    except Exception:
        return "https://www.google.com/"


def _browser_headers(url: str = "") -> dict[str, str]:
    """Nagłówki imitujące Chrome."""
    return {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": (
            "image/avif,image/webp,image/apng,image/svg+xml,"
            "image/*,*/*;q=0.8"
        ),
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": _smart_referer(url) if url else "https://www.google.com/",
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
        "Connection": "keep-alive",
    }


# ── Stealth download (fallback gdy brak _image_bytes) ──────────────────────

def _download_image_stealth(url: str) -> Optional[bytes]:
    """
    Pobiera obraz z zabezpieczeniem przed 403.
    curl_cffi → httpx z nagłówkami → None.
    """
    # ── Próba 1: curl_cffi ──
    if CURL_CFFI_AVAILABLE:
        for attempt in range(2):
            try:
                profile = random.choice(_CURL_PROFILES)
                response = curl_requests.get(
                    url,
                    headers={
                        "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
                        "Referer": _smart_referer(url),
                        "Sec-Fetch-Dest": "image",
                        "Sec-Fetch-Mode": "no-cors",
                        "Sec-Fetch-Site": "cross-site",
                    },
                    impersonate=profile,
                    timeout=DOWNLOAD_TIMEOUT,
                    allow_redirects=True,
                )
                if response.status_code == 200:
                    return response.content
                if response.status_code in (403, 429) and attempt == 0:
                    time.sleep(random.uniform(0.5, 1.5))
                    continue
                logger.warning(
                    "curl_cffi HTTP %d dla %s", response.status_code, url
                )
                break
            except Exception as exc:
                logger.debug("curl_cffi error: %s — %s", url, exc)
                break

    # ── Próba 2: httpx z nagłówkami ──
    try:
        headers = _browser_headers(url)
        with httpx.Client(
            follow_redirects=True,
            timeout=DOWNLOAD_TIMEOUT,
            headers={},
        ) as client:
            response = client.get(url, headers=headers)
            if response.status_code == 200:
                return response.content
            logger.warning(
                "httpx HTTP %d dla %s", response.status_code, url
            )
    except Exception as exc:
        logger.warning("httpx error pobierając %s: %s", url, exc)

    return None


# ── Image preparation ──────────────────────────────────────────────────────

def get_file_extension(url: str, row: pd.Series = None) -> str:
    """Wyciąga rozszerzenie z URL lub kolumny 'rozszerzenie'."""
    # Najpierw sprawdź kolumnę z analizy
    if row is not None:
        ext_col = row.get("rozszerzenie") or row.get("extension") or ""
        ext_col = str(ext_col).strip().lower()
        if ext_col and ext_col != "—" and ext_col != "none":
            return f".{ext_col}" if not ext_col.startswith(".") else ext_col

    # Fallback: z URL
    path = urlparse(url).path.lower()
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".avif"):
        if path.endswith(ext):
            return ext

    return ".jpg"


def prepare_images_payload(
    df: pd.DataFrame,
    progress_callback: callable = None,
) -> tuple[list[dict[str, str]], int, int]:
    """
    Przygotowuje payload z obrazami jako base64.

    KLUCZOWA OPTYMALIZACJA:
    Używa _image_bytes z DataFrame (już pobrane w image_analyzer.py).
    Nie pobiera obrazów ponownie — zero ryzyka 403.

    Fallback: jeśli _image_bytes brak, pobiera przez curl_cffi.
    """
    images: list[dict[str, str]] = []
    success_count = 0
    error_count = 0
    total = len(df)
    has_image_bytes = "_image_bytes" in df.columns

    for idx, row in df.iterrows():
        ean = str(row.get("ean", ""))
        url = str(row.get("url", ""))

        if not ean:
            error_count += 1
            if progress_callback:
                progress_callback(success_count + error_count, total)
            continue

        raw_bytes: Optional[bytes] = None

        # ── Priorytet 1: _image_bytes z analizy (już pobrane!) ──
        if has_image_bytes:
            cached = row.get("_image_bytes")
            if cached is not None and isinstance(cached, bytes) and len(cached) > 0:
                raw_bytes = cached
                logger.debug("Użyto cached _image_bytes dla EAN %s", ean)

        # ── Priorytet 2: fallback download (curl_cffi → httpx) ──
        if raw_bytes is None and url:
            logger.info(
                "Brak _image_bytes dla EAN %s — pobieranie z %s", ean, url
            )
            raw_bytes = _download_image_stealth(url)

        # ── Konwersja na base64 ──
        if raw_bytes:
            base64_content = base64.b64encode(raw_bytes).decode("utf-8")
            extension = get_file_extension(url, row)

            images.append({
                "fileName": f"{ean}{extension}",
                "fileContent": base64_content,
            })
            success_count += 1
        else:
            logger.warning("Nie udało się uzyskać obrazu dla EAN %s", ean)
            error_count += 1

        if progress_callback:
            progress_callback(success_count + error_count, total)

    return images, success_count, error_count


# ── Batch sending ──────────────────────────────────────────────────────────

def send_batch_to_onedrive(
    images: list[dict[str, str]],
    webhook_url: str,
    client: httpx.Client,
) -> dict[str, Any]:
    """Wysyła batch obrazów do Power Automate webhook."""
    payload = {"images": images}

    response = client.post(
        webhook_url,
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    response.raise_for_status()

    try:
        return response.json()
    except Exception:
        return {"status": "ok", "message": response.text}


# ── Main export function ──────────────────────────────────────────────────

def export_to_onedrive(
    accepted_df: pd.DataFrame,
    webhook_url: str,
    batch_size: int = BATCH_SIZE,
    progress_callback: callable = None,
) -> dict[str, Any]:
    """
    Eksportuje zaakceptowane grafiki do OneDrive przez Power Automate.

    Przepływ:
    1. Konwertuje _image_bytes → base64 (bez ponownego pobierania!)
    2. Jeśli _image_bytes brak → curl_cffi download z impersonacją Chrome
    3. Wysyła paczkami do webhooka Power Automate

    Args:
        accepted_df: DataFrame z kolumnami: ean, url, [_image_bytes].
        webhook_url: URL webhooka Power Automate.
        batch_size: Liczba obrazów w jednym batchu.
        progress_callback: Opcjonalny callback (current, total).

    Returns:
        {'sent': int, 'errors': int, 'batches': int}
    """
    if accepted_df.empty:
        return {"sent": 0, "errors": 0, "batches": 0}

    total_sent = 0
    total_errors = 0
    batches_sent = 0

    # Przygotuj obrazy (z cache _image_bytes lub fallback download)
    logger.info(
        "Przygotowanie %d obrazów do eksportu...", len(accepted_df)
    )

    images, download_ok, download_err = prepare_images_payload(
        accepted_df, progress_callback
    )

    total_errors += download_err
    logger.info(
        "Przygotowano: %d OK, %d błędów (z czego cached: sprawdź logi)",
        download_ok, download_err,
    )

    if not images:
        logger.warning("Brak obrazów do wysłania.")
        return {"sent": 0, "errors": total_errors, "batches": 0}

    # Wysyłaj w batchach do webhooka
    logger.info(
        "Wysyłanie %d obrazów w batchach po %d...",
        len(images), batch_size,
    )

    with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
        for i in range(0, len(images), batch_size):
            batch = images[i : i + batch_size]
            batch_num = i // batch_size + 1

            logger.info(
                "Wysyłanie batcha %d (%d obrazów)...",
                batch_num, len(batch),
            )

            for attempt in range(MAX_RETRIES + 1):
                try:
                    result = send_batch_to_onedrive(
                        batch, webhook_url, client
                    )
                    total_sent += len(batch)
                    batches_sent += 1
                    logger.info(
                        "Batch %d wysłany: %s", batch_num, result
                    )
                    break

                except httpx.TimeoutException as exc:
                    if attempt < MAX_RETRIES:
                        wait = (attempt + 1) * 2
                        logger.warning(
                            "Timeout batch %d, próba %d/%d — czekam %ds",
                            batch_num, attempt + 1, MAX_RETRIES, wait,
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            "Timeout batch %d po %d próbach",
                            batch_num, MAX_RETRIES + 1,
                        )
                        total_errors += len(batch)
                        raise TimeoutError(
                            f"Webhook timeout dla batcha {batch_num}"
                        ) from exc

                except httpx.HTTPStatusError as exc:
                    logger.error(
                        "HTTP %d przy batchu %d: %s",
                        exc.response.status_code, batch_num, exc,
                    )
                    total_errors += len(batch)
                    break  # Nie retry przy 4xx/5xx

                except httpx.RequestError as exc:
                    if attempt < MAX_RETRIES:
                        wait = (attempt + 1) * 2
                        logger.warning(
                            "Connection error batch %d, próba %d/%d",
                            batch_num, attempt + 1, MAX_RETRIES,
                        )
                        time.sleep(wait)
                    else:
                        logger.error(
                            "Connection error batch %d po %d próbach",
                            batch_num, MAX_RETRIES + 1,
                        )
                        total_errors += len(batch)
                        raise ConnectionError(
                            f"Nie można połączyć z webhookiem "
                            f"(batch {batch_num})"
                        ) from exc

    logger.info(
        "Export zakończony: wysłano=%d, błędy=%d, batchy=%d",
        total_sent, total_errors, batches_sent,
    )

    return {
        "sent": total_sent,
        "errors": total_errors,
        "batches": batches_sent,
    }