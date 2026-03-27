"""
EAN Image Manager — Streamlit Application
Pobiera grafiki na podstawie EAN-ów przez Power Automate webhook,
analizuje je i eksportuje do OneDrive.
"""

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

from demo_data import get_demo_ean_url_map, get_demo_eans_text, simulate_webhook_delay

# ── Constants ──────────────────────────────────────────────────────────────
MAX_EANS = 500
CARDS_PER_ROW = 4
ITEMS_PER_PAGE = 20
TEXTAREA_HEIGHT = 180
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
    demo_mode: bool = False
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
    """Waliduje kod EAN.

    Args:
        code: Kod do sprawdzenia.
        strict: Jeśli True, weryfikuje cyfrę kontrolną.
                Jeśli False, sprawdza tylko format (same cyfry, prawidłowa długość).
    """
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
    """Parse, deduplicate, and validate EAN codes.

    Returns (valid_eans, invalid_lines).
    """
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
    """Allow only http/https URLs, HTML-escape the result."""
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    return html_escape(url, quote=True)


def user_error_message(exc: Exception) -> str:
    """Return a user-friendly message; hide internals."""
    for exc_type, msg in _USER_ERRORS.items():
        if isinstance(exc, exc_type):
            return msg
    return "Wystąpił nieoczekiwany błąd. Sprawdź logi lub spróbuj ponownie."


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename legacy Polish column names to English equivalents."""
    return df.rename(columns=_LEGACY_COLUMN_MAP)


# ── Statistics ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AnalysisStats:
    total: int
    ok: int
    errors: int
    accepted: int
    missing: int


def compute_stats(df: pd.DataFrame, rejected_eans: set[str]) -> AnalysisStats:
    total = len(df)
    ok = int((df[COL_STATUS] == "OK").sum())
    missing = int((df[COL_STATUS] == "brak obrazu").sum())
    errors = total - ok - missing
    accepted = len(df[~df[COL_EAN].isin(rejected_eans)])
    return AnalysisStats(total=total, ok=ok, errors=errors, accepted=accepted, missing=missing)


def render_stats_html(s: AnalysisStats) -> str:
    return f"""
    <div class="stat-row">
        <div class="stat-box blue">
            <div class="stat-icon">📊</div>
            <div class="stat-label">Łącznie</div>
            <div class="stat-value blue">{s.total}</div>
        </div>
        <div class="stat-box green">
            <div class="stat-icon">✅</div>
            <div class="stat-label">OK</div>
            <div class="stat-value green">{s.ok}</div>
        </div>
        <div class="stat-box orange">
            <div class="stat-icon">📭</div>
            <div class="stat-label">Brak grafiki</div>
            <div class="stat-value orange">{s.missing}</div>
        </div>
        <div class="stat-box red">
            <div class="stat-icon">❌</div>
            <div class="stat-label">Błędy</div>
            <div class="stat-value red">{s.errors}</div>
        </div>
        <div class="stat-box default">
            <div class="stat-icon">✓</div>
            <div class="stat-label">Zaakceptowane</div>
            <div class="stat-value">{s.accepted}</div>
        </div>
    </div>
    """


# ── Product card renderer ─────────────────────────────────────────────────
def render_product_card_html(row: pd.Series, is_rejected: bool) -> str:
    """Generate safe HTML for a single product card."""
    status = str(row.get(COL_STATUS, ""))
    url = sanitize_url(str(row.get(COL_URL, "") or ""))
    ean = html_escape(str(row.get(COL_EAN, "")))
    name = html_escape(str(row.get(COL_NAME, "") or ""))
    err = html_escape(str(row.get(COL_ERROR, "") or ""))
    resolution = html_escape(str(row.get(COL_RESOLUTION, "") or ""))
    file_size = html_escape(str(row.get(COL_FILE_SIZE, "") or ""))
    extension = html_escape(str(row.get(COL_EXTENSION, "") or ""))

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

    # Image area
    if status == "OK" and url:
        image_html = (
            f"<img src='{url}' class='card-image' alt='EAN {ean}' "
            f"loading='lazy' onerror=\"this.style.display='none'\">"
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

    # Meta items
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

    # Link button
    link_html = (
        f"<a href='{url}' target='_blank' rel='noopener noreferrer' "
        f"class='card-link-button'>🔗 LINK DO GRAFIKI</a>"
        if url
        else ""
    )

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
        f"      {badge}"
        f"    </div>"
        f"    {name_html}"
        f"    {meta_html}"
        f"    {link_html}"
        f"    {error_html}"
        f"  </div>"
        f"</div>"
    )


# ── CSS loader ─────────────────────────────────────────────────────────────
def load_css() -> None:
    """Load CSS from external file, fall back to inline if file is missing."""
    if CSS_FILE.is_file():
        css_text = CSS_FILE.read_text(encoding="utf-8")
    else:
        logger.warning(
            "CSS file not found at %s — using embedded fallback.", CSS_FILE
        )
        css_text = _FALLBACK_CSS
    st.markdown(f"<style>{css_text}</style>", unsafe_allow_html=True)


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
    background-image:
        radial-gradient(ellipse 80% 50% at 50% -20%, rgba(99, 102, 241, 0.15), transparent),
        radial-gradient(ellipse 60% 30% at 80% 50%, rgba(139, 92, 246, 0.08), transparent),
        radial-gradient(ellipse 50% 40% at 20% 80%, rgba(168, 85, 247, 0.05), transparent);
    min-height: 100vh;
}

#MainMenu, footer, header {visibility: hidden;}
.stDeployButton {display: none;}

.app-header {
    padding: 2.5rem 0 2rem;
    margin-bottom: 2rem;
    position: relative;
}
.app-header::after {
    content: '';
    position: absolute;
    bottom: 0; left: 0; right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent, var(--border-primary) 20%, var(--accent-primary) 50%, var(--border-primary) 80%, transparent);
}
.header-content { display: flex; align-items: center; gap: 1rem; }
.header-icon {
    width: 56px; height: 56px;
    background: var(--accent-gradient);
    border-radius: var(--radius-md);
    display: flex; align-items: center; justify-content: center;
    font-size: 1.8rem;
    box-shadow: var(--shadow-glow);
}
.header-text h1 {
    font-family: 'Inter', sans-serif;
    font-size: 1.75rem; font-weight: 800;
    background: var(--accent-gradient);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    background-clip: text;
    letter-spacing: -0.5px; margin: 0; line-height: 1.2;
}
.header-text p {
    color: var(--text-secondary); font-size: 0.9rem;
    font-weight: 400; margin: 0.25rem 0 0;
}

.section-card {
    background: var(--bg-card);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-lg);
    padding: 1.75rem; margin-bottom: 1.5rem;
    box-shadow: var(--shadow-md);
}
.section-title {
    display: flex; align-items: center; gap: 0.75rem;
    font-family: 'Inter', sans-serif;
    font-size: 0.8rem; font-weight: 700;
    text-transform: uppercase; letter-spacing: 1.5px;
    color: var(--text-secondary);
    margin-bottom: 1.25rem; padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border-primary);
}
.step-number {
    width: 28px; height: 28px;
    background: var(--accent-gradient);
    border-radius: 50%;
    display: inline-flex; align-items: center; justify-content: center;
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem; font-weight: 700; color: white;
}

.badge {
    display: inline-flex; align-items: center;
    padding: 0.2rem 0.5rem; border-radius: 20px;
    font-size: 0.65rem; font-weight: 600;
    font-family: 'JetBrains Mono', monospace;
}
.badge-ok { background: var(--success-bg); color: var(--success); border: 1px solid var(--success-border); }
.badge-err { background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger-border); }
.badge-warn { background: var(--warning-bg); color: var(--warning); border: 1px solid var(--warning-border); }

.stat-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 1.25rem; margin-bottom: 2rem;
}
.stat-box {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    padding: 1.5rem 1.75rem;
    position: relative; overflow: hidden;
    transition: all 0.3s ease;
}
.stat-box:hover {
    border-color: var(--border-secondary);
    transform: translateY(-2px);
    box-shadow: var(--shadow-md);
}
.stat-box::before {
    content: ''; position: absolute;
    top: 0; left: 0; width: 4px; height: 100%;
}
.stat-box.blue::before { background: var(--accent-gradient); }
.stat-box.green::before { background: var(--success); }
.stat-box.orange::before { background: var(--warning); }
.stat-box.red::before { background: var(--danger); }
.stat-box.default::before { background: var(--text-muted); }
.stat-icon { font-size: 1.5rem; margin-bottom: 0.75rem; }
.stat-label {
    font-size: 0.85rem; font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase; letter-spacing: 1.5px;
    margin-bottom: 0.5rem;
}
.stat-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 2.5rem; font-weight: 700;
    line-height: 1.1; color: var(--text-primary);
}
.stat-value.blue { color: var(--accent-primary); }
.stat-value.green { color: var(--success); }
.stat-value.orange { color: var(--warning); }
.stat-value.red { color: var(--danger); }

.stButton > button {
    background: var(--accent-gradient) !important;
    color: white !important; border: none !important;
    font-weight: 600 !important;
    font-family: 'Inter', sans-serif !important;
    border-radius: var(--radius-sm) !important;
    padding: 0.65rem 1.5rem !important;
    font-size: 0.9rem !important;
    transition: all 0.3s ease !important;
    box-shadow: 0 4px 15px rgba(99, 102, 241, 0.3) !important;
}
.stButton > button:hover {
    transform: translateY(-2px) !important;
    box-shadow: 0 6px 25px rgba(99, 102, 241, 0.4) !important;
}

.stDownloadButton > button {
    background: var(--bg-tertiary) !important;
    color: var(--text-primary) !important;
    border: 1px solid var(--border-secondary) !important;
    font-weight: 600 !important;
    border-radius: var(--radius-sm) !important;
}
.stDownloadButton > button:hover {
    border-color: var(--accent-primary) !important;
    background: rgba(99, 102, 241, 0.1) !important;
}

.stTextArea textarea {
    background: var(--bg-secondary) !important;
    border: 1px solid var(--border-primary) !important;
    border-radius: var(--radius-md) !important;
    color: var(--text-primary) !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 0.85rem !important; padding: 1rem !important;
}
.stTextArea textarea:focus {
    border-color: var(--accent-primary) !important;
    box-shadow: 0 0 0 3px var(--accent-glow) !important;
}

.stToggle > label > div { background-color: var(--bg-tertiary) !important; }
.stToggle > label > div[data-checked="true"] { background: var(--accent-gradient) !important; }
.stCheckbox label { color: var(--text-secondary) !important; font-size: 0.85rem !important; }
.stCheckbox label:hover { color: var(--text-primary) !important; }
.stMultiSelect > div > div {
    background: var(--bg-secondary) !important;
    border-color: var(--border-primary) !important;
}

.stProgress > div > div {
    background: var(--accent-gradient) !important;
    border-radius: 10px !important;
}
.stProgress > div {
    background: var(--bg-tertiary) !important;
    border-radius: 10px !important;
}

.streamlit-expanderHeader {
    background: var(--bg-secondary) !important;
    border-radius: var(--radius-sm) !important;
    color: var(--text-secondary) !important;
}
.streamlit-expanderContent {
    background: var(--bg-tertiary) !important;
    border: 1px solid var(--border-primary) !important;
    border-top: none !important;
}

[data-testid="stMetricValue"] {
    font-family: 'JetBrains Mono', monospace !important;
    font-weight: 700 !important;
    color: var(--accent-primary) !important;
}
[data-testid="stMetricLabel"] { color: var(--text-secondary) !important; }

.product-card {
    background: var(--bg-secondary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-md);
    overflow: hidden; transition: all 0.3s ease;
    margin-bottom: 0.75rem;
}
.product-card:hover {
    border-color: var(--border-secondary);
    transform: translateY(-2px);
    box-shadow: var(--shadow-md);
}
.product-card.status-ok { border-color: var(--success-border); }
.product-card.status-warn { border-color: var(--warning-border); }
.product-card.status-error { border-color: var(--danger-border); }
.product-card.rejected { opacity: 0.4; filter: grayscale(0.5); }

.card-image {
    width: 100%; height: 600px;
    object-fit: cover; display: block;
    background: var(--bg-tertiary);
}
.card-placeholder {
    width: 100%; height: 600px;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 0.75rem;
    background: var(--bg-tertiary); color: var(--text-muted);
}
.card-placeholder.warn { background: var(--warning-bg); }
.card-placeholder.error { background: var(--danger-bg); }
.card-placeholder-icon { font-size: 2.5rem; }
.card-placeholder-text { font-size: 0.8rem; font-weight: 500; }

.card-body { padding: 1rem; }
.card-header {
    display: flex; justify-content: space-between;
    align-items: center; margin-bottom: 0.6rem; gap: 0.5rem;
}
.card-ean {
    font-family: 'JetBrains Mono', monospace;
    font-size: 0.85rem; font-weight: 700;
    color: var(--text-primary);
}
.card-meta {
    display: flex; flex-wrap: wrap;
    gap: 0.6rem; margin-bottom: 0.6rem;
}
.card-meta-item {
    font-size: 1rem; color: var(--text-muted);
    font-family: 'JetBrains Mono', monospace;
}

.card-link-button {
    color: white !important; text-decoration: none !important;
    display: block; width: 100%;
    padding: 0.5rem 0.75rem;
    background: var(--bg-tertiary);
    border: 1px solid var(--border-primary);
    border-radius: var(--radius-sm);
    color: var(--accent-primary);
    font-size: 0.75rem; font-weight: 600;
    text-align: center; transition: all 0.2s ease;
    margin-bottom: 0.5rem;
}
.card-link-button:hover {
    background: rgba(99, 102, 241, 0.1);
    border-color: var(--accent-primary);
}

.card-error {
    font-size: 0.7rem; color: var(--danger);
    background: var(--danger-bg);
    border-radius: var(--radius-sm);
    padding: 0.4rem 0.6rem; margin-bottom: 0.5rem;
}

.demo-banner {
    background: linear-gradient(135deg, rgba(139, 92, 246, 0.1) 0%, rgba(99, 102, 241, 0.1) 100%);
    border: 1px solid rgba(139, 92, 246, 0.3);
    border-radius: var(--radius-md);
    padding: 1rem 1.25rem; margin-bottom: 1.5rem;
    display: flex; align-items: center; gap: 0.75rem;
}
.demo-banner-icon { font-size: 1.5rem; }
.demo-banner-text strong { color: var(--accent-secondary); }
.demo-banner-text p {
    margin: 0; font-size: 0.85rem;
    color: var(--text-secondary);
}

.missing-eans-box {
    background: var(--warning-bg);
    border: 1px solid var(--warning-border);
    border-radius: var(--radius-md);
    padding: 1rem;
    margin-bottom: 1rem;
}
.missing-eans-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--warning);
    font-weight: 600;
    font-size: 0.9rem;
}
.missing-eans-count {
    background: var(--warning);
    color: var(--bg-primary);
    padding: 0.1rem 0.5rem;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
}

.app-footer {
    margin-top: 3rem; padding: 1.5rem 0;
    border-top: 1px solid var(--border-primary);
    text-align: center;
}
.app-footer p {
    color: var(--text-muted); font-size: 0.75rem;
    font-family: 'JetBrains Mono', monospace; margin: 0;
}

@media (max-width: 1200px) {
    .stat-row { grid-template-columns: repeat(2, 1fr); }
}
@media (max-width: 768px) {
    .stat-row { grid-template-columns: 1fr; }
    .header-text h1 { font-size: 1.4rem; }
    .stat-value { font-size: 2rem; }
}

::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-track { background: var(--bg-secondary); }
::-webkit-scrollbar-thumb { background: var(--border-secondary); border-radius: 4px; }
"""


# ══════════════════════════════════════════════════════════════════════════
#  MAIN APPLICATION
# ══════════════════════════════════════════════════════════════════════════


def main() -> None:
    # ── Page config ────────────────────────────────────────────────────
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

    # ── Demo mode toggle ──────────────────────────────────────────────
    demo_col, _ = st.columns([3, 6])
    with demo_col:
        state.demo_mode = st.toggle(
            "🧪 Tryb Demo",
            value=state.demo_mode,
            help="Testuj aplikację bez podłączania do Power Automate.",
        )

    if state.demo_mode:
        st.markdown(
            """
            <div class="demo-banner">
                <div class="demo-banner-icon">🧪</div>
                <div class="demo-banner-text">
                    <p><strong>Tryb Demo aktywny</strong> — aplikacja symuluje odpowiedź
                    webhooka i pobiera publiczne obrazy z picsum.photos.
                    Eksport do OneDrive jest symulowany.</p>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    # ── Section 1: Input ──────────────────────────────────────────────
    st.markdown(
        '<div class="section-card">'
        '<div class="section-title">'
        '<span class="step-number">1</span> Wklej kody EAN'
        "</div>",
        unsafe_allow_html=True,
    )

    default_text = (
        get_demo_eans_text()
        if state.demo_mode and not state.last_eans
        else state.last_eans
    )

    ean_input = st.text_area(
        label="Kody EAN (jeden na linię)",
        height=TEXTAREA_HEIGHT,
        placeholder="5901234123457\n5901234123458\n5901234123459\n...",
        label_visibility="collapsed",
        value=default_text,
    )

    # Strict validation toggle
    strict_col, _ = st.columns([3, 6])
    with strict_col:
        state.strict_ean = st.checkbox(
            "🔒 Ścisła walidacja EAN (cyfra kontrolna)",
            value=state.strict_ean,
            help=(
                "Wyłącz, jeśli używasz wewnętrznych kodów bez prawidłowej "
                "cyfry kontrolnej. Domyślnie sprawdzany jest tylko format "
                "(8, 13 lub 14 cyfr)."
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
            state.last_eans = ean_input
            state.rejected_eans = set()
            state.export_done = False

            ean_url_map = _fetch_urls(valid_eans, state.demo_mode)
            if ean_url_map is None:
                st.stop()

            df = _analyze(valid_eans, ean_url_map)
            if df is None:
                st.stop()

            state.results_df = df
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


def _fetch_urls(
    eans: list[str],
    demo_mode: bool,
) -> Optional[dict[str, dict]]:
    """Fetch EAN→{urls, name} mapping. Returns None on error."""
    if demo_mode:
        with st.spinner(
            f"🧪 [DEMO] Symulowanie odpowiedzi webhooka "
            f"dla {len(eans)} EAN-ów..."
        ):
            simulate_webhook_delay()
            ean_url_map = get_demo_ean_url_map(eans)
            logger.info(
                "[DEMO] Wygenerowano mapowanie dla %d EAN-ów",
                len(ean_url_map),
            )
        total_urls = sum(len(v["urls"]) for v in ean_url_map.values())
        st.success(
            f"✅ [DEMO] Zasymulowano {total_urls} URL-i "
            f"dla {len(ean_url_map)} EAN-ów."
        )
        return ean_url_map

    with st.spinner(
        f"📡 Wysyłanie {len(eans)} EAN-ów do Power Automate..."
    ):
        try:
            ean_url_map = fetch_ean_urls_batch(
                eans, WEBHOOK_URL_FETCH, BATCH_SIZE
            )
            logger.info(
                "Otrzymano mapowanie dla %d EAN-ów", len(ean_url_map)
            )
        except Exception as exc:
            st.error(f"❌ {user_error_message(exc)}")
            logger.exception("Błąd fetch_ean_urls_batch")
            return None

    total_urls = sum(len(v["urls"]) for v in ean_url_map.values())
    st.success(
        f"✅ Odebrano {total_urls} URL-i dla {len(ean_url_map)} EAN-ów."
    )
    return ean_url_map


def _analyze(
    eans: list[str],
    ean_url_map: dict[str, dict],
) -> Optional[pd.DataFrame]:
    """Download and analyze images. Returns normalized DataFrame or None."""
    with st.spinner("🔍 Pobieranie i analiza grafik..."):
        progress_bar = st.progress(0)

        def on_progress(done: int, total: int) -> None:
            progress_bar.progress(done / max(total, 1))

        try:
            records = analyze_images_parallel(
                eans=eans,
                ean_url_map=ean_url_map,
                progress_callback=on_progress,
            )
        except Exception as exc:
            st.error(f"❌ {user_error_message(exc)}")
            logger.exception("Błąd analyze_images_parallel")
            return None

        progress_bar.progress(1.0)

    df = pd.DataFrame(records)
    df = normalize_columns(df)
    return df


def _render_results(state: AppState) -> None:
    """Render results and export sections."""
    df = state.results_df

    if df is None:
        return

    if df.empty:
        st.info("Nie znaleziono żadnych grafik dla podanych EAN-ów.")
        return

    # ── Results card ──────────────────────────────────────────────────
    st.markdown(
        '<div class="section-card">'
        '<div class="section-title">'
        '<span class="step-number">2</span> Wyniki analizy'
        "</div>",
        unsafe_allow_html=True,
    )

    stats = compute_stats(df, state.rejected_eans)
    st.markdown(render_stats_html(stats), unsafe_allow_html=True)

    # Filters
    with st.expander("🔧 Filtry", expanded=False):
        fcol1, fcol2, fcol3 = st.columns(3)
        with fcol1:
            all_statuses = df[COL_STATUS].unique().tolist()
            filter_status = st.multiselect(
                "Status", options=all_statuses, default=all_statuses
            )
        with fcol2:
            all_extensions = df[COL_EXTENSION].dropna().unique().tolist()
            has_na = df[COL_EXTENSION].isna().any()
            ext_options = all_extensions + (["(brak)"] if has_na else [])
            filter_ext = st.multiselect(
                "Rozszerzenie", options=ext_options, default=ext_options
            )
        with fcol3:
            show_rejected = st.checkbox("Pokaż odrzucone", value=True)

    # Apply filters
    mask = df[COL_STATUS].isin(filter_status)
    if filter_ext:
        ext_set = {e for e in filter_ext if e != "(brak)"}
        include_na = "(brak)" in filter_ext
        mask &= df[COL_EXTENSION].isin(ext_set) | (
            df[COL_EXTENSION].isna() & include_na
        )
    else:
        mask &= False  # nothing selected → show nothing

    filtered_df = df[mask].copy()

    if not show_rejected:
        filtered_df = filtered_df[
            ~filtered_df[COL_EAN].isin(state.rejected_eans)
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

    page_col, _ = st.columns([1, 5])
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

    # ── Render cards with proper rejection handling ───────────────────
    rows_list = list(page_df.iterrows())

    # Funkcja callback do aktualizacji rejected_eans
    def make_toggle_callback(ean: str):
        """Tworzy callback dla konkretnego EAN-u."""
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
                    # Renderuj kartę z aktualnym stanem odrzucenia
                    st.markdown(
                        render_product_card_html(row, is_rejected),
                        unsafe_allow_html=True,
                    )

                    # Checkbox z on_change callback
                    st.checkbox(
                        "🗑 Odrzuć",
                        key=f"rej_{ean_value}",
                        value=is_rejected,
                        on_change=make_toggle_callback(ean_value),
                    )

    if total_pages > 1:
        st.caption(f"Strona {page} z {total_pages}")

    st.markdown("</div>", unsafe_allow_html=True)

    _render_optimization_panel(state)

    # ── Export card ───────────────────────────────────────────────────
    _render_export(df, state)

def _render_optimization_panel(state: AppState) -> None:
    """Panel konfiguracji i uruchamiania optymalizacji."""
    df = state.results_df
    if df is None or df.empty:
        return

    ok_count = int((df["status"] == "OK").sum())
    if ok_count == 0:
        return

    st.markdown(
        '<div class="section-card">'
        '<div class="section-title">'
        '<span class="step-number">⚡</span> Optymalizacja grafik'
        "</div>",
        unsafe_allow_html=True,
    )

    # ── Preset selector ──
    preset_col, info_col = st.columns([2, 3])

    with preset_col:
        preset_name = st.selectbox(
            "Preset optymalizacji",
            options=list(PRESETS.keys()),
            index=4,  # "Ecommerce"
            help="Wybierz gotowy zestaw ustawień lub dostosuj ręcznie.",
        )
        config = PRESETS[preset_name]

    with info_col:
        if config.enabled:
            st.info(
                f"📐 Max: **{config.max_width}×{config.max_height}** px · "
                f"💾 Max: **{config.max_file_size_kb} KB** · "
                f"🎨 JPEG: **{config.jpeg_quality}%** · "
                f"WebP: **{config.webp_quality}%**"
            )
        else:
            st.info("Optymalizacja wyłączona — obrazy zostaną wysłane bez zmian.")

    # ── Zaawansowane ustawienia ──
    if config.enabled:
        with st.expander("⚙️ Ustawienia zaawansowane", expanded=False):
            col1, col2, col3 = st.columns(3)

            with col1:
                st.markdown("##### 📐 Rozdzielczość")
                max_w = st.number_input(
                    "Max szerokość (px)",
                    min_value=200, max_value=10000,
                    value=config.max_width, step=100,
                )
                max_h = st.number_input(
                    "Max wysokość (px)",
                    min_value=200, max_value=10000,
                    value=config.max_height, step=100,
                )

            with col2:
                st.markdown("##### 🎨 Jakość kompresji")
                jpeg_q = st.slider(
                    "JPEG quality",
                    min_value=30, max_value=100,
                    value=config.jpeg_quality,
                    help="85 = dobry balans jakości i rozmiaru",
                )
                webp_q = st.slider(
                    "WebP quality",
                    min_value=30, max_value=100,
                    value=config.webp_quality,
                )
                max_size = st.number_input(
                    "Max rozmiar pliku (KB)",
                    min_value=100, max_value=20000,
                    value=config.max_file_size_kb, step=100,
                )

            with col3:
                st.markdown("##### 🔧 Opcje")
                convert_bmp = st.checkbox(
                    "BMP → JPEG", value=config.convert_bmp_to_jpeg,
                )
                convert_tiff = st.checkbox(
                    "TIFF → JPEG", value=config.convert_tiff_to_jpeg,
                )
                convert_webp = st.checkbox(
                    "WebP → PNG",
                    value=config.convert_webp_to_png,
                    help="Konwertuj WebP na PNG (lepsza kompatybilność)",
                )
                convert_png = st.checkbox(
                    "PNG → JPEG (bez alpha)",
                    value=config.convert_png_to_jpeg_if_no_alpha,
                    help="Tylko PNG bez przezroczystości",
                )
                strip_meta = st.checkbox(
                    "Usuń metadane EXIF",
                    value=config.strip_metadata,
                )
                sharpen = st.checkbox(
                    "Wyostrz po skalowaniu",
                    value=config.sharpen_after_resize,
                )
                progressive = st.checkbox(
                    "Progresywny JPEG",
                    value=config.progressive_jpeg,
                )

            # Nadpisz config wartościami z UI
            config = OptimizationConfig(
                enabled=True,
                max_width=max_w,
                max_height=max_h,
                max_file_size_kb=max_size,
                jpeg_quality=jpeg_q,
                webp_quality=webp_q,
                convert_bmp_to_jpeg=convert_bmp,
                convert_tiff_to_jpeg=convert_tiff,
                convert_webp_to_png=convert_webp,       # ← NOWE
                convert_png_to_jpeg_if_no_alpha=convert_png,
                strip_metadata=strip_meta,
                sharpen_after_resize=sharpen,
                progressive_jpeg=progressive,
            )

    state.optimization_config = config

    # ── Podgląd co wymaga optymalizacji ──
    if config.enabled and "_image_bytes" in df.columns:
        _render_optimization_preview(df, config)

    # ── Przycisk optymalizacji ──
    btn_col, status_col = st.columns([1, 3])

    with btn_col:
        optimize_btn = st.button(
            "⚡ Optymalizuj grafiki",
            use_container_width=True,
            disabled=not config.enabled,
        )

    if optimize_btn and config.enabled:
        with st.spinner("⚡ Optymalizacja grafik..."):
            progress = st.progress(0)

            def on_progress(done: int, total: int):
                progress.progress(done / max(total, 1))

            optimized_df, summary = optimize_dataframe(
                df, config=config, progress_callback=on_progress,
            )
            progress.progress(1.0)

        state.results_df = optimized_df
        state.optimization_summary = summary
        state.optimized = True

        with status_col:
            if summary.optimized > 0:
                st.success(
                    f"✅ Zoptymalizowano **{summary.optimized}** grafik · "
                    f"Oszczędność: **{summary.total_saved_kb:.0f} KB** "
                    f"(**{summary.total_saved_pct:.1f}%**)"
                )
            else:
                st.info("Żadna grafika nie wymagała optymalizacji.")

    # ── Wyświetl podsumowanie ──
    if state.optimization_summary and state.optimized:
        _render_optimization_summary(state.optimization_summary)

    st.markdown("</div>", unsafe_allow_html=True)


def _render_optimization_preview(
    df: pd.DataFrame, config: OptimizationConfig
) -> None:
    """Podgląd: ile grafik wymaga optymalizacji."""
    ok_df = df[df["status"] == "OK"].copy()

    needs_resize = 0
    needs_compress = 0
    needs_convert = 0
    total_size_kb = 0

    for _, row in ok_df.iterrows():
        raw = row.get("_image_bytes")
        if not isinstance(raw, bytes):
            continue

        size_kb = len(raw) / 1024
        total_size_kb += size_kb

        # Sprawdź rozdzielczość
        res = str(row.get("rozdzielczość", ""))
        if "×" in res:
            try:
                w, h = res.split("×")
                if int(w) > config.max_width or int(h) > config.max_height:
                    needs_resize += 1
            except (ValueError, TypeError):
                pass

        # Sprawdź rozmiar
        if size_kb > config.max_file_size_kb:
            needs_compress += 1

        # Sprawdź format
        ext = str(row.get("rozszerzenie", "")).upper()
        if ext in ("BMP",) and config.convert_bmp_to_jpeg:
            needs_convert += 1
        elif ext in ("TIFF",) and config.convert_tiff_to_jpeg:
            needs_convert += 1
        elif ext in ("WEBP",) and config.convert_webp_to_png:
            needs_convert += 1

    if needs_resize or needs_compress or needs_convert:
        st.markdown(
            f"""
            <div style="
                background: rgba(245, 158, 11, 0.1);
                border: 1px solid rgba(245, 158, 11, 0.3);
                border-radius: 8px;
                padding: 1rem;
                margin: 0.75rem 0;
            ">
                <strong style="color: #f59e0b;">⚠️ Wykryto grafiki do optymalizacji:</strong>
                <ul style="margin: 0.5rem 0 0; padding-left: 1.5rem; color: var(--text-secondary);">
                    {"<li>📐 <strong>" + str(needs_resize) + "</strong> z rozdzielczością > " + str(config.max_width) + "×" + str(config.max_height) + "</li>" if needs_resize else ""}
                    {"<li>💾 <strong>" + str(needs_compress) + "</strong> z rozmiarem > " + str(config.max_file_size_kb) + " KB</li>" if needs_compress else ""}
                    {"<li>🔄 <strong>" + str(needs_convert) + "</strong> do konwersji formatu</li>" if needs_convert else ""}
                    <li>📦 Łączny rozmiar: <strong>{total_size_kb:.0f} KB</strong></li>
                </ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            '<p style="color: var(--success); font-size: 0.85rem;">'
            "✅ Wszystkie grafiki mieszczą się w limitach.</p>",
            unsafe_allow_html=True,
        )


def _render_optimization_summary(summary: OptimizationSummary) -> None:
    """Wyświetla podsumowanie optymalizacji."""
    st.markdown(
        f"""
        <div class="stat-row">
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

    # Tabela szczegółów
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
            with st.expander(
                f"📋 Szczegóły optymalizacji ({len(details)} zmian)",
                expanded=False,
            ):
                st.dataframe(
                    pd.DataFrame(details),
                    use_container_width=True,
                    hide_index=True,
                )


def _render_export(df: pd.DataFrame, state: AppState) -> None:
    """Render OneDrive export section and missing EANs list."""
    accepted_df = df[~df[COL_EAN].isin(state.rejected_eans)]
    accepted_ok = accepted_df[accepted_df[COL_STATUS] == "OK"]

    # EAN-y bez grafiki (status "brak obrazu") + ręcznie odrzucone
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

    # ── Dwie kolumny: eksport i brakujące EAN-y ───────────────────────
    export_col, missing_col = st.columns(2, gap="large")

    with export_col:
        st.markdown("#### ☁️ Eksport zaakceptowanych grafik")

        st.metric("Zaakceptowane grafiki (OK)", len(accepted_ok))

        if state.export_done:
            st.success("✅ Grafiki zostały wysłane do OneDrive!")

        if not accepted_ok.empty:
            export_label = (
                "🧪 Symuluj wysyłkę"
                if state.demo_mode
                else "☁️ Wyślij do OneDrive"
            )
            if st.button(export_label, use_container_width=True):
                if state.demo_mode:
                    with st.spinner("🧪 [DEMO] Symulowanie eksportu..."):
                        simulate_webhook_delay()
                        state.export_done = True
                    st.success(
                        f"✅ [DEMO] Zasymulowano wysłanie "
                        f"{len(accepted_ok)} grafik."
                    )
                else:
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

        # CSV download
        st.markdown("---")
        export_df = df.rename(columns=DISPLAY_LABELS)
        st.download_button(
            "⬇️ Pobierz CSV z wynikami",
            data=export_df.to_csv(index=False).encode("utf-8"),
            file_name="ean_images_report.csv",
            mime="text/csv",
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

            # Użyj st.code z wbudowanym przyciskiem kopiowania
            st.code(missing_eans_text, language=None)

            # Przycisk pobierania TXT
            st.download_button(
                "⬇️ Pobierz listę jako TXT",
                data=missing_eans_text.encode("utf-8"),
                file_name="brakujace_eany.txt",
                mime="text/plain",
                use_container_width=True,
            )

            # Dodatkowa informacja
            st.caption(
                "💡 Kliknij ikonę 📋 w prawym górnym rogu pola powyżej, "
                "aby skopiować listę do schowka."
            )
        else:
            st.success("🎉 Wszystkie EAN-y mają przypisane grafiki!")
            st.markdown(
                """
                <div style="
                    text-align: center;
                    padding: 2rem;
                    color: var(--text-muted);
                ">
                    <div style="font-size: 3rem; margin-bottom: 0.5rem;">✨</div>
                    <p>Nie znaleziono żadnych EAN-ów bez grafiki.</p>
                </div>
                """,
                unsafe_allow_html=True,
            )

    st.markdown("</div>", unsafe_allow_html=True)


# ── Entry point ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()