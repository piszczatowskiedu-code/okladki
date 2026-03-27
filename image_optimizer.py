"""
image_optimizer.py — Kompresja i optymalizacja grafik.

Wykrywa ciężkie pliki i duże rozdzielczości,
kompresuje/skaluje przed eksportem do OneDrive.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import pandas as pd
from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)


# ── Konfiguracja optymalizacji ─────────────────────────────────────────────

@dataclass
class OptimizationConfig:
    """Parametry kompresji — wszystko konfigurowalne z UI."""

    # Włączenie/wyłączenie
    enabled: bool = True

    # Progi rozdzielczości (px)
    max_width: int = 2000
    max_height: int = 2000

    # Próg rozmiaru pliku (KB)
    max_file_size_kb: int = 2048  # 2 MB

    # Jakość kompresji JPEG (1-100)
    jpeg_quality: int = 85

    # Jakość kompresji WebP (1-100)
    webp_quality: int = 82

    # Jakość kompresji PNG — poziom kompresji (0-9)
    png_compress_level: int = 6

    # Konwersja formatów
    convert_bmp_to_jpeg: bool = True
    convert_tiff_to_jpeg: bool = True
    convert_png_to_jpeg_if_no_alpha: bool = False
    convert_webp_to_png: bool = False  # ← NOWE

    # Format docelowy dla konwersji BMP/TIFF (jpeg/webp)
    conversion_target: str = "jpeg"

    # Wyostrzanie po skalowaniu
    sharpen_after_resize: bool = True

    # Metoda resamplingu
    resample_method: str = "lanczos"

    # Progresywny JPEG
    progressive_jpeg: bool = True

    # Usuwanie metadanych EXIF
    strip_metadata: bool = True

    # Minimalna rozdzielczość — nie kompresuj za małych
    min_width: int = 100
    min_height: int = 100


# Preset-y konfiguracji
PRESETS: dict[str, OptimizationConfig] = {
    "Bez zmian": OptimizationConfig(enabled=False),
    "Lekka optymalizacja": OptimizationConfig(
        max_width=3000,
        max_height=3000,
        max_file_size_kb=5120,
        jpeg_quality=92,
        webp_quality=90,
        strip_metadata=True,
        convert_bmp_to_jpeg=True,
        convert_webp_to_png=False,
    ),
    "Standardowa": OptimizationConfig(
        max_width=2000,
        max_height=2000,
        max_file_size_kb=2048,
        jpeg_quality=85,
        webp_quality=82,
        strip_metadata=True,
        convert_bmp_to_jpeg=True,
        convert_tiff_to_jpeg=True,
        convert_webp_to_png=False,
    ),
    "Agresywna": OptimizationConfig(
        max_width=1200,
        max_height=1200,
        max_file_size_kb=500,
        jpeg_quality=75,
        webp_quality=72,
        strip_metadata=True,
        convert_bmp_to_jpeg=True,
        convert_tiff_to_jpeg=True,
        convert_png_to_jpeg_if_no_alpha=True,
        convert_webp_to_png=True,
    ),
    "E-commerce": OptimizationConfig(
        max_width=1500,
        max_height=1500,
        max_file_size_kb=1024,
        jpeg_quality=82,
        webp_quality=80,
        strip_metadata=True,
        convert_bmp_to_jpeg=True,
        convert_tiff_to_jpeg=True,
        convert_webp_to_png=True,   # ← WebP→PNG włączone
        sharpen_after_resize=True,
        progressive_jpeg=True,
    ),
}

# ── Statystyki optymalizacji ───────────────────────────────────────────────

@dataclass
class OptimizationResult:
    """Wynik optymalizacji pojedynczego obrazu."""

    ean: str = ""
    original_size_bytes: int = 0
    optimized_size_bytes: int = 0
    original_width: int = 0
    original_height: int = 0
    optimized_width: int = 0
    optimized_height: int = 0
    original_format: str = ""
    optimized_format: str = ""
    was_resized: bool = False
    was_compressed: bool = False
    was_converted: bool = False
    was_stripped: bool = False
    skipped: bool = False
    skip_reason: str = ""
    error: Optional[str] = None

    @property
    def size_reduction_pct(self) -> float:
        if self.original_size_bytes == 0:
            return 0.0
        return (
            (1 - self.optimized_size_bytes / self.original_size_bytes) * 100
        )

    @property
    def size_saved_kb(self) -> float:
        return (self.original_size_bytes - self.optimized_size_bytes) / 1024


@dataclass
class OptimizationSummary:
    """Podsumowanie optymalizacji całego batcha."""

    total: int = 0
    optimized: int = 0
    skipped: int = 0
    errors: int = 0
    total_original_kb: float = 0.0
    total_optimized_kb: float = 0.0
    resized_count: int = 0
    compressed_count: int = 0
    converted_count: int = 0
    results: list[OptimizationResult] = field(default_factory=list)

    @property
    def total_saved_kb(self) -> float:
        return self.total_original_kb - self.total_optimized_kb

    @property
    def total_saved_pct(self) -> float:
        if self.total_original_kb == 0:
            return 0.0
        return (1 - self.total_optimized_kb / self.total_original_kb) * 100


# ── Metody resamplingu ─────────────────────────────────────────────────────

_RESAMPLE_METHODS = {
    "lanczos": Image.LANCZOS,
    "bicubic": Image.BICUBIC,
    "bilinear": Image.BILINEAR,
    "nearest": Image.NEAREST,
}


# ── Rdzeń optymalizacji ───────────────────────────────────────────────────

def _needs_optimization(
    img: Image.Image,
    raw_bytes: bytes,
    config: OptimizationConfig,
) -> dict[str, bool]:
    """Sprawdza, które optymalizacje są potrzebne."""
    width, height = img.size
    size_kb = len(raw_bytes) / 1024
    fmt = (img.format or "").upper()

    return {
        "resize": width > config.max_width or height > config.max_height,
        "compress": size_kb > config.max_file_size_kb,
        "convert_bmp": fmt == "BMP" and config.convert_bmp_to_jpeg,
        "convert_tiff": fmt == "TIFF" and config.convert_tiff_to_jpeg,
        "convert_png": (
            fmt == "PNG"
            and config.convert_png_to_jpeg_if_no_alpha
            and not _has_alpha(img)
        ),
        "convert_webp": fmt == "WEBP" and config.convert_webp_to_png,
        "strip": config.strip_metadata,
        "too_small": width < config.min_width or height < config.min_height,
    }


def _has_alpha(img: Image.Image) -> bool:
    """Sprawdza czy obraz używa kanału alpha (przezroczystość)."""
    if img.mode in ("RGBA", "LA", "PA"):
        # Sprawdź czy alpha jest faktycznie używany
        if img.mode == "RGBA":
            alpha = img.getchannel("A")
            extrema = alpha.getextrema()
            # Jeśli min alpha < 255, obraz ma przezroczystość
            return extrema[0] < 255
        return True
    return False


def _resize_image(
    img: Image.Image,
    config: OptimizationConfig,
) -> tuple[Image.Image, bool]:
    """Skaluje obraz zachowując proporcje."""
    width, height = img.size

    if width <= config.max_width and height <= config.max_height:
        return img, False

    # Oblicz skalę zachowując proporcje
    ratio_w = config.max_width / width
    ratio_h = config.max_height / height
    ratio = min(ratio_w, ratio_h)

    new_width = int(width * ratio)
    new_height = int(height * ratio)

    resample = _RESAMPLE_METHODS.get(
        config.resample_method, Image.LANCZOS
    )

    resized = img.resize((new_width, new_height), resample)

    # Opcjonalne wyostrzanie po skalowaniu
    if config.sharpen_after_resize:
        resized = resized.filter(ImageFilter.UnsharpMask(
            radius=1.0, percent=30, threshold=2,
        ))

    logger.debug(
        "Resize: %dx%d → %dx%d (ratio=%.2f)",
        width, height, new_width, new_height, ratio,
    )
    return resized, True


def _determine_output_format(
    img: Image.Image,
    original_format: str,
    needs: dict[str, bool],
    config: OptimizationConfig,
) -> str:
    """Określa docelowy format wyjściowy."""
    fmt = original_format.upper()

    # WebP → PNG (priorytet — e-commerce potrzebuje PNG)
    if needs.get("convert_webp"):
        return "PNG"

    # BMP/TIFF/PNG(bez alpha) → JPEG lub WebP
    if (
        needs.get("convert_bmp")
        or needs.get("convert_tiff")
        or needs.get("convert_png")
    ):
        target = config.conversion_target.upper()
        if target == "WEBP":
            return "WEBP"
        return "JPEG"

    # Zachowaj oryginalny format
    if fmt in ("JPEG", "JPG"):
        return "JPEG"
    if fmt == "PNG":
        return "PNG"
    if fmt == "WEBP":
        return "WEBP"
    if fmt == "GIF":
        return "GIF"

    return "JPEG"


def _save_optimized(
    img: Image.Image,
    output_format: str,
    config: OptimizationConfig,
    target_size_kb: Optional[int] = None,
) -> bytes:
    """Zapisuje obraz z optymalnymi parametrami."""
    buffer = io.BytesIO()

    # Konwertuj tryb koloru jeśli potrzeba
    if output_format == "JPEG" and img.mode in ("RGBA", "LA", "P", "PA"):
        # JPEG nie obsługuje przezroczystości — dodaj białe tło
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
        img = background
    elif output_format == "JPEG" and img.mode != "RGB":
        img = img.convert("RGB")

    save_kwargs: dict[str, Any] = {}

    if output_format == "JPEG":
        save_kwargs["quality"] = config.jpeg_quality
        save_kwargs["optimize"] = True
        if config.progressive_jpeg:
            save_kwargs["progressive"] = True
        # Subsampling: 4:2:0 dla mniejszego pliku, 4:4:4 dla jakości
        save_kwargs["subsampling"] = "4:2:0" if config.jpeg_quality < 90 else "4:4:4"

    elif output_format == "PNG":
        save_kwargs["optimize"] = True
        save_kwargs["compress_level"] = config.png_compress_level

    elif output_format == "WEBP":
        save_kwargs["quality"] = config.webp_quality
        save_kwargs["method"] = 4  # 0-6, wyższy = lepsza kompresja ale wolniej

    img.save(buffer, format=output_format, **save_kwargs)
    result = buffer.getvalue()

    # Jeśli nadal za duży i mamy target — iteracyjna redukcja jakości
    if target_size_kb and len(result) / 1024 > target_size_kb:
        result = _iterative_compress(
            img, output_format, target_size_kb, config
        )

    return result


def _iterative_compress(
    img: Image.Image,
    output_format: str,
    target_size_kb: int,
    config: OptimizationConfig,
    min_quality: int = 40,
) -> bytes:
    """Iteracyjnie zmniejsza jakość aż do osiągnięcia target size."""
    if output_format not in ("JPEG", "WEBP"):
        # PNG — nie da się iteracyjnie kompresować jakością
        buffer = io.BytesIO()
        img.save(buffer, format=output_format, optimize=True)
        return buffer.getvalue()

    quality = config.jpeg_quality if output_format == "JPEG" else config.webp_quality
    best_result = None

    for step in range(10):  # Max 10 iteracji
        quality = max(min_quality, quality - 5 * (step + 1))

        buffer = io.BytesIO()
        save_kwargs: dict[str, Any] = {"quality": quality, "optimize": True}

        if output_format == "JPEG":
            if img.mode != "RGB":
                img = img.convert("RGB")
            save_kwargs["progressive"] = config.progressive_jpeg
            save_kwargs["subsampling"] = "4:2:0"

        img.save(buffer, format=output_format, **save_kwargs)
        result = buffer.getvalue()
        best_result = result

        if len(result) / 1024 <= target_size_kb:
            logger.debug(
                "Iterative compress: quality=%d → %.1f KB (target=%d KB)",
                quality, len(result) / 1024, target_size_kb,
            )
            return result

        if quality <= min_quality:
            break

    logger.warning(
        "Nie udało się osiągnąć target %d KB (wynik: %.1f KB, quality=%d)",
        target_size_kb, len(best_result) / 1024, quality,
    )
    return best_result


# ── Format → rozszerzenie pliku ────────────────────────────────────────────

_FORMAT_TO_EXT = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
    "BMP": ".bmp",
    "TIFF": ".tiff",
}


# ── Główna funkcja optymalizacji pojedynczego obrazu ───────────────────────

def optimize_single_image(
    raw_bytes: bytes,
    ean: str = "",
    config: Optional[OptimizationConfig] = None,
) -> tuple[bytes, str, OptimizationResult]:
    """
    Optymalizuje pojedynczy obraz.

    Args:
        raw_bytes: Surowe bajty obrazu.
        ean: Kod EAN (do logów i wyników).
        config: Konfiguracja optymalizacji.

    Returns:
        (optimized_bytes, new_extension, result)
    """
    if config is None:
        config = OptimizationConfig()

    result = OptimizationResult(
        ean=ean,
        original_size_bytes=len(raw_bytes),
    )

    if not config.enabled:
        result.skipped = True
        result.skip_reason = "Optymalizacja wyłączona"
        result.optimized_size_bytes = len(raw_bytes)
        return raw_bytes, "", result

    try:
        # Otwórz obraz
        img = Image.open(io.BytesIO(raw_bytes))
        original_format = (img.format or "JPEG").upper()
        width, height = img.size

        result.original_width = width
        result.original_height = height
        result.original_format = original_format

        # Sprawdź co wymaga optymalizacji
        needs = _needs_optimization(img, raw_bytes, config)

        # Za mały obraz — nie ruszaj
        if needs["too_small"]:
            result.skipped = True
            result.skip_reason = (
                f"Za mały ({width}x{height} < "
                f"{config.min_width}x{config.min_height})"
            )
            result.optimized_size_bytes = len(raw_bytes)
            result.optimized_width = width
            result.optimized_height = height
            result.optimized_format = original_format
            return raw_bytes, "", result

        # GIF animowany — nie ruszaj (utrata animacji)
        if original_format == "GIF" and getattr(img, "is_animated", False):
            result.skipped = True
            result.skip_reason = "Animowany GIF — pominięto"
            result.optimized_size_bytes = len(raw_bytes)
            result.optimized_width = width
            result.optimized_height = height
            result.optimized_format = "GIF"
            return raw_bytes, "", result

        any_needed = any(
            needs[k]
            for k in (
                "resize", "compress", "convert_bmp",
                "convert_tiff", "convert_png", "convert_webp",
                "strip",
            )
        )

        if not any_needed:
            result.skipped = True
            result.skip_reason = "Nie wymaga optymalizacji"
            result.optimized_size_bytes = len(raw_bytes)
            result.optimized_width = width
            result.optimized_height = height
            result.optimized_format = original_format
            return raw_bytes, "", result

        # ── Resize ──
        if needs["resize"]:
            img, was_resized = _resize_image(img, config)
            result.was_resized = was_resized

        result.optimized_width, result.optimized_height = img.size

        # ── Określ format wyjściowy ──
        output_format = _determine_output_format(
            img, original_format, needs, config
        )
        result.optimized_format = output_format
        result.was_converted = output_format != original_format

        # ── Strip metadata ──
        if needs["strip"]:
            result.was_stripped = True
            # Pillow domyślnie nie kopiuje EXIF przy save (chyba że podamy exif=)
            # Więc wystarczy nie przekazywać info= ani exif=

        # ── Compress & Save ──
        target_kb = config.max_file_size_kb if needs["compress"] else None
        optimized_bytes = _save_optimized(
            img, output_format, config, target_size_kb=target_kb
        )

        result.optimized_size_bytes = len(optimized_bytes)
        result.was_compressed = len(optimized_bytes) < len(raw_bytes)

        # Jeśli optymalizacja powiększyła plik — zwróć oryginał
        if len(optimized_bytes) >= len(raw_bytes) and not result.was_converted:
            logger.debug(
                "EAN %s: optymalizacja powiększyła plik "
                "(%.1f KB → %.1f KB) — zachowuję oryginał",
                ean,
                len(raw_bytes) / 1024,
                len(optimized_bytes) / 1024,
            )
            result.optimized_size_bytes = len(raw_bytes)
            result.was_compressed = False
            result.was_resized = False
            result.skipped = True
            result.skip_reason = "Optymalizacja powiększyłaby plik"
            return raw_bytes, "", result

        new_ext = _FORMAT_TO_EXT.get(output_format, ".jpg")

        logger.info(
            "EAN %s: %dx%d→%dx%d, %.1f KB→%.1f KB (-%0.f%%), %s→%s",
            ean,
            result.original_width, result.original_height,
            result.optimized_width, result.optimized_height,
            result.original_size_bytes / 1024,
            result.optimized_size_bytes / 1024,
            result.size_reduction_pct,
            original_format, output_format,
        )

        return optimized_bytes, new_ext, result

    except Exception as exc:
        result.error = str(exc)
        result.optimized_size_bytes = len(raw_bytes)
        logger.exception("Błąd optymalizacji EAN %s: %s", ean, exc)
        return raw_bytes, "", result


# ── Batch optymalizacja DataFrame ──────────────────────────────────────────

def optimize_dataframe(
    df: pd.DataFrame,
    config: Optional[OptimizationConfig] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> tuple[pd.DataFrame, OptimizationSummary]:
    """
    Optymalizuje wszystkie obrazy w DataFrame.

    Modyfikuje kolumny:
    - _image_bytes → zoptymalizowane bajty
    - rozmiar → nowy rozmiar
    - rozdzielczość → nowa rozdzielczość
    - rozszerzenie → nowe rozszerzenie (jeśli konwersja)

    Dodaje kolumny:
    - _was_optimized (bool)
    - _size_before (str)
    - _size_after (str)
    - _optimization_info (str)

    Args:
        df: DataFrame z kolumną _image_bytes.
        config: Konfiguracja optymalizacji.
        progress_callback: Opcjonalny callback (done, total).

    Returns:
        (zmodyfikowany_df, summary)
    """
    if config is None:
        config = OptimizationConfig()

    summary = OptimizationSummary()
    total = len(df)
    summary.total = total

    if not config.enabled:
        logger.info("Optymalizacja wyłączona — pomijam.")
        return df, summary

    has_bytes = "_image_bytes" in df.columns

    # Nowe kolumny
    df = df.copy()
    df["_was_optimized"] = False
    df["_size_before"] = ""
    df["_size_after"] = ""
    df["_optimization_info"] = ""

    for i, (idx, row) in enumerate(df.iterrows()):
        # Pomiń wiersze bez obrazu
        status = str(row.get("status", ""))
        if status != "OK":
            summary.skipped += 1
            if progress_callback:
                progress_callback(i + 1, total)
            continue

        raw_bytes = None
        if has_bytes:
            raw_bytes = row.get("_image_bytes")

        if not isinstance(raw_bytes, bytes) or len(raw_bytes) == 0:
            summary.skipped += 1
            if progress_callback:
                progress_callback(i + 1, total)
            continue

        ean = str(row.get("ean", ""))

        # Optymalizuj
        optimized_bytes, new_ext, result = optimize_single_image(
            raw_bytes, ean=ean, config=config
        )

        summary.results.append(result)

        if result.error:
            summary.errors += 1
        elif result.skipped:
            summary.skipped += 1
        else:
            summary.optimized += 1
            summary.total_original_kb += result.original_size_bytes / 1024
            summary.total_optimized_kb += result.optimized_size_bytes / 1024

            if result.was_resized:
                summary.resized_count += 1
            if result.was_compressed:
                summary.compressed_count += 1
            if result.was_converted:
                summary.converted_count += 1

            # Aktualizuj DataFrame
            df.at[idx, "_image_bytes"] = optimized_bytes
            df.at[idx, "_was_optimized"] = True

            # Aktualizuj metadane
            size_kb = len(optimized_bytes) / 1024
            df.at[idx, "rozmiar"] = (
                f"{size_kb:.1f} KB"
                if size_kb < 1024
                else f"{size_kb / 1024:.2f} MB"
            )
            df.at[idx, "rozdzielczość"] = (
                f"{result.optimized_width}×{result.optimized_height}"
            )

            if new_ext and result.was_converted:
                df.at[idx, "rozszerzenie"] = new_ext.lstrip(".").upper()

        # Info kolumny
        before_kb = result.original_size_bytes / 1024
        after_kb = result.optimized_size_bytes / 1024
        df.at[idx, "_size_before"] = f"{before_kb:.1f} KB"
        df.at[idx, "_size_after"] = f"{after_kb:.1f} KB"

        info_parts = []
        if result.was_resized:
            info_parts.append(
                f"Resize {result.original_width}×{result.original_height}"
                f"→{result.optimized_width}×{result.optimized_height}"
            )
        if result.was_compressed:
            info_parts.append(f"-{result.size_reduction_pct:.0f}%")
        if result.was_converted:
            info_parts.append(
                f"{result.original_format}→{result.optimized_format}"
            )
        if result.skipped:
            info_parts.append(result.skip_reason)
        df.at[idx, "_optimization_info"] = " | ".join(info_parts) or "—"

        if progress_callback:
            progress_callback(i + 1, total)

    logger.info(
        "Optymalizacja zakończona: %d zoptymalizowanych, %d pominiętych, "
        "%d błędów. Oszczędność: %.1f KB (%.1f%%)",
        summary.optimized,
        summary.skipped,
        summary.errors,
        summary.total_saved_kb,
        summary.total_saved_pct,
    )

    return df, summary