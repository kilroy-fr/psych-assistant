# Psych-Assistant

Ein KI-gestütztes Tool zur Erstellung von **PTV-Berichten** (Psychotherapie-Anträge an den Gutachter) für **Verhaltenstherapie**. Alle Daten bleiben lokal -- es werden ausschließlich lokale LLMs über [Ollama](https://ollama.ai/) verwendet.

## Was macht dieses Tool?

Psychotherapeuten müssen für Kassenanträge strukturierte Berichte (PTV 3) verfassen. Dieses Tool unterstützt dabei:

1. **Patientendaten hochladen** (PDF, DOCX oder Text einfügen)
2. **Automatische Berichtserstellung** durch lokale KI-Modelle
3. **Vergleich** mehrerer Modellkombinationen nebeneinander
4. **Export** als Word-Dokument (.docx)

Der generierte Bericht folgt der PTV-3-Gliederung für VT-Umwandlungsanträge mit 6 Abschnitten:

| Nr. | Abschnitt |
|-----|-----------|
| 1 | Relevante soziodemographische Daten |
| 2 | Symptomatik und psychischer Befund |
| 3 | Somatischer Befund |
| 4 | Lebensgeschichte und verhaltenstherapeutische Zusammenhänge |
| 5 | Diagnose nach ICD-10 |
| 6 | Behandlungsplan und Prognose |

## Architektur

```
Browser  ──▶  Flask-App (Docker)  ──▶  Ollama (lokale LLMs)
                │                          │
                ├── RAG (LlamaIndex)       ├── qwen3:14b
                ├── DOCX-Generator         ├── gpt-oss:20b
                └── SSE-Fortschritt        └── deepseek-r1:14b
```

- **Abschnitte 1-3** werden im **1-Pass-System** generiert (ein LLM erstellt den fertigen Text direkt)
- **Abschnitte 4 und 6** nutzen ein **2-Pass-System** (Pass 1: Faktenextraktion, Pass 2: Ausformulierung)
- **Abschnitt 5** (Diagnosen) wird im **1-Pass-System** mit einem kleineren Modell erstellt
- **RAG-Integration** nutzt Leitlinien und Checklisten als Wissensbasis (PTV-3-Struktur, Beihilfe-Checkliste)

## Voraussetzungen

- **Docker** und **Docker Compose**
- **[Ollama](https://ollama.ai/)** auf einem erreichbaren Host (lokal oder im Netzwerk)
- Mindestens **16 GB VRAM** (GPU) für die verwendeten Modelle
- Folgende Modelle in Ollama installiert:

```bash
ollama pull qwen3:14b
ollama pull gpt-oss:20b
ollama pull deepseek-r1:14b
ollama pull qwen3:8b           # für Abschnitt 5
ollama pull nomic-embed-text   # für RAG-Embeddings
```

## Installation

1. **Repository klonen:**
   ```bash
   git clone https://github.com/kilroy-fr/psych-assistant.git
   cd psych-assistant
   ```

2. **Docker-Netzwerk erstellen** (falls Ollama in einem separaten Container läuft):
   ```bash
   docker network create ollama-net
   ```

3. **Container starten:**
   ```bash
   docker-compose up -d --build
   ```

4. **Anwendung öffnen:**
   ```
   http://localhost:5005
   ```

## Konfiguration

### Ollama-Host

Der Ollama-Host wird über die Umgebungsvariable `OLLAMA_HOST` konfiguriert (Standard: `http://ollama:11434`). Für lokale Entwicklung ohne Docker kann der Host in `docker-compose.yml` angepasst werden.

### Modellkombinationen

Die verwendeten Modelle können in [app/app.py](app/app.py) angepasst werden:

```python
MODEL_COMBINATIONS = [
    {"pass1": "qwen3:14b", "pass2": "gpt-oss:20b"},
    {"pass1": "qwen3:14b", "pass2": "deepseek-r1:14b"},
]
```

### RAG-Wissensbasis

Die Leitlinien-Dokumente liegen in `data/guidelines/`. Um den RAG-Index neu zu erstellen:

```bash
docker exec psych-assistant rm -rf /app/storage/*
docker restart psych-assistant
```

## Projektstruktur

```
psych-assistant/
├── app/
│   ├── app.py                # Flask-Backend, Orchestrierung
│   ├── docx_generator.py     # Word-Dokument-Erstellung
│   ├── rag/                  # RAG-System (LlamaIndex + Ollama)
│   │   ├── build_index.py    # Index-Erstellung
│   │   └── query_engine.py   # RAG-Abfragen
│   ├── static/               # CSS, JS, Assets
│   └── templates/            # HTML-Template
├── data/
│   ├── guidelines/           # Leitlinien und Checklisten (JSON)
│   └── examples/             # Musterbeispiele für Prompts
├── storage/                  # Vector-Store für RAG (auto-generiert)
├── prompt1.txt               # Prompt Abschnitte 1-3
├── prompt4-1.txt / 4-2.txt   # Prompts Abschnitt 4 (2-Pass)
├── prompt5-1.txt             # Prompt Abschnitt 5
├── prompt6-1.txt / 6-2.txt   # Prompts Abschnitt 6 (2-Pass)
├── *_m.txt                   # Männliche Prompt-Varianten
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

## Verwendung

1. Anwendung im Browser öffnen (`http://localhost:5005`)
2. Patientendaten als **Text einfügen** oder **Datei hochladen** (PDF, DOCX, TXT)
3. **"Bericht erstellen"** klicken
4. Der Fortschritt wird live per Server-Sent Events angezeigt
5. Nach Abschluss: Ergebnisse der Modellkombinationen in einer **Vergleichstabelle** sehen
6. Beste Passagen pro Abschnitt auswählen und als **Word-Dokument exportieren**

## Hinweise

- Alle Daten bleiben **lokal** -- keine Cloud-APIs, keine externen Dienste
- Die generierten Berichte sind **Entwürfe** und müssen vor Verwendung fachlich geprüft und angepasst werden
- Das Tool ersetzt keine fachliche Expertise, sondern unterstützt bei der Formulierung
- Für Fragen zur Modellauswahl siehe [MODELLEMPFEHLUNGEN.md](MODELLEMPFEHLUNGEN.md)

## Lizenz

MIT License -- siehe [LICENSE](LICENSE)
