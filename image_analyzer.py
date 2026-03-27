"""
image_analyzer.py — Pobieranie i analiza grafik.
Używa wątków do równoległego przetwarzania wielu URL-i.
Zabezpieczony przed wykryciem: curl_cffi (TLS fingerprint Chrome)
+ fallback na httpx z rotacją nagłówków.
"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from PIL import Image, UnidentifiedImageError

from config import IMAGE_TIMEOUT, MAX_IMAGE_MB, MAX_WORKERS

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = int(MAX_IMAGE_MB * 1024 * 1024)

# ── Sprawdź dostępność curl_cffi (impersonacja TLS Chrome) ─────────────────

try:
    from curl_cffi import requests as curl_requests

    CURL_CFFI_AVAILABLE = True
    logger.info("curl_cffi dostępny — TLS fingerprint Chrome aktywny")
except ImportError:
    CURL_CFFI_AVAILABLE = False
    logger.warning(
        "curl_cffi niedostępny — fallback na httpx. "
        "Dla lepszej kompatybilności: pip install curl_cffi"
    )

# Fallback: httpx
import httpx

try:
    import h2  # noqa: F401

    HTTP2_AVAILABLE = True
except ImportError:
    HTTP2_AVAILABLE = False

# Dozwolone rozszerzenia graficzne
ALLOWED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".avif",
}
ALLOWED_CONTENT_TYPES = {
    "image/jpeg", "image/png", "image/webp",
    "image/gif", "image/bmp", "image/tiff",
    "image/avif", "image/svg+xml",
}

# ── Profile przeglądarek do impersonacji curl_cffi ──────────────────────────

_CURL_IMPERSONATE_PROFILES = [
    "chrome120",
    "chrome124",
    "chrome131",
    "chrome133a",
]

# ── Rotacja User-Agent (dla httpx fallback) ─────────────────────────────────

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
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
        "Gecko/20100101 Firefox/128.0"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.5 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
]

_ACCEPT_LANGUAGE_VARIANTS = [
    "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "pl-PL,pl;q=0.9,en;q=0.8",
    "en-US,en;q=0.9,pl-PL;q=0.8,pl;q=0.7",
]

_SEC_CH_UA_VARIANTS = [
    '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="8"',
    '"Chromium";v="125", "Google Chrome";v="125", "Not-A.Brand";v="8"',
    '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="24"',
]


# ── Inteligentny Referer ───────────────────────────────────────────────────

def _smart_referer(url: str) -> str:
    """
    Generuje naturalny Referer na podstawie domeny obrazu.
    img.example.com → https://example.com/
    cdn.example.com → https://example.com/
    images.example.co.uk → https://example.co.uk/
    """
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname or ""

        # Usuń subdomeny typu img., cdn., images., static., media.
        parts = hostname.split(".")
        if len(parts) > 2 and parts[0] in (
            "img", "cdn", "images", "static", "media",
            "assets", "content", "files", "pics", "photos",
        ):
            # img.tantis.pl → tantis.pl
            main_domain = ".".join(parts[1:])
        else:
            main_domain = hostname

        return f"https://{main_domain}/"
    except Exception:
        return "https://www.google.com/"


# ── Nagłówki przeglądarki ──────────────────────────────────────────────────

def _browser_headers(url: str = "") -> dict[str, str]:
    """Generuje nagłówki imitujące Chrome pobierającą obraz."""
    ua = random.choice(_USER_AGENTS)
    is_chrome = "Chrome" in ua and "Edg" not in ua
    is_firefox = "Firefox" in ua
    is_edge = "Edg" in ua

    headers: dict[str, str] = {
        "User-Agent": ua,
        "Accept-Language": random.choice(_ACCEPT_LANGUAGE_VARIANTS),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    if is_firefox:
        headers["Accept"] = (
            "image/avif,image/webp,image/png,image/svg+xml,"
            "image/*;q=0.8,*/*;q=0.5"
        )
    else:
        headers["Accept"] = (
            "image/avif,image/webp,image/apng,image/svg+xml,"
            "image/*,*/*;q=0.8"
        )

    if is_chrome or is_edge:
        headers["Sec-CH-UA"] = random.choice(_SEC_CH_UA_VARIANTS)
        headers["Sec-CH-UA-Mobile"] = "?0"
        headers["Sec-CH-UA-Platform"] = '"Windows"'
        headers["Sec-Fetch-Dest"] = "image"
        headers["Sec-Fetch-Mode"] = "no-cors"
        headers["Sec-Fetch-Site"] = "cross-site"

    # Referer dopasowany do domeny
    if url:
        headers["Referer"] = _smart_referer(url)

    return headers


def _random_delay(min_ms: int = 50, max_ms: int = 300) -> None:
    """Losowe mikro-opóźnienie."""
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


# ── URL validation ─────────────────────────────────────────────────────────

def validate_url(url: str) -> tuple[bool, str]:
    """Zwraca (ok, powód_błędu)."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Nieprawidłowy URL"

    if parsed.scheme not in ("http", "https"):
        return False, f"Niedozwolony schemat: {parsed.scheme}"

    hostname = parsed.hostname or ""
    if re.match(
        r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)",
        hostname,
    ):
        return False, "Adres lokalny niedozwolony"

    return True, ""


# ══════════════════════════════════════════════════════════════════════════
#  METODA 1: curl_cffi (preferowana — prawidłowy TLS fingerprint)
# ══════════════════════════════════════════════════════════════════════════

def _download_with_curl_cffi(
    url: str, timeout: float = IMAGE_TIMEOUT
) -> tuple[bytes, str]:
    """
    Pobiera obraz przy użyciu curl_cffi z impersonacją Chrome.
    Zwraca (raw_bytes, content_type).
    Rzuca wyjątek przy błędzie.
    """
    profile = random.choice(_CURL_IMPERSONATE_PROFILES)
    headers = {
        "Accept": (
            "image/avif,image/webp,image/apng,image/svg+xml,"
            "image/*,*/*;q=0.8"
        ),
        "Accept-Language": random.choice(_ACCEPT_LANGUAGE_VARIANTS),
        "Referer": _smart_referer(url),
        "Sec-Fetch-Dest": "image",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Site": "cross-site",
    }

    response = curl_requests.get(
        url,
        headers=headers,
        impersonate=profile,
        timeout=timeout,
        allow_redirects=True,
    )

    if response.status_code == 403:
        raise ConnectionRefusedError(f"HTTP 403 (profile={profile})")
    if response.status_code == 404:
        raise FileNotFoundError("HTTP 404")
    if response.status_code >= 400:
        raise ConnectionError(f"HTTP {response.status_code}")

    content_type = (
        response.headers.get("content-type", "")
        .split(";")[0].strip().lower()
    )
    return response.content, content_type


# ══════════════════════════════════════════════════════════════════════════
#  METODA 2: httpx fallback (gdy curl_cffi niedostępny)
# ══════════════════════════════════════════════════════════════════════════

def _download_with_httpx(
    url: str,
    client: httpx.Client,
    timeout: float = IMAGE_TIMEOUT,
) -> tuple[bytes, str]:
    """
    Pobiera obraz przy użyciu httpx (fallback).
    Zwraca (raw_bytes, content_type).
    """
    headers = _browser_headers(url)

    # TYLKO GET — nigdy HEAD (przeglądarki nie robią HEAD dla obrazów)
    with client.stream(
        "GET", url, headers=headers, timeout=timeout
    ) as resp:
        resp.raise_for_status()

        content_type = (
            resp.headers.get("content-type", "")
            .split(";")[0].strip().lower()
        )

        chunks = []
        downloaded = 0
        for chunk in resp.iter_bytes(chunk_size=65536):
            downloaded += len(chunk)
            if downloaded > MAX_IMAGE_BYTES:
                raise ValueError(f"Plik przekracza {MAX_IMAGE_MB} MB")
            chunks.append(chunk)

        return b"".join(chunks), content_type


# ══════════════════════════════════════════════════════════════════════════
#  Ujednolicona funkcja pobierania
# ══════════════════════════════════════════════════════════════════════════

def _download_image(
    url: str,
    httpx_client: Optional[httpx.Client] = None,
    retry_count: int = 2,
) -> tuple[bytes, str]:
    """
    Pobiera obraz — próbuje curl_cffi, potem httpx.
    Retry z rosnącym opóźnieniem przy 403/429.
    Zwraca (raw_bytes, content_type).
    """
    last_error: Optional[Exception] = None

    for attempt in range(retry_count + 1):
        # Opóźnienie przy retry
        if attempt > 0:
            delay = random.uniform(0.5, 2.0) * (attempt + 1)
            logger.debug(
                "Retry %d/%d dla %s (delay=%.1fs)",
                attempt, retry_count, url, delay,
            )
            time.sleep(delay)

        # ── Próba 1: curl_cffi (prawdziwy fingerprint Chrome) ──
        if CURL_CFFI_AVAILABLE:
            try:
                raw_bytes, content_type = _download_with_curl_cffi(url)
                if len(raw_bytes) > MAX_IMAGE_BYTES:
                    raise ValueError(
                        f"Plik za duży ({len(raw_bytes) / 1024 / 1024:.1f} MB)"
                    )
                logger.debug(
                    "curl_cffi OK: %s (%d bytes)", url, len(raw_bytes)
                )
                return raw_bytes, content_type

            except FileNotFoundError:
                raise  # 404 — nie retry

            except ConnectionRefusedError as exc:
                # 403 — retry z innym profilem
                last_error = exc
                logger.info(
                    "curl_cffi 403 dla %s — retry %d/%d",
                    url, attempt + 1, retry_count,
                )
                continue

            except Exception as exc:
                last_error = exc
                logger.debug("curl_cffi error: %s — %s", url, exc)
                # Jeśli curl_cffi zawiedzie, spróbuj httpx
                pass

        # ── Próba 2: httpx fallback ──
        if httpx_client is not None:
            try:
                raw_bytes, content_type = _download_with_httpx(
                    url, httpx_client
                )
                logger.debug(
                    "httpx OK: %s (%d bytes)", url, len(raw_bytes)
                )
                return raw_bytes, content_type

            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                last_error = exc
                if code == 404:
                    raise FileNotFoundError("HTTP 404") from exc
                if code in (403, 429) and attempt < retry_count:
                    continue
                raise

            except Exception as exc:
                last_error = exc
                if attempt < retry_count:
                    continue
                raise

    # Wszystkie próby wyczerpane
    raise last_error or ConnectionError(f"Nie udało się pobrać: {url}")


# ── Single image analysis ─────────────────────────────────────────────────

def _analyze_single(
    ean: str,
    url: str,
    name: str,
    httpx_client: Optional[httpx.Client] = None,
) -> dict[str, Any]:
    """Pobiera i analizuje jeden obraz. Zwraca słownik rekordu."""
    base_record: dict[str, Any] = {
        "ean": ean,
        "name": name,
        "url": url,
        "rozdzielczość": None,
        "rozmiar": None,
        "rozszerzenie": None,
        "status": "błąd",
        "błąd": None,
        "_image_bytes": None,
    }

    # 1. Walidacja URL
    ok, reason = validate_url(url)
    if not ok:
        base_record["błąd"] = f"Zły URL: {reason}"
        return base_record

    # 2. Rozszerzenie z URL
    path = urlparse(url).path
    _, ext = os.path.splitext(path)
    ext = ext.lower()

    try:
        # 3. Pobierz obraz (curl_cffi → httpx fallback)
        raw_bytes, content_type = _download_image(
            url, httpx_client=httpx_client
        )

        # 4. Walidacja content-type
        if (
            content_type
            and content_type not in ALLOWED_CONTENT_TYPES
            and "image" not in content_type
        ):
            base_record["błąd"] = (
                f"Nieprawidłowy content-type: {content_type}"
            )
            return base_record

        # 5. Rozszerzenie (z URL lub content-type)
        if not ext or ext not in ALLOWED_EXTENSIONS:
            guessed = mimetypes.guess_extension(content_type or "") or ""
            ext = guessed if guessed else ext

        # 6. Analiza obrazu przez Pillow
        with Image.open(io.BytesIO(raw_bytes)) as img:
            width, height = img.size
            fmt = img.format or ""

        size_kb = len(raw_bytes) / 1024
        size_str = (
            f"{size_kb:.1f} KB"
            if size_kb < 1024
            else f"{size_kb / 1024:.2f} MB"
        )

        base_record.update({
            "rozdzielczość": f"{width}×{height}",
            "rozmiar": size_str,
            "rozszerzenie": ext.lstrip(".").upper() or fmt or "—",
            "status": "OK",
            "_image_bytes": raw_bytes,
        })

    except FileNotFoundError:
        base_record["błąd"] = "HTTP 404 — nie znaleziono"
    except ValueError as exc:
        base_record["błąd"] = str(exc)
    except httpx.TimeoutException:
        base_record["błąd"] = "Timeout połączenia"
    except httpx.HTTPStatusError as exc:
        base_record["błąd"] = f"HTTP {exc.response.status_code}"
    except httpx.RequestError as exc:
        base_record["błąd"] = f"Błąd sieci: {type(exc).__name__}"
    except UnidentifiedImageError:
        base_record["błąd"] = "Nierozpoznany format obrazu"
    except ConnectionRefusedError:
        base_record["błąd"] = "HTTP 403 — serwer odmówił dostępu"
    except Exception as exc:
        base_record["błąd"] = f"Nieznany błąd: {exc}"
        logger.exception("Nieoczekiwany błąd przy %s", url)

    return base_record


# ── Parallel analysis ──────────────────────────────────────────────────────

def analyze_images_parallel(
    eans: list[str],
    ean_url_map: dict[str, dict],
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> list[dict[str, Any]]:
    """
    Dla każdego EAN pobiera i analizuje wszystkie przypisane URL-e.

    Strategia pobierania:
    1. curl_cffi z impersonacją Chrome (TLS fingerprint)
    2. Fallback na httpx z rotacją nagłówków
    3. Retry z rosnącym opóźnieniem przy 403/429
    4. Interleaving domen — rozkłada obciążenie

    Args:
        eans:              Lista EAN-ów (zachowuje kolejność).
        ean_url_map:       Mapowanie EAN → {"urls": [...], "name": "..."}.
        progress_callback: Opcjonalna funkcja (done, total).

    Returns:
        Lista słowników — jeden rekord na (EAN, URL).
    """
    # Buduj listę zadań
    tasks: list[tuple[str, Optional[str], str]] = []
    for ean in eans:
        entry = ean_url_map.get(ean, {"urls": [], "name": ""})
        urls = entry.get("urls", [])
        name = entry.get("name", "")
        if urls:
            for url in urls:
                tasks.append((ean, url, name))
        else:
            tasks.append((ean, None, name))

    total = len(tasks)
    records: list[dict[str, Any]] = []
    done = 0

    # EAN-y bez URL-i — natychmiast
    no_image_tasks = [t for t in tasks if t[1] is None]
    url_tasks = [t for t in tasks if t[1] is not None]

    for ean, _, name in no_image_tasks:
        records.append({
            "ean": ean,
            "name": name,
            "url": "",
            "rozdzielczość": None,
            "rozmiar": None,
            "rozszerzenie": None,
            "status": "brak obrazu",
            "błąd": None,
            "_image_bytes": None,
        })
        done += 1
        if progress_callback:
            progress_callback(done, total)

    if not url_tasks:
        return records

    # Interleaving domen
    domain_groups: dict[str, list[tuple[str, str, str]]] = {}
    for ean, url, name in url_tasks:
        try:
            domain = urlparse(url).netloc
        except Exception:
            domain = "unknown"
        domain_groups.setdefault(domain, []).append((ean, url, name))

    for domain_task_list in domain_groups.values():
        random.shuffle(domain_task_list)

    interleaved: list[tuple[str, str, str]] = []
    domain_iters = [iter(tl) for tl in domain_groups.values()]
    while domain_iters:
        next_round = []
        for it in domain_iters:
            try:
                interleaved.append(next(it))
            except StopIteration:
                continue
            else:
                next_round.append(it)
        domain_iters = next_round

    # Klient httpx jako fallback (lub jedyna metoda gdy brak curl_cffi)
    httpx_client: Optional[httpx.Client] = None
    try:
        httpx_client = httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(IMAGE_TIMEOUT, connect=10.0),
            http2=HTTP2_AVAILABLE,
            limits=httpx.Limits(
                max_connections=MAX_WORKERS + 2,
                max_keepalive_connections=MAX_WORKERS,
                keepalive_expiry=30.0,
            ),
            headers={},  # Czyste — nagłówki per-request
        )

        effective_workers = min(MAX_WORKERS, max(2, len(domain_groups)))

        with ThreadPoolExecutor(max_workers=effective_workers) as executor:
            future_map = {}
            for task_ean, task_url, task_name in interleaved:
                _random_delay(20, 150)
                future = executor.submit(
                    _analyze_single,
                    task_ean,
                    task_url,
                    task_name,
                    httpx_client,
                )
                future_map[future] = (task_ean, task_url, task_name)

            for future in as_completed(future_map):
                try:
                    rec = future.result()
                except Exception as exc:
                    ean, url, name = future_map[future]
                    rec = {
                        "ean": ean,
                        "name": name,
                        "url": url,
                        "rozdzielczość": None,
                        "rozmiar": None,
                        "rozszerzenie": None,
                        "status": "błąd",
                        "błąd": str(exc),
                        "_image_bytes": None,
                    }
                    logger.exception(
                        "Błąd w wątku dla %s / %s", ean, url
                    )

                records.append(rec)
                done += 1
                if progress_callback:
                    progress_callback(done, total)

    finally:
        if httpx_client is not None:
            httpx_client.close()

    return records