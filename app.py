"""
EAN Image Manager — Streamlit Application
Pobiera grafiki na podstawie EAN-ów przez Power Automate webhook,
analizuje je i eksportuje do OneDrive.
"""

import io
import re
import logging
from dataclasses import dataclass, field
from html import escape as html_escape
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import pandas as pd
import streamlit as st

from config import (
    WEBHOOK_URL_FETCH,
    WEBHOOK_URL_ONEDRIVE,
    BATCH_SIZE,
    PAGE_TITLE,
)

from ean_processor import fetch_ean_urls_batch
from image_analyzer import analyze_images_parallel
from onedrive_exporter import export_to_onedrive

from image_optimizer import (
    OptimizationConfig,
    OptimizationSummary,
    optimize_dataframe,
    PRESETS,
)


# ── Constants ──────────────────────────────────────────────────────────────
MAX_EANS = 500
CARDS_PER_ROW = 4
ITEMS_PER_PAGE = 50
TEXTAREA_HEIGHT = 180

# Auto-reject thresholds
AUTO_REJECT_MIN_WIDTH = 300
AUTO_REJECT_MIN_HEIGHT = 300
CSS_FILE = Path(__file__).parent / "styles" / "main.css"

# Column name mapping (internal english ↔ display polish)
COL_STATUS = "status"
COL_URL = "url"
COL_EAN = "ean"
COL_NAME = "name"
COL_RESOLUTION = "resolution"
COL_FILE_SIZE = "file_size"
COL_EXTENSION = "extension"
COL_ERROR = "error"

# Before-optimization column names
COL_RESOLUTION_BEFORE = "_resolution_before"
COL_FILE_SIZE_BEFORE = "_file_size_before"
COL_EXTENSION_BEFORE = "_extension_before"

DISPLAY_LABELS = {
    COL_NAME: "Nazwa produktu",
    COL_RESOLUTION: "Rozdzielczość",
    COL_FILE_SIZE: "Rozmiar",
    COL_EXTENSION: "Rozszerzenie",
    COL_ERROR: "Błąd",
    COL_STATUS: "Status",
    COL_EAN: "EAN",
    COL_URL: "URL",
}

# Legacy column remapping – if analyze_images_parallel returns Polish names
_LEGACY_COLUMN_MAP = {
    "rozdzielczość": COL_RESOLUTION,
    "rozmiar": COL_FILE_SIZE,
    "rozszerzenie": COL_EXTENSION,
    "błąd": COL_ERROR,
    "nazwa": COL_NAME,
}

# User-friendly error messages
_USER_ERRORS: dict[type, str] = {
    ConnectionError: "Nie można połączyć się z serwerem. Sprawdź połączenie sieciowe.",
    TimeoutError: "Serwer nie odpowiedział w wyznaczonym czasie. Spróbuj ponownie.",
    OSError: "Problem z połączeniem sieciowym. Sprawdź konfigurację.",
}

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ── State management ──────────────────────────────────────────────────────
@dataclass
class AppState:
    """Centralny stan aplikacji — eliminuje magiczne klucze w session_state."""

    results_df: Optional[pd.DataFrame] = None
    rejected_eans: set[str] = field(default_factory=set)
    export_done: bool = False
    last_eans: str = ""
    last_valid_eans: set[str] = field(default_factory=set)
    strict_ean: bool = False
    # ── Optymalizacja grafik ──
    optimization_config: OptimizationConfig = field(
        default_factory=lambda: PRESETS["E-commerce"]
    )
    optimization_summary: Optional[OptimizationSummary] = None
    optimized: bool = False


def get_state() -> AppState:
    if "_app_state" not in st.session_state:
        st.session_state._app_state = AppState()
    return st.session_state._app_state


# ── Validation helpers ────────────────────────────────────────────────────
def validate_ean(code: str, strict: bool = False) -> bool:
    if not re.fullmatch(r"\d{8}|\d{13}|\d{14}", code):
        return False
    if not strict:
        return True
    digits = [int(d) for d in code]
    checksum = sum(
        d * (3 if i % 2 == len(digits) % 2 else 1)
        for i, d in enumerate(digits[:-1])
    )
    return (10 - checksum % 10) % 10 == digits[-1]


def parse_eans(raw: str, strict: bool = False) -> tuple[list[str], list[str]]:
    seen: set[str] = set()
    valid: list[str] = []
    invalid: list[str] = []
    for line in raw.strip().splitlines():
        ean = line.strip()
        if not ean or ean in seen:
            continue
        seen.add(ean)
        if validate_ean(ean, strict=strict):
            valid.append(ean)
        else:
            invalid.append(ean)
    return valid, invalid


def sanitize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    return html_escape(url, quote=True)


def user_error_message(exc: Exception) -> str:
    for exc_type, msg in _USER_ERRORS.items():
        if isinstance(exc, exc_type):
            return msg
    return "Wystąpił nieoczekiwany błąd. Sprawdź logi lub spróbuj ponownie."


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    return df.rename(columns=_LEGACY_COLUMN_MAP)


# ── Statistics ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AnalysisStats:
    total: int
    ok: int
    errors: int
    accepted: int
    missing: int
    optimized: int


def compute_stats(df: pd.DataFrame, rejected_eans: set[str]) -> AnalysisStats:
    total = len(df)
    ok = int((df[COL_STATUS] == "OK").sum())
    missing = int((df[COL_STATUS] == "brak obrazu").sum())
    errors = total - ok - missing
    accepted = len(df[~df[COL_EAN].isin(rejected_eans)])
    optimized = int(df.get("_was_optimized", pd.Series(False)).sum()) if "_was_optimized" in df.columns else 0
    return AnalysisStats(total=total, ok=ok, errors=errors, accepted=accepted, missing=missing, optimized=optimized)


def render_stats_html(s: AnalysisStats) -> str:
    optimized_box = ""
    if s.optimized > 0:
        optimized_box = (
            '<div class="stat-box purple">'
            '<div class="stat-icon">⚡</div>'
            '<div class="stat-label">Zoptymalizowane</div>'
            f'<div class="stat-value purple">{s.optimized}</div>'
            "</div>"
        )

    boxes = (
        '<div class="stat-box blue">'
        '<div class="stat-icon">📊</div>'
        '<div class="stat-label">Łącznie</div>'
        f'<div class="stat-value blue">{s.total}</div>'
        "</div>"
        '<div class="stat-box green">'
        '<div class="stat-icon">✅</div>'
        '<div class="stat-label">OK</div>'
        f'<div class="stat-value green">{s.ok}</div>'
        "</div>"
        '<div class="stat-box orange">'
        '<div class="stat-icon">📭</div>'
        '<div class="stat-label">Brak grafiki</div>'
        f'<div class="stat-value orange">{s.missing}</div>'
        "</div>"
        '<div class="stat-box red">'
        '<div class="stat-icon">❌</div>'
        '<div class="stat-label">Błędy</div>'
        f'<div class="stat-value red">{s.errors}</div>'
        "</div>"
        '<div class="stat-box default">'
        '<div class="stat-icon">✓</div>'
        '<div class="stat-label">Zaakceptowane</div>'
        f'<div class="stat-value">{s.accepted}</div>'
        "</div>"
        + optimized_box
    )

    return '<div class="stat-row">' + boxes + "</div>"


# ── Product card renderer ─────────────────────────────────────────────────
def render_product_card_html(row: pd.Series, is_rejected: bool) -> str:
    """Generate safe HTML for a single product card, showing before/after optimization."""
    status = str(row.get(COL_STATUS, ""))
    url = sanitize_url(str(row.get(COL_URL, "") or ""))
    ean = html_escape(str(row.get(COL_EAN, "")))
    name = html_escape(str(row.get(COL_NAME, "") or ""))
    err = html_escape(str(row.get(COL_ERROR, "") or ""))

    # Current (after optimization) values
    resolution = html_escape(str(row.get(COL_RESOLUTION, "") or ""))
    file_size = html_escape(str(row.get(COL_FILE_SIZE, "") or ""))
    extension = html_escape(str(row.get(COL_EXTENSION, "") or ""))

    # Before optimization values
    resolution_before = html_escape(str(row.get(COL_RESOLUTION_BEFORE, "") or ""))
    file_size_before = html_escape(str(row.get(COL_FILE_SIZE_BEFORE, "") or ""))
    extension_before = html_escape(str(row.get(COL_EXTENSION_BEFORE, "") or ""))

    was_optimized = bool(row.get("_was_optimized", False))

    # Status badge
    if status == "OK":
        status_class = "status-ok"
        badge = "<span class='badge badge-ok'>✓ OK</span>"
    elif status == "brak obrazu":
        status_class = "status-warn"
        badge = "<span class='badge badge-warn'>⚠ BRAK</span>"
    else:
        status_class = "status-error"
        badge = "<span class='badge badge-err'>✗ BŁĄD</span>"

    rejected_class = "rejected" if is_rejected else ""

    # Optimized badge
    opt_badge = "<span class='badge badge-opt'>⚡ OPT</span>" if was_optimized else ""

    # Image area
    if status == "OK" and url:
        image_html = (
            f"<a href='{url}' target='_blank' rel='noopener noreferrer' style='display:block;cursor:zoom-in;'>"
            f"<img src='{url}' class='card-image' alt='EAN {ean}' "
            f"loading='lazy' onerror=\"this.parentElement.style.display='none'\">"
            f"</a>"
        )
    elif status == "brak obrazu":
        image_html = (
            "<div class='card-placeholder warn'>"
            "<span class='card-placeholder-icon'>📭</span>"
            "<span class='card-placeholder-text'>Brak grafiki</span>"
            "</div>"
        )
    else:
        image_html = (
            "<div class='card-placeholder error'>"
            "<span class='card-placeholder-icon'>❌</span>"
            "<span class='card-placeholder-text'>Błąd pobierania</span>"
            "</div>"
        )

    # Meta section — before/after if optimized, otherwise just current
    if was_optimized and (resolution_before or file_size_before):
        meta_html = _render_before_after_meta(
            resolution_before, file_size_before, extension_before,
            resolution, file_size, extension,
        )
    else:
        meta_parts: list[str] = []
        if resolution:
            meta_parts.append(f"<span class='card-meta-item'>📐 {resolution}</span>")
        if file_size:
            meta_parts.append(f"<span class='card-meta-item'>💾 {file_size}</span>")
        if extension:
            meta_parts.append(f"<span class='card-meta-item'>🗂 {extension}</span>")
        meta_html = (
            f"<div class='card-meta'>{''.join(meta_parts)}</div>" if meta_parts else ""
        )

    link_html = ""

    # Error line
    error_html = ""
    if err and status != "brak obrazu":
        short_err = (err[:40] + "...") if len(err) > 40 else err
        error_html = f"<div class='card-error'>⚠️ {short_err}</div>"

    name_html = (
        f"<div class='card-name'>{name}</div>" if name else ""
    )

    return (
        f"<div class='product-card {status_class} {rejected_class}'>"
        f"  {image_html}"
        f"  <div class='card-body'>"
        f"    <div class='card-header'>"
        f"      <span class='card-ean'>{ean}</span>"
        f"      <div class='card-badges'>{badge}{opt_badge}</div>"
        f"    </div>"
        f"    {name_html}"
        f"    {meta_html}"
        f"    {link_html}"
        f"    {error_html}"
        f"  </div>"
        f"</div>"
    )


def _render_before_after_meta(
    res_before: str, size_before: str, ext_before: str,
    res_after: str, size_after: str, ext_after: str,
) -> str:
    """Renders a compact before/after comparison table for image metadata."""
    rows_html = ""

    if res_before and res_after and res_before != res_after:
        rows_html += (
            f"<tr>"
            f"<td class='meta-label'>📐 Rozdzielczość</td>"
            f"<td class='meta-before'>{res_before}</td>"
            f"<td class='meta-arrow'>→</td>"
            f"<td class='meta-after'>{res_after}</td>"
            f"</tr>"
        )
    elif res_after:
        rows_html += (
            f"<tr>"
            f"<td class='meta-label'>📐 Rozdzielczość</td>"
            f"<td class='meta-before' colspan='3'>{res_after}</td>"
            f"</tr>"
        )

    if size_before and size_after and size_before != size_after:
        rows_html += (
            f"<tr>"
            f"<td class='meta-label'>💾 Rozmiar</td>"
            f"<td class='meta-before'>{size_before}</td>"
            f"<td class='meta-arrow'>→</td>"
            f"<td class='meta-after'>{size_after}</td>"
            f"</tr>"
        )
    elif size_after:
        rows_html += (
            f"<tr>"
            f"<td class='meta-label'>💾 Rozmiar</td>"
            f"<td class='meta-before' colspan='3'>{size_after}</td>"
            f"</tr>"
        )

    if ext_before and ext_after and ext_before != ext_after:
        rows_html += (
            f"<tr>"
            f"<td class='meta-label'>🗂 Format</td>"
            f"<td class='meta-before'>{ext_before}</td>"
            f"<td class='meta-arrow'>→</td>"
            f"<td class='meta-after'>{ext_after}</td>"
            f"</tr>"
        )
    elif ext_after:
        rows_html += (
            f"<tr>"
            f"<td class='meta-label'>🗂 Format</td>"
            f"<td class='meta-before' colspan='3'>{ext_after}</td>"
            f"</tr>"
        )

    if not rows_html:
        return ""

    return (
        f"<div class='meta-comparison'>"
        f"<table class='meta-table'>{rows_html}</table>"
        f"</div>"
    )


# ── CSS loader ─────────────────────────────────────────────────────────────
def load_css() -> None:
    if CSS_FILE.is_file():
        css_text = CSS_FILE.read_text(encoding="utf-8")
        css_text += _EXTRA_CSS
    else:
        logger.warning(
            "CSS file not found at %s — using embedded fallback.", CSS_FILE
        )
        css_text = _FALLBACK_CSS + _EXTRA_CSS
    st.markdown(f"<style>{css_text}</style>", unsafe_allow_html=True)
    st.markdown(_LIGHTBOX_JS, unsafe_allow_html=True)


# Additional CSS for new elements (appended to existing CSS)
_EXTRA_CSS = """
.card-badges {
    display: flex;
    align-items: center;
    gap: 0.3rem;
}

.badge-opt {
    background: rgba(99, 102, 241, 0.15);
    color: #a5b4fc;
    border: 1px solid rgba(99, 102, 241, 0.4);
}

.stat-box.purple { border-color: rgba(99, 102, 241, 0.3); }
.stat-box.purple::before { background: linear-gradient(135deg, #6366f1, #a855f7); }
.stat-value.purple { color: #a5b4fc; }

.meta-comparison {
    margin-bottom: 0.6rem;
    background: rgba(99, 102, 241, 0.06);
    border: 1px solid rgba(99, 102, 241, 0.2);
    border-radius: 6px;
    overflow: hidden;
    padding: 0.4rem 0.5rem;
}

.meta-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.72rem;
    font-family: 'JetBrains Mono', monospace;
}

.meta-table tr + tr td {
    border-top: 1px solid rgba(255,255,255,0.05);
    padding-top: 0.3rem;
    margin-top: 0.3rem;
}

.meta-label {
    color: var(--text-muted);
    padding-right: 0.5rem;
    white-space: nowrap;
    padding-top: 0.2rem;
    padding-bottom: 0.2rem;
    width: 40%;
}

.meta-before {
    color: #94a3b8;
    text-decoration: line-through;
    text-decoration-color: rgba(239, 68, 68, 0.5);
    padding-right: 0.3rem;
}

.meta-arrow {
    color: #6366f1;
    padding: 0 0.3rem;
    font-weight: 700;
}

.meta-after {
    color: #10b981;
    font-weight: 600;
}

/* Optimization config panel at top */
.opt-config-panel {
    background: linear-gradient(135deg, rgba(99, 102, 241, 0.08) 0%, rgba(139, 92, 246, 0.05) 100%);
    border: 1px solid rgba(99, 102, 241, 0.25);
    border-radius: var(--radius-lg);
    padding: 1.5rem 1.75rem;
    margin-bottom: 1.5rem;
}

.opt-config-title {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    font-size: 0.8rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: #a5b4fc;
    margin-bottom: 1.25rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid rgba(99, 102, 241, 0.2);
}

.opt-inline-badge {
    background: linear-gradient(135deg, #6366f1, #a855f7);
    color: white;
    font-size: 0.65rem;
    font-weight: 700;
    padding: 0.15rem 0.5rem;
    border-radius: 20px;
    letter-spacing: 0.5px;
    vertical-align: middle;
}
"""

_LIGHTBOX_JS = ""

_FALLBACK_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap');

:root {
    --bg-primary: #0a0b0f;
    --bg-secondary: #12141c;
    --bg-tertiary: #1a1d28;
    --bg-card: linear-gradient(145deg, #14161f 0%, #0f1118 100%);
    --border-primary: #2a2d3a;
    --border-secondary: #363a4a;
    --accent-primary: #6366f1;
    --accent-secondary: #8b5cf6;
    --accent-gradient: linear-gradient(135deg, #6366f1 0%, #8b5cf6 50%, #a855f7 100%);
    --accent-glow: rgba(99, 102, 241, 0.15);
    --success: #10b981;
    --success-bg: rgba(16, 185, 129, 0.1);
    --success-border: rgba(16, 185, 129, 0.3);
    --warning: #f59e0b;
    --warning-bg: rgba(245, 158, 11, 0.1);
    --warning-border: rgba(245, 158, 11, 0.3);
    --danger: #ef4444;
    --danger-bg: rgba(239, 68, 68, 0.1);
    --danger-border: rgba(239, 68, 68, 0.3);
    --text-primary: #f8fafc;
    --text-secondary: #e2e8f0;
    --text-muted: #94a3b8;
    --radius-sm: 8px;
    --radius-md: 12px;
    --radius-lg: 16px;
    --shadow-md: 0 4px 12px rgba(0, 0, 0, 0.4);
    --shadow-glow: 0 0 40px rgba(99, 102, 241, 0.15);
}

html, body, [class*="css"] {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background-color: var(--bg-primary);
    color: var(--text-primary);
}

.stApp {
    background: var(--bg-primary);
    min-height: 100vh;
}

#MainMenu, footer, header {visibility: hidden;}
.stDeployButton {display: none;}

.app-header { padding-bottom: 2rem; margin-bottom: 2rem; position: relative; }
.header-content { display: flex; align-items: center; gap: 1rem; }
.header-icon { width: 56px; height: 56px; background: var(--accent-gradient); border-radius: var(--radius-md); display: flex; align-items: center; justify-content: center; font-size: 1.8rem; }
.header-text h1 { font-size: 1.75rem; font-weight: 800; background: var(--accent-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text; margin: 0; }
.header-text p { color: var(--text-secondary); font-size: 0.9rem; margin: 0.25rem 0 0; }

.section-card { background: var(--bg-card); border: 1px solid var(--border-primary); border-radius: var(--radius-lg); padding: 1.75rem; margin-bottom: 1.5rem; box-shadow: var(--shadow-md); }
.section-title { display: flex; align-items: center; gap: 0.75rem; font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1.5px; color: var(--text-secondary); padding-bottom: 0.75rem; border-bottom: 1px solid var(--border-primary); }
.step-number { width: 28px; height: 28px; background: var(--accent-gradient); border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 0.85rem; font-weight: 700; color: white; }

.badge { display: inline-flex; align-items: center; padding: 0.2rem 0.5rem; border-radius: 20px; font-size: 0.65rem; font-weight: 600; font-family: 'JetBrains Mono', monospace; }
.badge-ok { background: var(--success-bg); color: var(--success); border: 1px solid var(--success-border); }
.badge-err { background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger-border); }
.badge-warn { background: var(--warning-bg); color: var(--warning); border: 1px solid var(--warning-border); }

.stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 1.25rem; margin-bottom: 2rem; }
.stat-box { background: var(--bg-secondary); border: 1px solid var(--border-primary); border-radius: var(--radius-md); padding: 1.5rem 1.75rem; position: relative; overflow: hidden; }
.stat-box::before { content: ''; position: absolute; top: 0; left: 0; width: 4px; height: 100%; }
.stat-box.blue::before { background: var(--accent-gradient); }
.stat-box.green::before { background: var(--success); }
.stat-box.orange::before { background: var(--warning); }
.stat-box.red::before { background: var(--danger); }
.stat-box.default::before { background: var(--text-muted); }
.stat-icon { font-size: 1.5rem; margin-bottom: 0.75rem; }
.stat-label { font-size: 0.85rem; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 0.5rem; }
.stat-value { font-family: 'JetBrains Mono', monospace; font-size: 2.5rem; font-weight: 700; line-height: 1.1; color: var(--text-primary); }
.stat-value.blue { color: var(--accent-primary); }
.stat-value.green { color: var(--success); }
.stat-value.orange { color: var(--warning); }
.stat-value.red { color: var(--danger); }

.stButton > button { background: var(--accent-gradient) !important; color: white !important; border: none !important; font-weight: 600 !important; border-radius: var(--radius-sm) !important; padding: 0.65rem 1.5rem !important; font-size: 0.9rem !important; }
.stDownloadButton > button { background: var(--bg-tertiary) !important; color: var(--text-primary) !important; border: 1px solid var(--border-secondary) !important; font-weight: 600 !important; border-radius: var(--radius-sm) !important; }
.stTextArea textarea { background: var(--bg-secondary) !important; border: 1px solid var(--border-primary) !important; border-radius: var(--radius-md) !important; color: var(--text-primary) !important; font-family: 'JetBrains Mono', monospace !important; font-size: 0.85rem !important; padding: 1rem !important; }

.product-card { background: var(--bg-secondary); border: 1px solid var(--border-primary); border-radius: var(--radius-md); overflow: hidden; margin-bottom: 0.75rem; }
.product-card.status-ok { border-color: var(--success-border); }
.product-card.status-warn { border-color: var(--warning-border); }
.product-card.status-error { border-color: var(--danger-border); }
.product-card.rejected { opacity: 0.4; filter: grayscale(0.5); }
.card-image { width: 100%; height: 600px; object-fit: cover; display: block; background: var(--bg-tertiary); cursor: zoom-in; }
.card-placeholder { width: 100%; height: 600px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 0.75rem; background: var(--bg-tertiary); color: var(--text-muted); }
.card-placeholder.warn { background: var(--warning-bg); }
.card-placeholder.error { background: var(--danger-bg); }
.card-placeholder-icon { font-size: 2.5rem; }
.card-placeholder-text { font-size: 0.8rem; font-weight: 500; }
#eim-lightbox { display:none; position:fixed; inset:0; z-index:999999; background:rgba(0,0,0,0.92); align-items:center; justify-content:center; cursor:zoom-out; }
#eim-lightbox.open { display:flex; }
#eim-lightbox img { max-width:92vw; max-height:92vh; object-fit:contain; border-radius:6px; box-shadow:0 8px 48px rgba(0,0,0,0.8); pointer-events:none; }
#eim-lightbox-close { position:fixed; top:1.25rem; right:1.5rem; font-size:2rem; color:#fff; cursor:pointer; line-height:1; opacity:0.8; }
#eim-lightbox-close:hover { opacity:1; }
.card-body { padding: 1rem; }
.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.6rem; gap: 0.5rem; }
.card-ean { font-family: 'JetBrains Mono', monospace; font-size: 0.85rem; font-weight: 700; color: var(--text-primary); }
.card-name { font-size: 0.8rem; font-weight: 500; color: var(--text-secondary); margin-bottom: 0.6rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.card-meta { display: flex; flex-wrap: wrap; gap: 0.6rem; margin-bottom: 0.6rem; }
.card-meta-item { font-size: 1rem; color: var(--text-muted); font-family: 'JetBrains Mono', monospace; }
.card-link-button { color: var(--accent-primary) !important; text-decoration: none !important; display: block; width: 100%; padding: 0.5rem 0.75rem; background: var(--bg-tertiary); border: 1px solid var(--border-primary); border-radius: var(--radius-sm); font-size: 0.75rem; font-weight: 600; text-align: center; margin-bottom: 0.5rem; }
.card-error { font-size: 0.7rem; color: var(--danger); background: var(--danger-bg); border-radius: var(--radius-sm); padding: 0.4rem 0.6rem; margin-bottom: 0.5rem; }


.missing-eans-box { background: var(--warning-bg); border: 1px solid var(--warning-border); border-radius: var(--radius-md); padding: 1rem; margin-bottom: 1rem; }
.missing-eans-header { display: flex; align-items: center; gap: 0.5rem; color: var(--warning); font-weight: 600; font-size: 0.9rem; }
.missing-eans-count { background: var(--warning); color: var(--bg-primary); padding: 0.1rem 0.5rem; border-radius: 12px; font-size: 0.75rem; font-weight: 700; }

.app-footer { margin-top: 3rem; padding: 1.5rem 0; border-top: 1px solid var(--border-primary); text-align: center; }
.app-footer p { color: var(--text-muted); font-size: 0.75rem; font-family: 'JetBrains Mono', monospace; margin: 0; }
"""


# ── Optimization config panel ─────────────────────────────────────────────

def render_optimization_config_panel(state: AppState) -> OptimizationConfig:
    """
    Renders the optimization configuration panel at the top of the page.
    Returns the current OptimizationConfig built from UI selections.
    """
    config = PRESETS["E-commerce"]

    if config.enabled:
        with st.expander(
            f"⚙️ Ustawienia optymalizacji — "
            f"max {config.max_width}×{config.max_height} px · "
            f"{config.max_file_size_kb} KB · "
            f"JPEG {config.jpeg_quality}% · "
            f"WebP {config.webp_quality}%",
            expanded=False,
        ):
            col1, col2, col3 = st.columns(3)

            with col1:
                st.markdown("##### 📐 Rozdzielczość")
                max_w = st.number_input(
                    "Max szerokość (px)", min_value=200, max_value=10000,
                    value=config.max_width, step=100, key="opt_max_w",
                )
                max_h = st.number_input(
                    "Max wysokość (px)", min_value=200, max_value=10000,
                    value=config.max_height, step=100, key="opt_max_h",
                )

            with col2:
                st.markdown("##### 🎨 Jakość kompresji")
                jpeg_q = st.slider(
                    "JPEG quality", min_value=30, max_value=100,
                    value=config.jpeg_quality, key="opt_jpeg_q",
                )
                webp_q = st.slider(
                    "WebP quality", min_value=30, max_value=100,
                    value=config.webp_quality, key="opt_webp_q",
                )
                max_size = st.number_input(
                    "Max rozmiar pliku (KB)", min_value=100, max_value=20000,
                    value=config.max_file_size_kb, step=100, key="opt_max_size",
                )

            with col3:
                st.markdown("##### 🔧 Opcje")
                convert_bmp = st.checkbox("BMP → JPEG", value=config.convert_bmp_to_jpeg, key="opt_bmp")
                convert_tiff = st.checkbox("TIFF → JPEG", value=config.convert_tiff_to_jpeg, key="opt_tiff")
                convert_webp = st.checkbox("WebP → PNG", value=config.convert_webp_to_png, key="opt_webp")
                convert_png = st.checkbox("PNG → JPEG (bez alpha)", value=config.convert_png_to_jpeg_if_no_alpha, key="opt_png")
                strip_meta = st.checkbox("Usuń metadane EXIF", value=config.strip_metadata, key="opt_strip")
                sharpen = st.checkbox("Wyostrz po skalowaniu", value=config.sharpen_after_resize, key="opt_sharpen")
                progressive = st.checkbox("Progresywny JPEG", value=config.progressive_jpeg, key="opt_progressive")

            config = OptimizationConfig(
                enabled=True,
                max_width=max_w,
                max_height=max_h,
                max_file_size_kb=max_size,
                jpeg_quality=jpeg_q,
                webp_quality=webp_q,
                convert_bmp_to_jpeg=convert_bmp,
                convert_tiff_to_jpeg=convert_tiff,
                convert_webp_to_png=convert_webp,
                convert_png_to_jpeg_if_no_alpha=convert_png,
                strip_metadata=strip_meta,
                sharpen_after_resize=sharpen,
                progressive_jpeg=progressive,
            )

    st.markdown("</div>", unsafe_allow_html=True)
    return config


# ══════════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION
# ══════════════════════════════════════════════════════════════════════════


def main() -> None:
    st.set_page_config(
        page_title=PAGE_TITLE,
        page_icon="🖼️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    load_css()
    state = get_state()

    # ── Header ─────────────────────────────────────────────────────────
    st.markdown(
        """
        <div class="app-header">
            <div class="header-content">
                <div class="header-icon">🖼️</div>
                <div class="header-text">
                    <h1>Pobieranie okładek na podstawie EAN</h1>
                    <p>-</p>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── OPTIMIZATION CONFIG PANEL (moved to top) ──────────────────────
    opt_config = render_optimization_config_panel(state)
    state.optimization_config = opt_config

    # ── Section 1: Input ──────────────────────────────────────────────
    st.markdown(
        '<div class="section-card">'
        '<div class="section-title">'
        '<span class="step-number">1</span> Wklej kody EAN'
        "</div>",
        unsafe_allow_html=True,
    )

    default_text = state.last_eans

    ean_input = st.text_area(
        label="Kody EAN (jeden na linię)",
        height=TEXTAREA_HEIGHT,
        placeholder="5901234123457\n5901234123458\n5901234123459\n...",
        label_visibility="collapsed",
        value=default_text,
    )

    strict_col, _ = st.columns([3, 6])
    with strict_col:
        state.strict_ean = st.checkbox(
            "🔒 Ścisła walidacja EAN (cyfra kontrolna)",
            value=state.strict_ean,
            help=(
                "Wyłącz, jeśli używasz wewnętrznych kodów bez prawidłowej "
                "cyfry kontrolnej."
            ),
        )

    col_btn, col_info = st.columns([1, 4])
    with col_btn:
        submit = st.button("🚀 Wyślij i pobierz", use_container_width=True)
    with col_info:
        if ean_input.strip():
            valid_preview, invalid_preview = parse_eans(
                ean_input, strict=state.strict_ean
            )
            parts: list[str] = [
                f"Wykryto **{len(valid_preview)}** poprawnych kodów EAN"
            ]
            if invalid_preview:
                parts.append(f"⚠️ {len(invalid_preview)} nieprawidłowych")
            parts.append(f"Batch size: {BATCH_SIZE}")
            if opt_config.enabled:
                parts.append(f"⚡ Optymalizacja aktywna")
            st.caption(" · ".join(parts))

    st.markdown("</div>", unsafe_allow_html=True)

    # ── Section 2: Processing ─────────────────────────────────────────
    if submit and ean_input.strip():
        valid_eans, invalid_eans = parse_eans(
            ean_input, strict=state.strict_ean
        )

        if invalid_eans:
            preview = ", ".join(invalid_eans[:10])
            suffix = (
                f" … i {len(invalid_eans) - 10} więcej"
                if len(invalid_eans) > 10
                else ""
            )
            st.warning(
                f"⚠️ Pominięto {len(invalid_eans)} nieprawidłowych wpisów: "
                f"{preview}{suffix}"
            )

        if not valid_eans:
            st.error("Nie wykryto żadnych prawidłowych kodów EAN.")
        elif len(valid_eans) > MAX_EANS:
            st.error(
                f"Maksymalna liczba kodów EAN to {MAX_EANS}. "
                f"Podano: {len(valid_eans)}."
            )
        else:
            # Sprawdź czy to te same EAN-y co ostatnie zapytanie
            if (
                state.results_df is not None
                and set(valid_eans) == state.last_valid_eans
            ):
                st.info(
                    "ℹ️ Już pobrano wyniki dla tych EAN-ów. "
                    "Zmień listę, aby pobrać ponownie."
                )
            else:
                state.last_eans = ean_input
                state.rejected_eans = set()
                state.export_done = False
                state.optimization_summary = None
                state.optimized = False

                ean_url_map = _fetch_urls(valid_eans)
                if ean_url_map is None:
                    st.stop()

                # Analyze + optimize inline
                df, summary = _analyze_and_optimize(valid_eans, ean_url_map, opt_config)
                if df is None:
                    st.stop()

                state.results_df = df
                state.last_valid_eans = set(valid_eans)
                state.optimization_summary = summary
                if summary is not None:
                    state.optimized = summary.optimized > 0
                state.rejected_eans = _auto_reject_low_res(df)
                st.rerun()

    elif submit and not ean_input.strip():
        st.warning("⚠️ Wklej przynajmniej jeden kod EAN.")

    # ── Section 3: Results ────────────────────────────────────────────
    _render_results(state)

    # ── Footer ────────────────────────────────────────────────────────
    st.markdown(
        '<div class="app-footer">'
        "<p>EAN Image Manager · Streamlit + Pillow + httpx</p>"
        "</div>",
        unsafe_allow_html=True,
    )


# ── Processing helpers ────────────────────────────────────────────────────


def _auto_reject_low_res(df: pd.DataFrame) -> set[str]:
    """
    Zwraca zbiór EAN-ów, których rozdzielczość jest poniżej progów
    AUTO_REJECT_MIN_WIDTH × AUTO_REJECT_MIN_HEIGHT.
    Rozdzielczość odczytywana z kolumny COL_RESOLUTION jako "WxH" lub "W×H".
    """
    rejected: set[str] = set()
    if COL_RESOLUTION not in df.columns or COL_EAN not in df.columns:
        return rejected

    for _, row in df.iterrows():
        if str(row.get(COL_STATUS, "")) != "OK":
            continue
        res = str(row.get(COL_RESOLUTION, "") or "")
        # obsługuje zarówno "×" (unicode) jak i "x" (ascii)
        parts = res.replace("×", "x").lower().split("x")
        if len(parts) != 2:
            continue
        try:
            w, h = int(parts[0].strip()), int(parts[1].strip())
        except ValueError:
            continue
        if w < AUTO_REJECT_MIN_WIDTH or h < AUTO_REJECT_MIN_HEIGHT:
            rejected.add(str(row[COL_EAN]))

    return rejected


def _fetch_urls(
    eans: list[str],
) -> Optional[dict[str, dict]]:
    """Fetch EAN→{urls, name} mapping. Returns None on error."""
    with st.spinner(f"📡 Wysyłanie {len(eans)} EAN-ów do Power Automate..."):
        try:
            ean_url_map = fetch_ean_urls_batch(eans, WEBHOOK_URL_FETCH, BATCH_SIZE)
            logger.info("Otrzymano mapowanie dla %d EAN-ów", len(ean_url_map))
        except Exception as exc:
            st.error(f"❌ {user_error_message(exc)}")
            logger.exception("Błąd fetch_ean_urls_batch")
            return None

    total_urls = sum(len(v["urls"]) for v in ean_url_map.values())
    st.success(f"✅ Odebrano {total_urls} URL-i dla {len(ean_url_map)} EAN-ów.")
    return ean_url_map


def _analyze_and_optimize(
    eans: list[str],
    ean_url_map: dict[str, dict],
    opt_config: OptimizationConfig,
) -> tuple[Optional[pd.DataFrame], Optional[OptimizationSummary]]:
    """
    Download, analyze, and immediately optimize images inline.
    Returns (normalized DataFrame, OptimizationSummary) or (None, None) on error.
    """
    from image_optimizer import optimize_dataframe

    # Step 1: Download & analyze
    spinner_msg = "🔍 Pobieranie i analiza grafik..."
    if opt_config.enabled:
        spinner_msg = "🔍 Pobieranie, analiza i optymalizacja grafik w locie..."

    with st.spinner(spinner_msg):
        progress_bar = st.progress(0)

        def on_progress(done: int, total: int) -> None:
            # Allocate 70% for download, 30% for optimization
            progress_bar.progress(int((done / max(total, 1)) * 0.7))

        try:
            records = analyze_images_parallel(
                eans=eans,
                ean_url_map=ean_url_map,
                progress_callback=on_progress,
            )
        except Exception as exc:
            st.error(f"❌ {user_error_message(exc)}")
            logger.exception("Błąd analyze_images_parallel")
            return None, None

        df = pd.DataFrame(records)
        df = normalize_columns(df)

        summary: Optional[OptimizationSummary] = None

        if opt_config.enabled and not df.empty:
            # Save original metadata BEFORE optimization
            df[COL_RESOLUTION_BEFORE] = df[COL_RESOLUTION].copy()
            df[COL_FILE_SIZE_BEFORE] = df[COL_FILE_SIZE].copy()
            df[COL_EXTENSION_BEFORE] = df[COL_EXTENSION].copy()

            def on_opt_progress(done: int, total: int) -> None:
                base = 0.7
                progress_bar.progress(base + int((done / max(total, 1)) * 0.3))

            df, summary = optimize_dataframe(
                df, config=opt_config, progress_callback=on_opt_progress,
            )
        else:
            # No optimization — still add before columns as empty
            df[COL_RESOLUTION_BEFORE] = ""
            df[COL_FILE_SIZE_BEFORE] = ""
            df[COL_EXTENSION_BEFORE] = ""

        progress_bar.progress(1.0)

    # Success message
    ok_count = int((df[COL_STATUS] == "OK").sum()) if COL_STATUS in df.columns else 0
    if opt_config.enabled and summary and summary.optimized > 0:
        st.success(
            f"✅ Pobrano **{ok_count}** grafik · "
            f"Zoptymalizowano **{summary.optimized}** · "
            f"Oszczędność: **{summary.total_saved_kb:.0f} KB** "
            f"(**{summary.total_saved_pct:.1f}%**)"
        )
    else:
        st.success(f"✅ Pobrano i przeanalizowano **{ok_count}** grafik.")

    return df, summary


def _render_results(state: AppState) -> None:
    """Render results, optimization summary, and export sections."""
    df = state.results_df

    if df is None:
        return

    if df.empty:
        st.info("Nie znaleziono żadnych grafik dla podanych EAN-ów.")
        return

    # ── Results card ──────────────────────────────────────────────────
    st.markdown(
        '<div class="section-card" id="wyniki-top">'
        '<div class="section-title">'
        '<span class="step-number">2</span> Wyniki analizy'
        "</div>",
        unsafe_allow_html=True,
    )

    stats = compute_stats(df, state.rejected_eans)
    st.markdown(render_stats_html(stats), unsafe_allow_html=True)

    # Show optimization summary if we have one
    if state.optimization_summary and state.optimized:
        _render_optimization_summary(state.optimization_summary)

    filtered_df = df.copy()

    page_col, rejected_col, missing_col, _ = st.columns([1, 2, 2, 3])
    with rejected_col:
        show_rejected = st.checkbox("Pokaż odrzucone", value=True)
    with missing_col:
        show_missing = st.checkbox("Pokaż brak grafik", value=True)

    if not show_rejected:
        filtered_df = filtered_df[
            ~filtered_df[COL_EAN].isin(state.rejected_eans)
        ]
    if not show_missing:
        filtered_df = filtered_df[
            filtered_df[COL_STATUS] != "brak obrazu"
        ]

    # Pagination
    total_items = len(filtered_df)
    total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    st.markdown(
        f"<p style='color:var(--text-secondary);font-size:0.85rem;"
        f"margin:1rem 0'>Wyświetlono "
        f"<strong style='color:var(--text-primary)'>{total_items}</strong>"
        f" grafik</p>",
        unsafe_allow_html=True,
    )

    with page_col:
        page = st.number_input(
            "Strona",
            min_value=1,
            max_value=total_pages,
            value=1,
            step=1,
            label_visibility="collapsed",
            help=f"Strona 1–{total_pages} ({ITEMS_PER_PAGE}/stronę)",
        )

    start_idx = (page - 1) * ITEMS_PER_PAGE
    page_df = filtered_df.iloc[start_idx : start_idx + ITEMS_PER_PAGE]

    rows_list = list(page_df.iterrows())

    def make_toggle_callback(ean: str):
        def callback():
            if ean in state.rejected_eans:
                state.rejected_eans.discard(ean)
            else:
                state.rejected_eans.add(ean)
        return callback

    for i in range(0, len(rows_list), CARDS_PER_ROW):
        cols = st.columns(CARDS_PER_ROW, gap="small")
        for j in range(CARDS_PER_ROW):
            if i + j < len(rows_list):
                idx, row = rows_list[i + j]
                ean_value = str(row[COL_EAN])
                is_rejected = ean_value in state.rejected_eans

                with cols[j]:
                    st.markdown(
                        render_product_card_html(row, is_rejected),
                        unsafe_allow_html=True,
                    )
                    if str(row.get(COL_STATUS, "")) == "OK":
                        st.checkbox(
                            "🗑 Odrzuć",
                            key=f"rej_{ean_value}",
                            value=is_rejected,
                            on_change=make_toggle_callback(ean_value),
                        )

    if total_pages > 1:
        st.caption(f"Strona {page} z {total_pages}")

    st.markdown("</div>", unsafe_allow_html=True)

    # ── Export card ───────────────────────────────────────────────────
    _render_export(df, state)


def _render_optimization_summary(summary: OptimizationSummary) -> None:
    """Displays a compact optimization summary inline in results."""
    with st.expander(
        f"⚡ Podsumowanie optymalizacji — zaoszczędzono **{summary.total_saved_kb:.0f} KB** "
        f"({summary.total_saved_pct:.1f}%)",
        expanded=False,
    ):
        st.markdown(
            f"""
            <div class="stat-row" style="margin-bottom:1rem">
                <div class="stat-box green">
                    <div class="stat-icon">⚡</div>
                    <div class="stat-label">Zoptymalizowane</div>
                    <div class="stat-value green">{summary.optimized}</div>
                </div>
                <div class="stat-box blue">
                    <div class="stat-icon">📐</div>
                    <div class="stat-label">Przeskalowane</div>
                    <div class="stat-value blue">{summary.resized_count}</div>
                </div>
                <div class="stat-box orange">
                    <div class="stat-icon">🗜️</div>
                    <div class="stat-label">Skompresowane</div>
                    <div class="stat-value orange">{summary.compressed_count}</div>
                </div>
                <div class="stat-box default">
                    <div class="stat-icon">🔄</div>
                    <div class="stat-label">Skonwertowane</div>
                    <div class="stat-value">{summary.converted_count}</div>
                </div>
                <div class="stat-box green">
                    <div class="stat-icon">💾</div>
                    <div class="stat-label">Oszczędność</div>
                    <div class="stat-value green">{summary.total_saved_kb:.0f} KB</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if summary.results:
            details = []
            for r in summary.results:
                if r.skipped and not r.error:
                    continue
                info = []
                if r.was_resized:
                    info.append(f"📐 {r.original_width}×{r.original_height}→{r.optimized_width}×{r.optimized_height}")
                if r.was_compressed:
                    info.append(f"🗜️ -{r.size_reduction_pct:.0f}%")
                if r.was_converted:
                    info.append(f"🔄 {r.original_format}→{r.optimized_format}")
                if r.error:
                    info.append(f"❌ {r.error}")
                if info:
                    details.append({
                        "EAN": r.ean,
                        "Przed": f"{r.original_size_bytes / 1024:.1f} KB",
                        "Po": f"{r.optimized_size_bytes / 1024:.1f} KB",
                        "Oszczędność": f"{r.size_saved_kb:.1f} KB",
                        "Zmiany": " · ".join(info),
                    })

            if details:
                st.dataframe(
                    pd.DataFrame(details),
                    use_container_width=True,
                    hide_index=True,
                )


def _render_export(df: pd.DataFrame, state: AppState) -> None:
    """Render OneDrive export section and missing EANs list."""
    accepted_df = df[~df[COL_EAN].isin(state.rejected_eans)]
    accepted_ok = accepted_df[accepted_df[COL_STATUS] == "OK"]

    missing_eans_df = df[df[COL_STATUS] == "brak obrazu"]
    missing_auto = missing_eans_df[COL_EAN].tolist()
    missing_rejected = [e for e in state.rejected_eans if e not in set(missing_auto)]
    missing_eans_list = missing_auto + missing_rejected

    st.markdown(
        '<div class="section-card">'
        '<div class="section-title">'
        '<span class="step-number">3</span> Eksport do OneDrive'
        "</div>",
        unsafe_allow_html=True,
    )

    export_col, missing_col = st.columns(2, gap="large")

    with export_col:
        st.markdown("#### ☁️ Eksport zaakceptowanych grafik")
        st.metric("Zaakceptowane grafiki (OK)", len(accepted_ok))

        if state.export_done:
            st.success("✅ Grafiki zostały wysłane do OneDrive!")

        if not accepted_ok.empty:
            if st.button("☁️ Wyślij do OneDrive", use_container_width=True):
                with st.spinner("📤 Eksportowanie do OneDrive..."):
                    try:
                        result = export_to_onedrive(
                            accepted_df=accepted_ok,
                            webhook_url=WEBHOOK_URL_ONEDRIVE,
                        )
                        state.export_done = True
                        st.success(
                            f"✅ Wysłano {result['sent']} grafik. "
                            f"Błędy: {result['errors']}."
                        )
                        logger.info("Export done: %s", result)
                    except Exception as exc:
                        st.error(f"❌ {user_error_message(exc)}")
                        logger.exception("Błąd export_to_onedrive")
        else:
            st.info("Brak zaakceptowanych grafik z statusem OK do eksportu.")

        st.markdown("---")
        export_df = df.rename(columns=DISPLAY_LABELS)
        excel_buf = io.BytesIO()
        export_df.to_excel(excel_buf, index=False, engine="openpyxl")
        st.download_button(
            "⬇️ Pobierz Excel z wynikami",
            data=excel_buf.getvalue(),
            file_name="ean_images_report.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    with missing_col:
        st.markdown("#### 📭 EAN-y bez grafiki / odrzucone")

        if missing_eans_list:
            missing_eans_text = "\n".join(missing_eans_list)
            n_auto = len(missing_auto)
            n_rejected_only = len(missing_rejected)
            breakdown_parts = []
            if n_auto:
                breakdown_parts.append(f"{n_auto} bez grafiki")
            if n_rejected_only:
                breakdown_parts.append(f"{n_rejected_only} odrzuconych")
            breakdown_str = " + ".join(breakdown_parts)

            st.markdown(
                f"""
                <div class="missing-eans-box">
                    <div class="missing-eans-header">
                        📭 Lista do uzupełnienia:
                        <span class="missing-eans-count">{len(missing_eans_list)}</span>
                        EAN-ów
                        <span style="font-size:0.75rem;opacity:0.7;margin-left:0.5rem">({breakdown_str})</span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            st.code(missing_eans_text, language=None)

            btn1, btn2, btn3 = st.columns(3)
            with btn1:
                st.button(
                    "📋 Skopiuj EAN-y",
                    key="copy_missing_eans",
                    use_container_width=True,
                    on_click=None,
                    help="Użyj ikony 📋 w prawym górnym rogu pola powyżej",
                )
            with btn2:
                st.download_button(
                    "⬇️ Pobierz jako TXT",
                    data=missing_eans_text.encode("utf-8"),
                    file_name="brakujace_eany.txt",
                    mime="text/plain",
                    use_container_width=True,
                )
            with btn3:
                missing_excel_buf = io.BytesIO()
                pd.DataFrame({COL_EAN: missing_eans_list}).rename(
                    columns=DISPLAY_LABELS
                ).to_excel(missing_excel_buf, index=False, engine="openpyxl")
                st.download_button(
                    "⬇️ Pobierz jako Excel",
                    data=missing_excel_buf.getvalue(),
                    file_name="brakujace_eany.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                )
        else:
            st.success("🎉 Wszystkie EAN-y mają przypisane grafiki!")
            st.markdown(
                """
                <div style="text-align:center;padding:2rem;color:var(--text-muted)">
                    <div style="font-size:3rem;margin-bottom:0.5rem">✨</div>
                    <p>Nie znaleziono żadnych EAN-ów bez grafiki.</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("</div>", unsafe_allow_html=True)


# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
