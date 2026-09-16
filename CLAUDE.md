# Psych-Assistant

## Projektbeschreibung

KI-gestütztes Tool zur Erstellung von PTV-Berichten (Psychotherapie-Anträge) für Verhaltenstherapie.
Verwendet lokale LLMs via Ollama mit einem Multi-Pass-System und RAG-Integration.

## Architektur

- **Backend:** Flask (Python 3.11), läuft in Docker
- **LLM-Anbindung:** Ollama (lokal, keine Cloud-API)
- **RAG:** LlamaIndex mit Ollama-Embeddings (`nomic-embed-text`)
- **Frontend:** Vanilla HTML/CSS/JS (kein Framework)
- **DOCX-Export:** python-docx

## Abhängigkeiten

Alle Pakete in `requirements.txt` sind exakt gepinnt (inkl. `pydantic`), damit
`docker-compose up --build` reproduzierbar bleibt. Stand: September 2026.

| Paket | Version | Rolle |
|-------|---------|-------|
| `flask` | 3.1.3 | Web-Backend, SSE, Datei-Upload |
| `llama-index` / `llama-index-core` | 0.14.24 | RAG-Framework, Vector-Store |
| `llama-index-llms-ollama` | 0.11.0 | LLM-Anbindung (LlamaIndex-Seite) |
| `llama-index-embeddings-ollama` | 0.10.0 | Embeddings via `nomic-embed-text` |
| `pydantic` | 2.13.5 | Transitiv über LlamaIndex, bewusst gepinnt |
| `requests` | 2.34.2 | Direkte Ollama-API-Calls (`/api/generate`, `/api/tags`) |
| `python-docx` | 1.2.0 | DOCX-Export **und** DOCX-Upload-Parsing |
| `pypdf` | 6.18.1 | PDF-Upload-Parsing (`extract_text_from_files`) |

Regeln beim Anheben von Versionen:

- `llama-index` und `llama-index-core` müssen dieselbe Version haben — `llama-index`
  pinnt `llama-index-core` exakt (`>=0.14.24,<0.15.0`).
- `pypdf` ist keine optionale Abhängigkeit: `extract_text_from_files()` in
  `query_engine.py` fängt den `ImportError` ab und fällt auf `pdfplumber` zurück;
  fehlen beide, werden hochgeladene PDFs **stillschweigend** als leer behandelt.
  `pdfplumber` ist nicht installiert, `pypdf` ist damit der einzige PDF-Pfad.
- `docx2txt` und `python-dotenv` wurden entfernt (September 2026) — beide wurden
  nirgends importiert. `docx2txt` würde nur gebraucht, wenn `data/guidelines/`
  DOCX-Dateien enthielte; der `SimpleDirectoryReader` liest dort aktuell nur JSON und TXT.

Nach jeder Änderung an `requirements.txt` ist ein Image-Neubau nötig:
`docker-compose up -d --build`.

## Modellkombinationen (2 Kombis)

Alle Kombis teilen Pass1 und den Pass2 für Abschnitte 1-3 und 5 — nur Pass2 für Abschnitte 4 und 6 differenziert.

| Kombi | Pass 1 (alle) | Pass 2 (1-3, 5: geteilt) | Pass 2 (4, 6: je Kombi) |
|-------|---------------|---------------------------|--------------------------|
| 1 | gemma4:12b | gemma4:12b | deepseek-r1:14b |
| 2 | gemma4:12b | gemma4:12b | gemma4:12b (T=0.65) |

`gemma4:12b` als Pass-1-Modell: kompaktes 12B-Modell mit gutem Verhältnis aus Geschwindigkeit und Qualität.

Kombi 2 nutzt gemma4:12b auch als Pass-2-Modell mit höherer Temperatur (0.65 vs. Standard 0.1)
→ kreativere/vielfältigere Formulierungen als Vergleichsvariante.

Abschnitte 1–3 und 5: 2-Pass mit gemma4:12b → identisches Ergebnis in allen 2 Kombis.

### Wo die Modellnamen wirklich stehen

Achtung beim Umstellen von Modellen — die Namen liegen an drei Stellen in `app.py`:

1. **`MODEL_COMBINATIONS`** bestimmt nur noch die *Anzahl* der Kombis (`n_combos`) und
   die Modellnamen in der Vergleichstabelle der UI. Der Wert `pass1` wird von
   `run_computation_task()` **nicht** gelesen.
2. **`MODEL_COMBINATIONS_SECTION4_5_6`** liefert das tatsächlich benutzte Pass-2-Modell
   und `pass2_temperature` für die Abschnitte 4 und 6.
3. **Hartkodierte `"gemma4:12b"`-Literale** in `run_computation_task()` für alle
   Pass-1-Läufe sowie für Pass 2 der Abschnitte 1-3 und 5.

Ein Modellwechsel muss deshalb in beiden Listen *und* bei den Literalen erfolgen,
sonst zeigt die UI ein anderes Modell an als gerechnet wurde.

Die Helfer `run_section4()`, `run_section5()` und `run_section6()` sind Altlasten aus
der Zeit vor `run_computation_task()` und werden nirgends mehr aufgerufen.

### Ausführungsreihenfolge (`run_computation_task`)

**Phase 1** — Alle gemma4:12b Läufe sequenziell (Modell bleibt im VRAM):
- Pass1 für Abschnitte 1-3, 4, 5, 6
- Pass2 für Abschnitte 1-3 und 5 (gemma4:12b, geteilt)

**Phase 2** — Pass2-Läufe für Abschnitte 4 und 6, je Kombi anderes Modell.

Die Kombis laufen dabei **nicht** in Kombi-Reihenfolge, sondern sortiert: Kombis,
deren Pass-2-Modell schon aus Phase 1 im VRAM liegt (`gemma4:12b`), zuerst. Sonst
würde gemma entladen, deepseek geladen und gemma danach ein zweites Mal geladen.
Die Sortierung ist stabil — kommen weitere Kombis dazu, wandern alle gemma-Kombis
nach vorn, der Rest behält seine relative Ordnung.

Die Ergebnisse werden deshalb per Index in ein vorbelegtes `results_by_combo`
geschrieben, nicht per `append()`. Das hält die Kombi-Reihenfolge in den
DOCX-Spalten und der Vergleichstabelle stabil, weil beide ihre Modellnamen aus
`MODEL_COMBINATIONS` in Originalreihenfolge ziehen. Wer die Schleife anfasst:
Ausführungsreihenfolge und Ergebnisreihenfolge sind hier bewusst entkoppelt.

In der Fortschrittsanzeige leuchtet dadurch Kombi 2 vor Kombi 1 auf — das ist
korrekt, sie wird ja auch zuerst gerechnet. Die Zeitentabelle sortiert in
`renderTimingTable()` selbst nach Kombi und bleibt unbeeinflusst.

### Kontextfenster-Logik (`query_engine.py`)

Bei zu langem Eingabetext greift eine zweistufige Kürzung:
1. Guidelines: 10 → 3 Chunks
2. Patientendaten: Chunks von hinten entfernen bis Prompt ins Fenster passt

Modellgrößen-Erkennung via Namens-Pattern (`:12b`, `:14b` etc.) → `num_ctx_rag` 8K–49K.

### Index-Aktualität (`build_index.py`)

`build_index()` schreibt beim Neubau einen Fingerabdruck nach `storage/index_fingerprint.json`
und vergleicht ihn bei jedem Start. Enthalten sind:

- SHA-256 jeder Datei in `data/guidelines/` (erkennt geänderte, neue, gelöschte Dateien)
- Hash von `DOCUMENT_METADATA` (geänderte Rollenzuweisung erzwingt Neubau)
- Name des Embedding-Modells (`nomic-embed-text`)
- Formatversion des Fingerabdrucks (`FINGERPRINT_VERSION`)

Bei Abweichung wird der Index automatisch neu gebaut und der Grund geloggt.
Der Fingerabdruck wird erst nach erfolgreichem Persistieren geschrieben — bricht der
Build ab (z.B. Ollama nicht erreichbar), gilt der Index weiterhin als veraltet.

`build_index(force_rebuild=True)` erzwingt einen Neubau.

## Berichtsstruktur (6 Abschnitte)

| Abschnitt | Thema | Methode |
|-----------|-------|---------|
| 1-3 | Soziodemographie, Symptomatik, Somatik | 2-Pass (prompt1-1.txt → prompt1-2.txt) |
| 4 | Lebensgeschichte/Bedingungsmodell | 2-Pass (prompt4-1.txt → prompt4-2.txt) |
| 5 | Diagnose nach ICD-10 | 2-Pass (prompt5-1.txt → prompt5-2.txt) |
| 6 | Behandlungsplan/Prognose | 2-Pass (prompt6-1.txt → prompt6-2.txt) |

## Wichtige Dateien

- `app/app.py` — Flask-Backend, Modellkombinationen, Abschnitts-Orchestrierung
- `app/docx_generator.py` — Word-Dokument-Erstellung mit Schema-Validierung
- `app/rag/query_engine.py` — RAG-Abfragen gegen Ollama
- `app/rag/build_index.py` — Index-Erstellung aus Leitlinien-Dokumenten
- `app/static/main.js` — Frontend-Logik (SSE, Vergleichstabelle)
- `app/templates/index.html` — Hauptseite
- `data/guidelines/` — Leitlinien und Checklisten für RAG (6 Dateien, JSON + TXT)

`prompt*_m.txt` (männliche Varianten) liegen im Repo, werden aber **nicht verwendet**:
weder von `app.py` geladen noch im Dockerfile ins Image kopiert. Die Genus-Anpassung
läuft stattdessen über das `alternatives`-Array im Report-Schema (`docx_generator.py`).

## Konventionen

- Sprache im Code: Deutsch (Kommentare, Variablennamen teilweise gemischt)
- Umlaute in Strings vermeiden wo möglich (Kompatibilität)
- Prompts als externe .txt-Dateien, nicht inline im Code
- Docker-Netzwerk `ollama-net` verbindet App mit Ollama-Container

## Lokale Entwicklung

```bash
# Container starten
docker-compose up -d --build

# Logs prüfen
docker logs -f psych-assistant
```

### RAG-Index

Geänderte Dateien in `data/guidelines/` werden automatisch erkannt — ein Neustart
genügt, der Index wird beim nächsten Zugriff neu gebaut:

```bash
docker restart psych-assistant
```

Vollständiger Reset (z.B. bei beschädigtem Storage):

```bash
docker exec psych-assistant rm -rf /app/storage/*
docker restart psych-assistant
```

Index-Zustand prüfen:

```bash
docker exec psych-assistant python -c "from app.rag.build_index import build_index; i = build_index(); print(len(i.docstore.docs), 'Nodes')"
```
