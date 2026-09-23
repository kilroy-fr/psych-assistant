# Psych-Assistant

Ein KI-gestütztes Tool zur Erstellung von **PTV-Berichten** (Psychotherapie-Anträge an den Gutachter) für **Verhaltenstherapie**. Alle Daten bleiben lokal -- es werden ausschließlich lokale LLMs über [Ollama](https://ollama.ai/) verwendet.

## Was macht dieses Tool?

Psychotherapeuten müssen für Kassenanträge strukturierte Berichte (PTV 3) verfassen. Dieses Tool unterstützt dabei:

1. **Patientendaten hochladen** (PDF, DOCX oder Text einfügen)
2. **Automatische Berichtserstellung** durch lokale KI-Modelle
3. **Vergleich** mehrerer Modellkombinationen nebeneinander, pro Abschnitt die bessere Fassung wählen
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
                ├── RAG (LlamaIndex)       ├── gemma4:26b
                ├── DOCX-Generator         ├── qwen3:14b
                └── SSE-Fortschritt        └── nomic-embed-text (Embeddings)
```

- **Alle 6 Abschnitte** nutzen ein **2-Pass-System** (Pass 1: Faktenextraktion, Pass 2: Ausformulierung)
- **RAG-Integration** nutzt Leitlinien und Checklisten als Wissensbasis (PTV-3-Struktur, Beihilfe-Checkliste, SORC-Schema, AMDP-Befundschema, Anamnese-Leitfaden)

### Modellkombinationen

Es werden **2 Kombinationen** berechnet und nebeneinander angezeigt. In der
Vergleichstabelle lässt sich pro Abschnitt die bessere Fassung auswählen.

| Kombi | Pass 1 (alle Abschnitte) | Pass 2 (alle Abschnitte) |
|-------|--------------------------|--------------------------|
| 1 | gemma4:26b (Abschnitt 6: gemma4:12b) | qwen3:14b |
| 2 | gemma4:12b | qwen3:14b |

Beide nutzen dasselbe Pass-2-Modell, die Unterschiede entstehen also bei der
Faktenextraktion in Pass 1. Kombi 1 liefert die besseren Ergebnisse, braucht aber
deutlich länger, weil gemma4:26b nicht vollständig in 16 GB VRAM passt (ca. 5 Min.
für Pass 1). Abschnitt 6 rechnet auch Kombi 1 mit gemma4:12b, dort ist der
Qualitätsunterschied gering. Kombi 2 ist die schnelle Vergleichsspalte.

Die Aufrufe sind nach Modell gruppiert (erst gemma4:26b, dann gemma4:12b, dann alles mit
qwen3:14b), damit Modelle möglichst selten nachgeladen werden.

Zusätzlich prüft der Code:
- **BDI-Werte** werden per Regex aus der Akte gelesen und dem Modell als feste, sortierte
  Liste vorgegeben.
- **SORC-Konsequenzen:** Das Modell beantwortet je Konsequenz nur „tritt ein oder fällt
  weg?“ und „angenehm oder unangenehm?“; die Zuordnung zu C+, C-, C+/ und C-/ macht der Code.
- **Anonymisierung:** Name, Initialen und Geburtsdatum der Patientin/des Patienten werden
  aus dem Aktenkopf gelesen und im Bericht ersetzt. Namen Dritter (Personen, Arbeitgeber,
  Einrichtungen, Orte) werden gesucht und nur in den betroffenen Satzteilen durch Rollen
  ersetzt ("der Sohn", "ein Logistikunternehmen"), mit `[Prüfhinweis: …]`.
- **Diagnosen** mit formalen Fehlern (z.B. `F50.x`), einer nicht offiziellen Bezeichnung
  oder einem Schweregrad, der nicht zum jüngsten BDI-II-Wert passt, werden mit
  `[Prüfhinweis: …]` markiert. Ebenso, wenn eine Diagnose, die die Akte durchgängig nur
  als Verdacht führt, im Bericht als gesichert erscheint (unter Haupt-/Nebendiagnose(n)
  statt Differenzialdiagnose(n), oder als "gesichert"/"bestätigt" formuliert).
- **Medikamentendosis:** Abschnitt 3.2 wird gegen die zuletzt in der Akte dokumentierte
  Dosis je Wirkstoff geprüft; weicht sie ohne Datumsbezug ("Stand: …") ab, gibt es einen
  `[Prüfhinweis: …]`.
- **Vollständigkeit:** Fehlen in Abschnitt 1–3 Unterabschnitte, wird neu gerechnet und
  notfalls markiert. Der psychopathologische Befund (2.3) wird auf die 11 Begriffe geprüft.
- **Termine per Kalenderwoche** ("Reha in KW 4") werden in ein Datum umgerechnet.

### Datenschutz

Hochgeladene Akten werden nach dem Einlesen sofort gelöscht. `data/debug_results.log`
enthält standardmäßig keine Akteninhalte, nur Metadaten und die BDI-Liste. Zur Fehlersuche
lassen sich Inhalte mit `LOG_PATIENT_CONTENT=1` in `docker-compose.yml` einschalten; das
Log enthält dann Echtdaten.

## Voraussetzungen

- **Docker** und **Docker Compose**
- **[Ollama](https://ollama.ai/)** auf einem erreichbaren Host (lokal oder im Netzwerk)
- Mindestens **16 GB VRAM** (GPU) für die verwendeten Modelle
- Folgende Modelle in Ollama installiert:

```bash
ollama pull gemma4:26b         # Pass 1 (Kombi 1)
ollama pull gemma4:12b         # Pass 1 (Kombi 2)
ollama pull qwen3:14b          # Pass 2 (alle), Namenprüfung
ollama pull nomic-embed-text   # RAG-Embeddings
```

### Python-Abhängigkeiten

Alle Pakete sind in `requirements.txt` exakt gepinnt und werden beim Image-Bau
installiert -- eine lokale Python-Installation ist für den Betrieb nicht nötig.
Stand: September 2026.

| Paket | Version | Wofür |
|-------|---------|-------|
| flask | 3.1.3 | Web-Backend, Server-Sent Events, Datei-Upload |
| llama-index / llama-index-core | 0.14.25 | RAG-Framework und Vector-Store |
| llama-index-llms-ollama | 0.11.0 | LLM-Anbindung an Ollama |
| llama-index-embeddings-ollama | 0.10.0 | Embeddings (`nomic-embed-text`) |
| pydantic | 2.13.5 | Transitive Abhängigkeit, bewusst gepinnt |
| requests | 2.34.2 | Direkte Aufrufe der Ollama-HTTP-API |
| python-docx | 1.2.0 | DOCX-Export und Einlesen hochgeladener DOCX-Dateien |
| pypdf | 6.19.0 | Einlesen hochgeladener PDF-Dateien |

Nach einer Änderung an `requirements.txt` muss das Image neu gebaut werden:

```bash
docker-compose up -d --build
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

Die verwendeten Modelle stehen in [app/app.py](app/app.py):

```python
MODEL_COMBINATIONS = [
    {"pass1": "gemma4:26b", "pass2": "qwen3:14b", "pass1_override": {"6": "gemma4:12b"}},
    {"pass1": "gemma4:12b", "pass2": "qwen3:14b"},  # schnelle Vergleichsspalte
]

NAME_CHECK_MODEL = "qwen3:14b"   # Namenprüfung im fertigen Bericht
```

`pass2_temperature` ist optional und steuert die Basis-Temperatur von Pass 2
(Standard: 0.1). `pass1_override` ist optional und legt für einzelne Abschnitte
(`"1-3"`, `"4"`, `"5"`, `"6"`) ein anderes Pass-1-Modell fest.

`MODEL_COMBINATIONS` ist die einzige Stelle für die Modelle. Berechnung,
Fortschrittsanzeige, Vergleichstabelle und DOCX lesen daraus. Eine Kombi entfernen
oder hinzufügen heißt also nur: einen Eintrag streichen oder ergänzen und den
Container neu bauen. Das Kontextfenster wird automatisch auf das Maximum des Modells
begrenzt.

### RAG-Wissensbasis

Die Leitlinien-Dokumente liegen in `data/guidelines/`. Dort gehören nur Leitfäden und
Schemata hin, **keine Beispielberichte** — alles in diesem Verzeichnis landet im
Pass-1-Prompt, und das Modell übernimmt sonst Fakten des Beispielfalls in den Bericht.
Beispielberichte liegen in `data/examples/` (nicht im Index). Der Index erkennt Änderungen
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
│   ├── guidelines/           # Leitlinien und Schemata (JSON, im RAG-Index)
│   └── examples/             # Beispielberichte (nicht im RAG-Index)
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
2. Patientendaten als **Text einfügen** oder **Datei hochladen** (PDF, DOCX, TXT --
   PDF wird über `pypdf`, DOCX über `python-docx` eingelesen)
3. **"Bericht erstellen"** klicken
4. Der Fortschritt wird live per Server-Sent Events angezeigt
5. Nach Abschluss: Ergebnisse der Modellkombinationen in einer **Vergleichstabelle** sehen
6. Beste Passagen pro Abschnitt auswählen und als **Word-Dokument exportieren**

## Version

Die aktuelle Version steht in `VERSION` und wird als Badge oben rechts im Header
angezeigt. Ein lokaler Git-Hook erhöht die Patch-Version bei jedem Commit automatisch.

## Hinweise

- Alle Daten bleiben **lokal** -- keine Cloud-APIs, keine externen Dienste
- Die generierten Berichte sind **Entwürfe** und müssen vor Verwendung fachlich geprüft und angepasst werden
- Das Tool ersetzt keine fachliche Expertise, sondern unterstützt bei der Formulierung
- Für Fragen zur Modellauswahl siehe [MODELLEMPFEHLUNGEN.md](MODELLEMPFEHLUNGEN.md)
- Zur Rolle der einzelnen Leitliniendokumente siehe [DOKUMENTEN_ROLLEN.md](DOKUMENTEN_ROLLEN.md)

## Lizenz

MIT License -- siehe [LICENSE](LICENSE)
