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
| `llama-index` / `llama-index-core` | 0.14.25 | RAG-Framework, Vector-Store |
| `llama-index-llms-ollama` | 0.11.0 | LLM-Anbindung (LlamaIndex-Seite) |
| `llama-index-embeddings-ollama` | 0.10.0 | Embeddings via `nomic-embed-text` |
| `pydantic` | 2.13.5 | Transitiv über LlamaIndex, bewusst gepinnt |
| `requests` | 2.34.2 | Direkte Ollama-API-Calls (`/api/generate`, `/api/tags`) |
| `python-docx` | 1.2.0 | DOCX-Export **und** DOCX-Upload-Parsing |
| `pypdf` | 6.19.0 | PDF-Upload-Parsing (`extract_text_from_files`) |

Regeln beim Anheben von Versionen:

- `llama-index` und `llama-index-core` müssen dieselbe Version haben — `llama-index`
  pinnt `llama-index-core` exakt (`>=0.14.25,<0.15.0`).
- `pypdf` ist keine optionale Abhängigkeit: `extract_text_from_files()` in
  `query_engine.py` fängt den `ImportError` ab und fällt auf `pdfplumber` zurück;
  fehlen beide, werden hochgeladene PDFs **stillschweigend** als leer behandelt.
  `pdfplumber` ist nicht installiert, `pypdf` ist damit der einzige PDF-Pfad.
- `docx2txt` und `python-dotenv` wurden entfernt (September 2026) — beide wurden
  nirgends importiert. `docx2txt` würde nur gebraucht, wenn `data/guidelines/`
  DOCX-Dateien enthielte; der `SimpleDirectoryReader` liest dort aktuell nur JSON.

Nach jeder Änderung an `requirements.txt` ist ein Image-Neubau nötig:
`docker-compose up -d --build`.

## Modellkombinationen (2 Kombis)

| Kombi | Pass 1 (alle Abschnitte) | Pass 2 (alle Abschnitte) |
|-------|--------------------------|--------------------------|
| 1 | gemma4:26b (Abschnitt 6: gemma4:12b) | qwen3:14b |
| 2 | gemma4:12b | qwen3:14b |

Kombi 1 rechnet Abschnitt 6 per `pass1_override` mit gemma4:12b (spart ca. 2 Min.;
26b brachte dort kaum Mehrwert). Abschnitt 6 ist damit in beiden Spalten identisch und
wird nur einmal gerechnet. `combo_label()` zeigt den Override in UI und DOCX an.

Jede Kombi rechnet **alle** Abschnitte mit ihrem eigenen Pass-1- und Pass-2-Modell. Die
Ergebnisse stehen in der Vergleichstabelle nebeneinander, pro Abschnitt kann eine Spalte
gewählt werden. Beide Kombis nutzen dasselbe Pass-2-Modell, damit Unterschiede auf Pass 1
zurückgehen. Die Modelle stehen nur noch in `MODEL_COMBINATIONS` (`app.py`); die
Fortschrittsanzeige in `index.html` wird daraus erzeugt.

`gemma4:26b` ist das Hauptmodell für Pass 1. Es passt auf der 16-GB-GPU nicht vollständig
in den VRAM, gemessen ca. 1:45 Min. pro Abschnitt (7 Min. für Pass 1 gesamt). Qualitativ
deutlich besser als 12b bei Befund, Vorbehandlungen und Diagnose.

`gemma4:12b` ist die schnelle Vergleichsspalte (ca. 15–30 s pro Abschnitt).

`qwen3:14b` für Pass 2 und Namenprüfung: respektiert `think: False`, maximal 40.960
Tokens Kontext, T=0.1.

Verlauf der Modellwahl (September 2026, Vergleichsläufe mit derselben Akte):

- `deepseek-r1:14b` ignoriert `"think": False` und schrieb seine englische Denkspur ins DOCX.
- `gemma4:26b` (MoE) passt auf der 16-GB-GPU nicht vollständig in den VRAM (ca. 30 % CPU,
  grob 2 Minuten pro Abschnitt). Die Ergebnisse waren fast wortgleich mit qwen3:14b, enthielten
  aber mehr Wortfehler ("traubig", "Erschündung", "Kontraverlustes").
- `gemma4:12b` als Pass-2-Modell mit erhöhter Temperatur: Bei T=0.65 kam es zu
  Wortverstümmelungen ("Freufähigkeit"), bei T=0.3 fehlten klinisch relevante Angaben.
  Auch bei T=0.1 blieben Fehler wie "[Angberg fehlt]" oder "Selbstgeforderung" stehen.
- `gemma4:12b` als Pass-1-Modell: Extraktion und Formatierung klappten, Aufgaben mit
  Schlussfolgern nicht — trotz eindeutiger Prompts. Die SORC-Konsequenzen waren falsch
  zugeordnet, Diagnoseregeln wurden ignoriert (F33.0 bei BDI 7, F50.x), und der
  Datumsvergleich für Vorbehandlungen landete als Anweisungstext im Bericht. Deshalb ist
  gemma4:26b jetzt Hauptmodell, 12b bleibt als schnelle Vergleichsspalte.
- `qwen3:14b` als Pass-1-Modell: ebenso langsam wie gemma4:26b (7:40 Min., vermutlich
  weil 40K Kontext plus Modell nicht ganz in die GPU passen), sehr ausführlich (Abschnitt
  1-3 lief ins Ausgabelimit von 4096 Tokens, Abschnitt 3 fehlte komplett) und inhaltlich
  schwächer (erfundener Auslöser, F33.1 trotz BDI 7). Wieder entfernt.
- Die SORC-Konsequenzen ordnete keines der Modelle zuverlässig zu, auch gemma4:26b nicht
  (1 von 4 richtig). Deshalb die zerlegte Aufgabe (siehe "Absicherungen im Code").
- `gemma4:e4b` ist kein quantisiertes 12b, sondern ein kleineres Modell (8,0 Mrd.
  Parameter, gleiche Quantisierung Q4_K_M) und für diese Aufgabe ungeeignet.

`query_engine.strip_think()` entfernt `<think>`-Blöcke aus jeder Pass-1-/Pass-2-Antwort als
Sicherheitsnetz, unabhängig vom Modell, und loggt das in `debug_results.log`.

### Wo die Modellnamen stehen

- **`MODEL_COMBINATIONS`** (`app.py`): Pass-1- und Pass-2-Modell je Kombi, optional
  `pass2_temperature` und `pass1_override` (Pass-1-Modell je Abschnitt, z.B.
  `{"6": "gemma4:12b"}`). Das ist die einzige Stelle, UI und DOCX lesen daraus.
- **`NAME_CHECK_MODEL`** (`app.py`): Modell der Namenprüfung.

`query_engine.cap_num_ctx()` fragt die maximale Kontextlänge eines Modells bei Ollama ab
(`/api/show`) und kappt `num_ctx` darauf. Die Werte in `query_engine.py` werden nach
Namensmuster gewählt (":14b" → 48K), qwen3:14b kann aber nur 40K.

Das heutige Datum wird mitgegeben: an Pass 1 für Abschnitt 1-3 (Alter), an Pass 2 für
Abschnitt 1-3 (Einordnung der Maßnahmen in 3.3) und an Pass 1 für Abschnitt 6. Das Modell
kennt das Datum sonst nicht, rechnet das Alter falsch und führt vergangene Termine als "geplant".

Pass 1 läuft mit Temperatur 0.1 (`query_engine.py`). Bei 0.3 lieferten zwei Läufe mit
derselben Akte unterschiedliche BDI-Werte, Daten und Methodennamen.

### Absicherungen im Code (nicht nur im Prompt)

Manche Regeln befolgt das Modell trotz Prompt nicht zuverlässig. Sie werden deshalb im
Code geprüft:

- `add_diagnosis_hints()` (`app.py`) hängt an Abschnitt 5 `[Prüfhinweis: …]`-Zeilen an:
  bei unvollständigen Codes (`F50.x`), bei F43.2 neben F32/F33, wenn derselbe Code
  zugleich als Diagnose und als Differenzialdiagnose steht, und wenn der Schweregrad
  (F32.0–F33.4) nicht zum jüngsten BDI-II-Wert passt. Bereiche nach Beck/Hautzinger:
  0–13 minimal (passt nur zu remittiert), 14–19 leicht, 20–28 mittel, ab 29 schwer. Die
  Diagnosen selbst werden nicht verändert. `prompt5-2.txt` enthält deshalb keine eigene
  formale Prüfung mehr — Pass 2 hatte den Beispiel-Prüfhinweis aus dem Prompt wörtlich
  in den Bericht übernommen. `fix_icd_titles()` vergleicht den Klartext hinter jedem Code
  (auch in den Differenzialdiagnosen) mit `ICD10_TITLES` (häufige F-Codes, ICD-10-GM).
  Stehen mindestens 60 % der Wörter der offiziellen Bezeichnung da, ist sie nur
  umformuliert und wird ersetzt ("aktuell in einer remittierten Episode" → "gegenwärtig
  remittiert"). Sonst gibt es einen Hinweis: bei Diagnosen "Code und Bezeichnung prüfen"
  ("F50.0 Restriktive Essstörung"), bei Differenzialdiagnosen "Code prüfen" ("F63.2
  Zwangsstörung", F63.2 ist Kleptomanie). Codes, die nicht in der Tabelle stehen, werden
  nicht geprüft. "[DD nicht in Daten genannt]" wird entfernt, wenn darüber DDs stehen.
- **Abschnitt 1-3:**
  - `missing_subsections_13()` prüft nach Pass 2, ob 1, 2.1–2.5, 3 und 3.1–3.3 vorhanden
    sind. Fehlt etwas, wird Pass 2 bis zu zweimal wiederholt (T +0.1 je Versuch) und die
    vollständigste Fassung behalten. Bleibt es unvollständig, steht ein Prüfhinweis im
    betroffenen Abschnitt statt einer leeren Zelle. In Lauf 8 brach qwen3:14b einmal nach
    2.3 ab, das fiel vorher nicht auf.
  - `check_befund_23()` prüft 2.3 auf genau die 11 Befundbegriffe in fester Reihenfolge und
    darauf, dass kein Begriff unter einem anderen mitbefundet wird ("Wahrnehmung: keine
    Ich-Störungen" neben "Ich-Störungen: [Angabe fehlt]").
  - `normalize_markers()` (auf alle Pass-2-Ausgaben) macht aus "Zeitraum nicht angegeben",
    "Jahr nicht angegeben" und "Stand nicht dokumentiert" die Marker "[Zeitraum prüfen]",
    "[Jahr prüfen]" und "[Durchführung prüfen]" und entfernt Vorlagenreste wie
    "DD-Überlegungen aus Pass 1:". Das Klammerverbot im Prompt befolgt qwen nicht.
  - `extract_calendar_weeks()` rechnet "Reha in KW 4" in ein Datum um. Ohne Jahr gilt die
    erste KW 4 ab dem Eintragsdatum. Die Liste geht an Pass 1 für 1-3 und 6.
  - `fix_past_planned()` ersetzt in 3.3 "geplant" durch "[Durchführung prüfen]", wenn der
    zugehörige Zeitpunkt vor heute liegt. Jedes "geplant" wird dem nächsten Datum im selben
    Teilsatz zugeordnet. Pass 2 hatte "ab 19.01.2026, geplant" im September 2026 stehen lassen.
  - `replace_bdi_in_25()` ersetzt alle BDI-Angaben in 2.5 durch die Liste aus der Akte
    (`_bdi_line()`, dieselbe Form wie in der Liste für Pass 1). Die Modelle hatten "Punkten"
    zu "Punkte" gemacht und "[Einordnung prüfen]" weggelassen. Andere Testverfahren bleiben.
  - `check_family_status()`: Ein Familienstand im Bericht muss als eigenes Wort in der Akte
    stehen ("verheirateten Mann" belegt nicht "verheiratet"), sonst Prüfhinweis.
  - `check_somatic_31()`: Prüfhinweis bei psychischen Begriffen (Essstörung, Trauma …) in 3.1.
- **Familienstand und Lebensereignisse** werden nicht mehr erschlossen (`prompt1-1.txt`,
  `prompt4-1.txt`). gemma4:26b schrieb "verwitwet", weil in der Akte vom verstorbenen
  Ehemann einer anderen Person die Rede war. Fehlt die Angabe, steht "[Angabe fehlt]".
- **SORC-Konsequenzen** werden zerlegt: Pass 1 schreibt statt C-Zeilen K-Zeilen
  (`K: Verhalten | kurzfristige Folge | tritt ein/fällt weg | angenehm/unangenehm |
  langfristige Folge`, siehe `prompt4-1.txt`). `build_consequence_lines()` ordnet daraus
  C+/C-/C+//C-/ zu und ersetzt die K-Zeilen vor Pass 2 durch den fertigen C-Block.
  `apply_consequence_lines()` setzt denselben Block nach Pass 2 noch einmal ein, falls
  Pass 2 umformuliert hat. Die Formulierung ist bewusst stichwortartig ("Rückzug:
  kurzfristig …, langfristig …"), weil die Angaben im Nominativ kommen. Ein vom Modell
  mitgeliefertes "kurzfristig(e)"/"langfristig(e)" wird entfernt. Dubletten werden still
  verworfen, auch mit Tippfehler ("Vermeidung"/"Vermehmung", unscharfer Vergleich ab 8
  Zeichen). Prüfhinweise gibt es:
  - bei fehlenden oder unvollständigen K-Zeilen;
  - bei nach Entlastung klingenden Folgen ("Ruhe", "Entlastung"): unter C+, C- und C+/
    gehört das eher zu C-/, unter C-/ ist es die Entlastung selbst statt des wegfallenden
    Unangenehmen;
  - bei angenehmen Zuständen ("Stabilität", "Anerkennung") unter C- oder C-/: diese werden
    automatisch nach C+ verschoben (Läufe 8–10: "Funktionieren → Wegfall von Unangenehmem
    (Stabilität)"), mit Sammelhinweis;
  - wenn dasselbe Verhalten mit derselben Folge unter zwei verschiedenen C-Typen steht;
  - wenn S-intern die Überlebensregel aus O wiederholt (`sorc_structure_hints()`).

  "Vermeidung/Reduktion/Ausbleiben von X" als eintretende angenehme Folge wird automatisch
  zu "X fällt weg, unangenehm" (C-/) umgedeutet, mit einem Sammelhinweis. Adjektive am
  Anfang werden kleingeschrieben ("kurzfristig erhöhte Belastung"). Mehr als 8 K-Zeilen
  werden abgeschnitten (`_MAX_CONSEQUENCES`), das deutet auf eine Wiederholungsschleife hin.

  Die Anzahl der Hinweise steht in `debug_results.log` ("SORC-Konsequenzen"), der Block
  selbst nur mit `LOG_PATIENT_CONTENT=1`.
- `extract_bdi_values()` liest die BDI-Werte per Regex aus der Akte. Erkannt werden u.a.
  "BDI 2 vom 13.12. mit 47 Punkten", "BDI 2 10.1.25 33 Punkte", "BDI 2 von Januar mit
  40 Punkten" und "BDI 2 vom 5. März 2025 mit 27 Punkten". Fehlt das Jahr, wird es aus dem
  Datum am Zeilenanfang abgeleitet (13.12. im Eintrag vom Januar ergibt das Vorjahr). Die
  sortierte Liste geht als "verbindlich" an Pass 1 für Abschnitt 1-3 und 5. Vorher trug das
  Modell die verstreuten Werte in jedem Lauf anders zusammen. Werte ohne Tag stehen als
  "Januar 2025" in der Liste (aus "01/2025" machte Pass 2 "01.01.2025"). Widerspricht die
  Einordnung in der Akte dem Manualbereich (13 Punkte als "leicht"), kommt
  "[Einordnung prüfen: …]" dazu. Das Ergebnis steht in `debug_results.log`
  ("BDI-Werte aus der Akte").
- `find_person_names()` lässt `NAME_CHECK_MODEL` alle identifizierenden Eigennamen im
  fertigen Bericht auflisten: Personen, Firmen/Arbeitgeber, Einrichtungen, Orte. Der Code
  verwirft Platzhalter ("Frau X.", "F.") und behält nur Namen, die wirklich als Wort im
  Text stehen. Die Regel allein im Prompt hat nicht gereicht (Vornamen von Sohn, Freundin,
  früherem Partner und ein Arbeitgeber standen im Bericht).
  `anonymize_names()` ersetzt die gefundenen Namen: Nur die Satzteile mit einem Namen gehen
  mit `ANONYMIZE_PROMPT` (T=0, kein Thinking) an `NAME_CHECK_MODEL`, Feldbezeichnungen wie
  "S: Extern:" werden vorher abgetrennt. Satzteile werden an Semikolon, Zeilenende und
  Satzende getrennt, nicht an Abkürzungen (`_FRAGMENT_SEP_RE`; "z. B." zerlegte in Lauf 11
  eine Klammer). Der Code übernimmt die Antwort nur, wenn der Name weg ist, die Klammern
  ausgeglichen bleiben und der Rest fast gleich blieb (Ähnlichkeit ≥ 0,7). Sonst wird
  der Name auf die Initiale gekürzt. Ein Prüfhinweis nennt die Anzahl, nicht die Namen.
  Bewusst kein kompletter dritter Durchgang über den Bericht: qwen baut beim Umschreiben
  Wortfehler ein oder kürzt, das fiele im ganzen Text nicht auf. `add_name_hints()` wird
  nicht mehr aufgerufen.
  Die Patientin/der Patient selbst wird vorher deterministisch anonymisiert:
  `extract_patient_identity()` liest Name und Geburtsdatum aus dem Aktenkopf
  ("Nachname, Vorname geb. TT.MM.JJJJ"), `anonymize_patient()` ersetzt Name, Vor- und
  Nachname einzeln, Initialen und Geburtsdatum. Lauf 10 hatte "Frau X. D.".
  `strip_initial_parens()` entfernt Initialen in Klammern hinter einer Rolle ("einem
  Partner (Y.)"). Einzelwörter,
  die im Text nur mit Artikel vorkommen ("im Dorfladen"), gelten als Gattungsbegriff und
  werden verworfen. Das Modell meldete "Dorfladen" trotz Ausnahme im Prompt. Rolle +
  Initial ("Partner Y.", "Herr Z.") gilt als Platzhalter, Vorname + Initial ("Anna B.") nicht.
- **Ausgabelimit in Pass 1:** `query_engine.last_done_reason()` liefert den `done_reason`
  des letzten Aufrufs. Ist er "length", rechnet `run_pass1()` einmal mit
  `repeat_penalty=1.15` neu. gemma4:12b schrieb in Abschnitt 4 sonst K-Zeilen bis 4096 Tokens.

### Frontend: Vergleichstabelle

`renderComparisonTable()` (`main.js`) zeigt jede Kombi in jeder Zeile als eigene Spalte,
auch bei identischem Text (Abschnitt 6 per `pass1_override`) — wie die DOCX-Tabelle. Bis
September 2026 fasste eine feste Liste `[0, 1, 2, 4]` aus der Zeit vor dem 2-Pass-System
Zeilen zu einer Zelle zusammen; Abschnitte 1–3 und 5 von Kombi 2 waren im Browser nie zu sehen.

`main.js` und `style.css` werden mit `?v=<Änderungszeit>` eingebunden
(`inject_static_version()` in `app.py`), damit der Browser nach einem Neubau nicht die alte
Datei aus dem Cache nimmt.

`sanitize_sensitive_text()` (`docx_generator.py`) ersetzt Initialenpaare ("K. D.") vor
"Frau K." — die Regel hatte durch ein `\b` nach dem Punkt nie gegriffen, im Bericht stand
"Frau X. D.".

### Datenschutz

- `extract_text_from_files()` löscht das Temp-Verzeichnis nach dem Einlesen. Bis
  September 2026 blieb jede hochgeladene Akte in `/tmp/uploaded_docs_*` im Container liegen.
- `debug_results.log` enthält nur Metadaten (Längen, Modell, `done_reason`, Anzahl Hinweise)
  und die BDI-Liste. Aktenvorschau, Pass-1-Vorschau, SORC-Block und gefundene Namen werden
  nur mit `LOG_PATIENT_CONTENT=1` (`docker-compose.yml`) geschrieben — nur zur Fehlersuche.
- `.dockerignore` hält `*.log` aus dem Image. `COPY data ./data` hatte das Log mit Echtdaten
  in jedes Image kopiert.

Die Helfer `run_section4()`, `run_section5()` und `run_section6()` sind Altlasten aus
der Zeit vor `run_computation_task()` und werden nirgends mehr aufgerufen.

### Ausführungsreihenfolge (`run_computation_task`)

Die Läufe sind nach Modell gruppiert, damit möglichst selten ein Modell nachgeladen wird:

**Phase 1** — alle Pass-1-Läufe (Abschnitte 1-3, 4, 5, 6). Pass-1-Modelle, die auch als
Pass-2-Modell vorkommen, laufen zuletzt und bleiben dann geladen. Aktuell: gemma4:26b
(Abschnitte 1-3, 4, 5), dann gemma4:12b (alle vier), dann Wechsel zu qwen3:14b für alles
Weitere.

**Phase 2** — alle Pass-2-Läufe, das noch geladene Modell zuerst.

**Phase 3** — Namenprüfung je Kombi.

Läufe mit identischem Modell, Abschnitt und Eingabe werden nur einmal gerechnet und allen
betroffenen Kombis zugeordnet (Schlüssel `(pass1, pass2, temperatur, abschnitt)`, wobei
`pass1` das Modell nach `pass1_model_for()` ist, also inkl. Override).

Ausführungsreihenfolge und Ergebnisreihenfolge sind bewusst entkoppelt: Ergebnisse werden
per Kombi-Index abgelegt, DOCX-Spalten und Vergleichstabelle folgen der Reihenfolge in
`MODEL_COMBINATIONS`. In der Fortschrittsanzeige kann deshalb Kombi 2 vor Kombi 1
aufleuchten. Die Zeitentabelle sortiert in `renderTimingTable()` selbst nach Kombi.

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
- `data/guidelines/` — Leitlinien und Checklisten für RAG (5 JSON-Dateien)
- `data/examples/` — Beispielberichte, **nicht** im RAG-Index

**Keine Beispielberichte in `data/guidelines/` legen.** Alles dort wird als "Guideline" in
den Pass-1-Prompt eingefügt, und das Modell übernimmt daraus Fakten. Bis September 2026 lag
dort `EM.txt` (vollständiger Bericht eines anderen Falls). Pass 1 schrieb daraufhin Alter,
Beruf, BDI-Werte und Medikation dieses Falls in fremde Berichte. Die Datei liegt jetzt in
`data/examples/`. Aus demselben Fall stammen die Musterbeispiele in `prompt4-2.txt` und
`prompt6-2.txt`; sie sind deshalb ausdrücklich als "anderer Patient, nur Form übernehmen" markiert.

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
