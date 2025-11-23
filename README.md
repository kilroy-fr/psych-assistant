# Psych-Assistant

Ein KI-gestütztes Tool zur Erstellung von psychotherapeutischen Berichten (PTV-Berichte) für Verhaltenstherapie.

## Funktionen

- **2-Pass-System**: Kombiniert verschiedene LLM-Modelle für optimale Ergebnisse
  - Pass 1: Fakten-Extraktion aus Patientendaten
  - Pass 2: Formulierung des strukturierten Berichts
- **4 Modellkombinationen**: Vergleicht automatisch verschiedene Modellkombinationen (qwen2.5, qwen3, gpt-oss, deepseek-r1)
- **RAG-Integration**: Nutzt Leitlinien und Checklisten als Wissensbasis
- **DOCX-Export**: Generiert fertige Word-Dokumente mit Vergleichstabelle oder Fließtext
- **Web-Interface**: Einfache Bedienung über den Browser

## Voraussetzungen

- Docker & Docker Compose
- [Ollama](https://ollama.ai/) mit den benötigten Modellen:
  - `qwen2.5:14b`
  - `qwen3:14b`
  - `gpt-oss:20b`
  - `deepseek-r1:14b`

## Installation

1. Repository klonen:
   ```bash
   git clone https://github.com/DEIN-USERNAME/psych-assistant.git
   cd psych-assistant
   ```

2. Docker-Netzwerk erstellen (falls nicht vorhanden):
   ```bash
   docker network create ollama-net
   ```

3. Container starten:
   ```bash
   docker-compose up -d --build
   ```

4. Anwendung aufrufen:
   - HTTPS: `https://psych.domain.local`
   - HTTP (direkt): `http://localhost:5005`

## Projektstruktur

```
psych-assistant/
├── app/
│   ├── app.py              # Flask-Backend
│   ├── docx_generator.py   # Word-Dokument-Erstellung
│   ├── rag/                # RAG-System (Index & Query)
│   ├── static/             # CSS, JS, Assets
│   └── templates/          # HTML-Templates
├── data/
│   └── guidelines/         # Leitlinien und Checklisten (JSON)
├── storage/                # Vector-Store für RAG
├── prompt1.txt             # System-Prompt Pass 1
├── prompt2.txt             # System-Prompt Pass 2
├── docker-compose.yml
└── Dockerfile
```

## Verwendung

1. Patientendaten als Text einfügen oder Datei hochladen
2. "Bericht erstellen" klicken
3. Die 4 Modellkombinationen werden nacheinander ausgeführt
4. Ergebnisse werden in einer Vergleichstabelle angezeigt
5. Beste Passagen auswählen und als Word-Dokument exportieren

## Konfiguration

Die Modellkombinationen können in `app/app.py` angepasst werden:

```python
MODEL_COMBINATIONS = [
    {"pass1": "qwen2.5:14b", "pass2": "gpt-oss:20b"},
    {"pass1": "qwen2.5:14b", "pass2": "deepseek-r1:14b"},
    {"pass1": "qwen3:14b", "pass2": "gpt-oss:20b"},
    {"pass1": "qwen3:14b", "pass2": "deepseek-r1:14b"},
]
```

## Hinweise

- Die Anwendung ist für den lokalen/privaten Einsatz konzipiert
- Patientendaten werden nicht extern übertragen (alle Modelle laufen lokal via Ollama)
- Die generierten Berichte sollten vor der Verwendung geprüft und angepasst werden
