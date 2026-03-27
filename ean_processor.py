"""
ean_processor.py — Komunikacja z webhookiem Power Automate.
Wysyła EAN-y batchami i zbiera mapowanie EAN → lista URL-i.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import httpx

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
