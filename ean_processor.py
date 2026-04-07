"""
ean_processor.py — Komunikacja z webhookiem Power Automate + OpenLibrary fallback.
Wysyła EAN-y batchami i zbiera mapowanie EAN → lista URL-i.
"""

from __future__ import annotations

import io
import logging
import time
from typing import Callable, Optional

import httpx
from PIL import Image

from config import HTTP_TIMEOUT

logger = logging.getLogger(__name__)


def _send_batch(
    eans: list[str],
    webhook_url: str,
    client: httpx.Client,
    retry: int = 2,
) -> dict[str, dict]:
    """
    Wysyła jeden batch EAN-ów do webhooka i zwraca dict {ean: {"urls": [...], "name": "..."}}.
    Payload:  {"eans": ["1234...", ...]}
    Oczekiwana odpowiedź:
      {"results": {"1234...": {"urls": [...], "name": "..."}, ...}}
    """
    payload = {"eans": eans}

    for attempt in range(retry + 1):
        try:
            resp = client.post(webhook_url, json=payload, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
        except httpx.TimeoutException:
            logger.warning("Timeout przy próbie %d/%d (batch=%d EAN-ów)", attempt + 1, retry + 1, len(eans))
            if attempt < retry:
                time.sleep(2 ** attempt)
                continue
            raise
        except httpx.HTTPStatusError as exc:
            logger.error("HTTP %s dla webhooka: %s", exc.response.status_code, exc.response.text[:200])
            raise
        except httpx.RequestError as exc:
            logger.error("Błąd sieci: %s", exc)
            if attempt < retry:
                time.sleep(2 ** attempt)
                continue
            raise

        data = resp.json()
        raw = data.get("results", data)
        return _normalize_results(raw)

    return {}  # nigdy tu nie dotrzemy, ale mypy jest szczęśliwy


def _normalize_results(raw: dict) -> dict[str, dict]:
    """
    Normalizuje odpowiedź webhooka do jednolitego formatu:
      {ean: {"urls": [...], "name": "..."}}

    Obsługuje stary format (lista URL-i) oraz nowy (dict z urls i name).
    """
    normalized: dict[str, dict] = {}
    for ean, value in raw.items():
        if isinstance(value, list):
            # Stary format: ["url1", "url2"]
            normalized[ean] = {"urls": value, "name": ""}
        elif isinstance(value, dict):
            # Nowy format: {"urls": [...], "name": "..."}
            normalized[ean] = {
                "urls": value.get("urls", []),
                "name": value.get("name", ""),
            }
        else:
            normalized[ean] = {"urls": [], "name": ""}
    return normalized


def check_openlibrary_cover(ean: str, timeout: float = 10.0) -> Optional[str]:
    """
    Sprawdza czy grafika istnieje w OpenLibrary dla podanego EAN/ISBN.
    Pobiera i weryfikuje faktyczny obraz (tylko rozmiar L - large).
    
    Args:
        ean: Kod EAN/ISBN (8, 13 lub 14 cyfr)
        timeout: Timeout żądania HTTP w sekundach
        
    Returns:
        URL grafiki jeśli istnieje, None w przeciwnym razie
    """
    # OpenLibrary akceptuje ISBN-10 i ISBN-13
    # Jeśli EAN ma 14 cyfr, obcinamy pierwszą cyfrę (prefix)
    isbn = ean
    if len(ean) == 14:
        isbn = ean[1:]
    
    # Tylko rozmiar L (large) - najwyższa jakość
    url = f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg"
    
    logger.debug(f"Sprawdzam OpenLibrary dla EAN: {ean} (ISBN: {isbn}) - {url}")
    
    try:
        # Pobierz obraz z nagłówkami przeglądarki
        response = httpx.get(
            url, 
            timeout=timeout, 
            follow_redirects=True,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
                'Accept': 'image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8',
                'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
            }
        )
        
        if response.status_code != 200:
            logger.debug(f"OpenLibrary {url} returned {response.status_code}")
            return None
        
        # Sprawdź Content-Type
        content_type = response.headers.get('Content-Type', '').lower()
        if 'image' not in content_type:
            logger.debug(f"OpenLibrary {url} - nieprawidłowy Content-Type: {content_type}")
            return None
        
        # Pobierz zawartość
        content = response.content
        content_size = len(content)
        
        # OpenLibrary placeholder ma ~368 bajtów (stary) lub ~2.8KB (nowy)
        # Prawdziwe okładki są większe (zazwyczaj > 5KB)
        if content_size < 5000:
            logger.debug(
                f"OpenLibrary {url} - plik za mały ({content_size} bytes), "
                f"prawdopodobnie placeholder"
            )
            return None
        
        # Weryfikuj że to prawidłowy obraz i sprawdź rozdzielczość
        try:
            img = Image.open(io.BytesIO(content))
            width, height = img.size
            img_format = img.format or "Unknown"
            
            # OpenLibrary placeholder może mieć rozdzielczość 1×1 lub bardzo małą
            if width < 50 or height < 50:
                logger.debug(
                    f"OpenLibrary {url} - obraz za mały ({width}×{height}px), "
                    f"prawdopodobnie placeholder"
                )
                return None
            
            logger.info(
                f"✓ OpenLibrary cover found for {ean}: {url} "
                f"({content_size} bytes, {width}×{height}px, {img_format})"
            )
            return url
            
        except Exception as img_err:
            logger.debug(f"OpenLibrary {url} - błąd weryfikacji obrazu: {img_err}")
            return None
            
    except httpx.TimeoutException:
        logger.debug(f"OpenLibrary timeout for {ean}")
        return None
    except httpx.RequestError as e:
        logger.debug(f"OpenLibrary request error for {ean}: {e}")
        return None
    except Exception as e:
        logger.warning(f"OpenLibrary unexpected error for {ean}: {e}")
        return None


def fetch_ean_urls_batch(
    eans: list[str],
    webhook_url: str,
    batch_size: int = 50,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict[str, dict]:
    """
    Wysyła wszystkie EAN-y batchami, agreguje wyniki.

    Args:
        eans:              Lista kodów EAN.
        webhook_url:       URL webhooka Power Automate.
        batch_size:        Ile EAN-ów na jedno żądanie POST.
        progress_callback: Opcjonalna funkcja (done, total) do raportowania postępu.

    Returns:
        Słownik {ean: {"urls": [url1, ...], "name": "Nazwa produktu"}}.
        EAN-y bez grafik mają pustą listę urls.
    """
    result: dict[str, dict] = {}
    total_batches = (len(eans) + batch_size - 1) // batch_size

    with httpx.Client(follow_redirects=True) as client:
        for batch_idx in range(total_batches):
            batch = eans[batch_idx * batch_size : (batch_idx + 1) * batch_size]
            logger.info("Batch %d/%d — %d EAN-ów", batch_idx + 1, total_batches, len(batch))

            try:
                batch_result = _send_batch(batch, webhook_url, client)
                result.update(batch_result)
            except Exception as exc:
                logger.error("Batch %d nieudany: %s", batch_idx + 1, exc)
                for ean in batch:
                    result.setdefault(ean, {"urls": [], "name": ""})

            if progress_callback:
                progress_callback(batch_idx + 1, total_batches)

    # Upewnij się, że każdy EAN ma wpis
    for ean in eans:
        result.setdefault(ean, {"urls": [], "name": ""})

    return result