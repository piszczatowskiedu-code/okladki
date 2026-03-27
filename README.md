# EAN Image Manager 🖼️

Aplikacja Streamlit do pobierania, analizy i eksportowania grafik produktowych
na podstawie kodów EAN — zintegrowana z Microsoft Power Automate i OneDrive.

---

## 📁 Struktura projektu

```
ean_image_app/
├── app.py                  # Główna aplikacja Streamlit (UI)
├── config.py               # Konfiguracja (URL-e webhooków, limity)
├── ean_processor.py        # Komunikacja z webhookiem Power Automate (pobieranie URL-i)
├── image_analyzer.py       # Pobieranie i analiza grafik (wielowątkowe)
├── onedrive_exporter.py    # Eksport do OneDrive (przez webhook lub Graph API)
├── requirements.txt        # Zależności Python
└── README.md               # Ta instrukcja
```

---

## ⚙️ Wymagania

- Python 3.11+
- pip

---

## 🚀 Uruchomienie

### 1. Sklonuj / skopiuj pliki projektu

```bash
mkdir ean_image_app && cd ean_image_app
# skopiuj wszystkie pliki projektu
```

### 2. Utwórz środowisko wirtualne (zalecane)

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
```

### 3. Zainstaluj zależności

```bash
pip install -r requirements.txt
```

### 4. Skonfiguruj URL-e webhooków

**Opcja A — bezpośrednio w `config.py`:**

```python
WEBHOOK_URL_FETCH   = "https://prod-XX.westeurope.logic.azure.com/..."
WEBHOOK_URL_ONEDRIVE = "https://prod-XX.westeurope.logic.azure.com/..."
```

**Opcja B — przez zmienne środowiskowe (zalecane dla produkcji):**

```bash
export WEBHOOK_URL_FETCH="https://prod-XX.westeurope.logic.azure.com/..."
export WEBHOOK_URL_ONEDRIVE="https://prod-XX.westeurope.logic.azure.com/..."
```

**Opcja C — jeśli używasz Microsoft Graph API bezpośrednio:**

```bash
export GRAPH_ACCESS_TOKEN="eyJ0eXAi..."
export ONEDRIVE_ROOT_FOLDER="EAN_Images"
```

### 5. Uruchom aplikację

```bash
streamlit run app.py
```

Aplikacja otworzy się automatycznie w przeglądarce pod adresem `http://localhost:8501`.

---

## 🔌 Konfiguracja Power Automate

### Webhook 1 — Pobieranie URL-i grafik

**Trigger:** HTTP Request  
**Method:** POST  
**Oczekiwany payload:**
```json
{
  "eans": ["1234567890123", "9876543210987"]
}
```

**Oczekiwana odpowiedź:**
```json
{
  "results": {
    "1234567890123": [
      "https://cdn.example.com/img/product1.jpg",
      "https://cdn.example.com/img/product1_2.jpg"
    ],
    "9876543210987": []
  }
}
```

> Akceptowany jest też płaski dict bez klucza `"results"`.

---

### Webhook 2 — Eksport do OneDrive

**Trigger:** HTTP Request  
**Method:** POST  
**Oczekiwany payload (jeden plik na żądanie):**
```json
{
  "ean": "1234567890123",
  "filename": "1234567890123_0.jpg",
  "folder": "EAN_Images/1234567890123",
  "content_base64": "<base64 encoded image>",
  "source_url": "https://cdn.example.com/img/product1.jpg",
  "resolution": "800×600",
  "size": "245.3 KB",
  "extension": "jpg"
}
```

**Akcja w Power Automate:**  
`Create file` w OneDrive → użyj `folder`/`filename` jako ścieżki,  
`base64ToBinary(triggerBody()?['content_base64'])` jako zawartości pliku.

---

## 🔧 Parametry konfiguracyjne (`config.py`)

| Parametr | Domyślnie | Opis |
|---|---|---|
| `BATCH_SIZE` | `50` | Liczba EAN-ów wysyłanych w jednym żądaniu |
| `MAX_WORKERS` | `10` | Liczba równoległych wątków pobierania |
| `IMAGE_TIMEOUT` | `15` s | Timeout pobierania pojedynczego obrazu |
| `MAX_IMAGE_MB` | `20` MB | Maksymalny rozmiar pliku |
| `HTTP_TIMEOUT` | `30` s | Timeout komunikacji z webhookiem |
| `MAX_EANS_TOTAL` | `5000` | Limit EAN-ów jednorazowo |

---

## 📦 Wdrożenie produkcyjne

### Streamlit Community Cloud

1. Wgraj projekt na GitHub.
2. Dodaj sekrety w panelu Streamlit Cloud (`Settings → Secrets`):
   ```toml
   WEBHOOK_URL_FETCH = "https://..."
   WEBHOOK_URL_ONEDRIVE = "https://..."
   ```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
```

```bash
docker build -t ean-image-manager .
docker run -p 8501:8501 \
  -e WEBHOOK_URL_FETCH="https://..." \
  -e WEBHOOK_URL_ONEDRIVE="https://..." \
  ean-image-manager
```

---

## 🧪 Testowanie bez webhooków

Aby przetestować aplikację bez rzeczywistych webhooków, możesz tymczasowo
w `ean_processor.py` w funkcji `_send_batch` zastąpić wywołanie HTTP
przykładowymi danymi:

```python
# MOCK — usuń po testach
return {
    eans[0]: ["https://via.placeholder.com/800x600.jpg"],
    eans[1]: ["https://via.placeholder.com/1200x900.png"],
}
```

---

## 📋 Workflow aplikacji

```
Użytkownik wkleja EAN-y
        ↓
POST do Webhooka 1 (Power Automate)
        ↓ 
Odbiór {EAN → [URL1, URL2, ...]}
        ↓
Równoległe pobieranie obrazów (wielowątkowo)
        ↓
Analiza: rozdzielczość, rozmiar, format (Pillow)
        ↓
Tabela wyników z checkboxami (odrzuć/akceptuj)
        ↓
POST do Webhooka 2 (Power Automate → OneDrive)
   lub bezpośrednio przez Microsoft Graph API
        ↓
Grafiki zapisane w OneDrive: /EAN_Images/{EAN}/{plik}
```
