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
                ├── RAG (LlamaIndex)       ├── gemma4:12b
                ├── DOCX-Generator         ├── deepseek-r1:14b
                └── SSE-Fortschritt        └── nomic-embed-text (Embeddings)
```

- **Alle 6 Abschnitte** nutzen ein **2-Pass-System** (Pass 1: Faktenextraktion, Pass 2: Ausformulierung)
- **RAG-Integration** nutzt Leitlinien und Checklisten als Wissensbasis (PTV-3-Struktur, Beihilfe-Checkliste, SORC-Schema, AMDP-Befundschema, Anamnese-Leitfaden)

### Modellkombinationen

Es werden **2 Kombinationen** parallel berechnet und zum Vergleich nebeneinander gestellt.
Beide teilen sich Pass 1 sowie Pass 2 der Abschnitte 1-3 und 5 — unterschiedlich ist nur
Pass 2 der Abschnitte 4 und 6:

| Kombi | Pass 1 (alle) | Pass 2 (1-3, 5) | Pass 2 (4, 6) |
|-------|---------------|------------------|----------------|
| 1 | gemma4:12b | gemma4:12b | deepseek-r1:14b |
| 2 | gemma4:12b | gemma4:12b | gemma4:12b (Temperatur 0.65) |

Kombi 2 verwendet dasselbe Modell mit höherer Temperatur (0.65 statt 0.1) und liefert
dadurch kreativere Formulierungen als Vergleichsvariante. Da die Abschnitte 1-3 und 5
geteilt sind, liefern beide Kombis dort identische Ergebnisse.

Die Läufe sind so sortiert, dass alle `gemma4:12b`-Aufrufe zusammenhängend laufen —
das Modell bleibt dabei im VRAM und muss nicht mehrfach geladen werden.

## Voraussetzungen

- **Docker** und **Docker Compose**
- **[Ollama](https://ollama.ai/)** auf einem erreichbaren Host (lokal oder im Netzwerk)
- Mindestens **16 GB VRAM** (GPU) für die verwendeten Modelle
- Folgende Modelle in Ollama installiert:

```bash
ollama pull gemma4:12b         # Pass 1 (alle Abschnitte) + Pass 2
ollama pull deepseek-r1:14b    # Pass 2 der Abschnitte 4 und 6 (Kombi 1)
ollama pull nomic-embed-text   # RAG-Embeddings
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
    {"pass1": "gemma4:12b", "pass2": "deepseek-r1:14b"},
    {"pass1": "gemma4:12b", "pass2": "gemma4:12b", "pass2_temperature": 0.65},
]
```

`pass2_temperature` ist optional und steuert die Basis-Temperatur von Pass 2
(Standard: 0.1).

### RAG-Wissensbasis

Die Leitlinien-Dokumente liegen in `data/guidelines/`. Der Index erkennt Änderungen
automatisch: beim Start werden die Prüfsummen aller Quelldateien mit dem gespeicherten
Fingerabdruck (`storage/index_fingerprint.json`) verglichen. Weicht etwas ab — geänderte,
neue oder gelöschte Dateien, ein anderes Embedding-Modell oder geänderte Dokument-Metadaten —
wird der Index neu gebaut und der Grund im Log ausgegeben.

Nach dem Bearbeiten der Leitlinien genügt also ein Neustart:

```bash
docker restart psych-assistant
```

Vollständiger Reset (nur nötig bei beschädigtem Storage):

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
│   ├── guidelines/           # Leitlinien und Checklisten (JSON + TXT)
│   └── examples/             # Musterbeispiele für Prompts
├── storage/                  # Vector-Store für RAG (auto-generiert)
├── prompt1-1.txt / 1-2.txt   # Prompts Abschnitte 1-3 (2-Pass)
├── prompt4-1.txt / 4-2.txt   # Prompts Abschnitt 4 (2-Pass)
├── prompt5-1.txt / 5-2.txt   # Prompts Abschnitt 5 (2-Pass)
├── prompt6-1.txt / 6-2.txt   # Prompts Abschnitt 6 (2-Pass)
├── prompt1.txt               # Nur zur Anzeige in der UI
├── docker-compose.yml
├── Dockerfile
├── Caddyfile                 # Reverse-Proxy-Konfiguration
└── requirements.txt
```

> Die Dateien `prompt*_m.txt` (männliche Varianten) liegen noch im Repository, werden
> aber nicht mehr verwendet. Die geschlechtsspezifische Anpassung läuft über das
> `alternatives`-Array im Report-Schema.

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
- Zur Rolle der einzelnen Leitliniendokumente siehe [DOKUMENTEN_ROLLEN.md](DOKUMENTEN_ROLLEN.md)

## Lizenz

MIT License -- siehe [LICENSE](LICENSE)
