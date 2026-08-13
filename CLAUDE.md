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

Abhängigkeiten in `requirements.txt` sind exakt gepinnt (inkl. `pydantic`), damit
`docker-compose up --build` reproduzierbar bleibt. `llama-index` und `llama-index-core`
müssen dieselbe Version haben — `llama-index` pinnt `llama-index-core` exakt.

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

### Ausführungsreihenfolge (`run_computation_task`)

**Phase 1** — Alle gemma4:12b Läufe sequenziell (Modell bleibt im VRAM):
- Pass1 für Abschnitte 1-3, 4, 5, 6
- Pass2 für Abschnitte 1-3 und 5 (gemma4:12b, geteilt)

**Phase 2** — Pass2-Läufe für Abschnitte 4 und 6, je Kombi anderes Modell.

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
