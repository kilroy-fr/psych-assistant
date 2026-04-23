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

## Modellkombinationen (3 Kombis)

Alle Kombis teilen denselben `pass1`-Lauf (gecacht) — nur `pass2` in Abschnitten 4 und 6 differenziert.

| Kombi | Pass 1 (alle Abschnitte) | Pass 2 (Abschnitte 4 + 6) |
|-------|--------------------------|---------------------------|
| 1 | gemma4:e4b | gpt-oss:20b |
| 2 | gemma4:e4b | deepseek-r1:14b |
| 3 | gemma4:e4b | gemma4:e4b (T=0.65) |

`gemma4:e4b` als Pass-1-Modell: MoE-Architektur (27B Gewichte, 4B aktiv) → Inferenzgeschwindigkeit
wie ein 4B-Modell, Kapazität eines 27B-Modells. Löst Timeout-Problem bei langen Eingabetexten.

Kombi 3 nutzt gemma4:e4b auch als Pass-2-Modell mit höherer Temperatur (0.65 vs. Standard 0.1)
→ kreativere/vielfältigere Formulierungen als Vergleichsvariante.

Abschnitte 1–3 und 5 sind 1-Pass → identisches Ergebnis in allen 3 Kombis.

### Ausführungsreihenfolge (`run_computation_task`)

**Phase 1** — Alle gemma4:e4b Pass1-Läufe (Abschnitte 1-3, 4, 5, 6 sequenziell).
Modell bleibt im VRAM, kein Modell-Swap zwischen den Runs.

**Phase 2** — Pass2-Läufe für Abschnitte 4 und 6, je Kombi anderes Modell.
Abschnitte 1-3 und 5 werden direkt aus Phase-1-Ergebnissen übernommen.

### Kontextfenster-Logik (`query_engine.py`)

Bei zu langem Eingabetext greift eine zweistufige Kürzung:
1. Guidelines: 10 → 3 Chunks
2. Patientendaten: Chunks von hinten entfernen bis Prompt ins Fenster passt

Modellgrößen-Erkennung via Namens-Pattern (`:14b`, `:e4b` etc.) → `num_ctx_rag` 8K–49K.

## Berichtsstruktur (6 Abschnitte)

| Abschnitt | Thema | Methode |
|-----------|-------|---------|
| 1-3 | Soziodemographie, Symptomatik, Somatik | 1-Pass (prompt1.txt) |
| 4 | Lebensgeschichte/Bedingungsmodell | 2-Pass (prompt4-1.txt → prompt4-2.txt) |
| 5 | Diagnose nach ICD-10 | 1-Pass (prompt5-1.txt) |
| 6 | Behandlungsplan/Prognose | 2-Pass (prompt6-1.txt → prompt6-2.txt) |

## Wichtige Dateien

- `app/app.py` — Flask-Backend, Modellkombinationen, Abschnitts-Orchestrierung
- `app/docx_generator.py` — Word-Dokument-Erstellung mit Schema-Validierung
- `app/rag/query_engine.py` — RAG-Abfragen gegen Ollama
- `app/rag/build_index.py` — Index-Erstellung aus Leitlinien-Dokumenten
- `app/static/main.js` — Frontend-Logik (SSE, Vergleichstabelle)
- `app/templates/index.html` — Hauptseite
- `prompt*_m.txt` — Männliche Prompt-Varianten (Genus-Anpassung)
- `data/guidelines/` — Leitlinien und Checklisten für RAG

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

# Index neu erstellen
docker exec psych-assistant rm -rf /app/storage/*
docker restart psych-assistant
```
