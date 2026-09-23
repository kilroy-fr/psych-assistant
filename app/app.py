# app/app.py
import os
import re
import io
import requests
import queue
import threading
import time
from datetime import date
from flask import Flask, render_template, request, jsonify, send_file, Response
from app.rag.query_engine import answer_question, extract_text_from_files, last_done_reason
from app.docx_generator import (
    create_flowing_text_docx,
    sanitize_sensitive_text,
    format_text_as_html,
    post_process_text,
    validate_schema,
    ValidationResult,
)
from app.logging_setup import debug_logger, LOG_PATIENT_CONTENT
from app.model_config import (
    MODEL_COMBINATIONS,
    pass1_model_for,
    combo_label,
    labeled_combos,
    NAME_CHECK_MODEL,
    SECTION_HEADERS,
    DEFAULT_SYSTEM_PROMPT,
    PROMPT1_PASS1,
    PROMPT1_PASS2,
    PROMPT4_PASS1,
    PROMPT4_PASS2,
    PROMPT5_PASS1,
    PROMPT5_PASS2,
    PROMPT6_PASS1,
    PROMPT6_PASS2,
)
from app.report_checks import (
    missing_subsections_13,
    strip_pass1_heading_numbers,
    section13_hints,
    add_diagnosis_hints,
    check_diagnosis_certainty,
    replace_bdi_in_25,
    fix_past_planned,
    mark_past_undocumented,
    normalize_markers,
    extract_bdi_values,
    format_bdi_block,
    format_diagnosis_block,
    extract_calendar_weeks,
    format_calendar_week_block,
    append_hints,
)
from app.sorc import build_consequence_lines, apply_consequence_lines, sorc_structure_hints
from app.anonymization import (
    extract_patient_identity,
    anonymize_patient,
    strip_initial_parens,
    find_person_names,
    source_names_in_report,
    anonymize_names,
)

app = Flask(__name__)

# Versionsparameter fuer main.js/style.css: aendert sich mit jedem Image-Neubau,
# damit der Browser nach einem Update nicht die alte Datei aus dem Cache nimmt.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_STATIC_VERSION = str(int(max(
    (os.path.getmtime(os.path.join(_STATIC_DIR, f)) for f in os.listdir(_STATIC_DIR)), default=0
)))


@app.context_processor
def inject_static_version():
    return {"static_version": _STATIC_VERSION}


# App-Version aus VERSION-Datei (Projektwurzel) lesen und in alle Templates injizieren.
# Ein lokaler pre-commit-Hook erhoeht die Patch-Version automatisch bei jedem Commit.
def _get_version():
    version_file = os.path.join(os.path.dirname(__file__), "..", "VERSION")
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "0.0.0"


@app.context_processor
def inject_version():
    return {"version": _get_version()}

# Globaler Event-Queue für Fortschrittsupdates
progress_queues = {}

# Globaler Speicher für abgeschlossene Berechnungen (Session-ID -> Ergebnis)
# Ermöglicht Ergebnis-Abruf nach Standby/Reconnect
completed_results = {}
# Status für laufende Berechnungen
running_tasks = {}  # session_id -> {"status": "running"|"completed"|"error", "error": str|None}

# Cleanup-Interval für alte Ergebnisse (1 Stunde)
RESULT_EXPIRY_SECONDS = 3600


@app.route("/")
def index():
    # default_prompt geht in das Template
    return render_template("index.html", default_prompt=DEFAULT_SYSTEM_PROMPT, combos=labeled_combos())


@app.route("/models", methods=["GET"])
def get_models():
    """Holt die Liste verfügbarer Modelle von Ollama."""
    ollama_host = os.getenv("OLLAMA_HOST", "http://ollama:11434")
    try:
        response = requests.get(f"{ollama_host}/api/tags", timeout=5)
        response.raise_for_status()
        data = response.json()
        # Ollama API gibt models als Liste zurück
        models = [model["name"] for model in data.get("models", [])]
        return jsonify({"models": models})
    except Exception as e:
        return jsonify({"error": str(e), "models": []}), 500


def parse_sections(text):
    """Extrahiert die 6 Abschnitte aus dem Antworttext.

    WICHTIG: Fuer Abschnitte 1-3 (aus prompt1.txt):
    - Das LLM nummeriert diese als "1, 2, 3" (sequenziell)
    - Abschnitte 4, 5, 6 werden separat generiert
    """
    sections = [""] * 6  # 6 Abschnitte

    # Muster fuer Abschnitts-Ueberschriften (flexibel)
    # WICHTIG: Muss mit report_schema_vt_umwandlung.json und den Prompts uebereinstimmen
    patterns = [
        r"(?:1\.|I\.?|1\)|\*\*1\.?\*\*|##?\s*1\.?)?\s*(?:Relevante\s+)?soziodemographische\s+Daten",
        r"(?:2\.|II\.?|2\)|\*\*2\.?\*\*|##?\s*2\.?)?\s*Symptomatik\s+und\s+psychischer\s+Befund",
        r"(?:3\.|III\.?|3\)|\*\*3\.?\*\*|##?\s*3\.?)?\s*Somatischer\s+Befund",
        r"(?:4\.|IV\.?|4\)|\*\*4\.?\*\*|##?\s*4\.?)?\s*Lebensgeschichte\s+und\s+(?:psychodynamische|verhaltenstherapeutische)",  # Abschnitt 4
        r"(?:5\.|V\.?|5\)|\*\*5\.?\*\*|##?\s*5\.?)?\s*Diagnose\s+nach\s+ICD(?:-10)?",  # Abschnitt 5
        r"(?:6\.|VI\.?|6\)|\*\*6\.?\*\*|##?\s*6\.?)?\s*Behandlungsplan\s+und\s+Prognose",  # Abschnitt 6
    ]

    # Finde alle Abschnittspositionen
    positions = []
    for i, pattern in enumerate(patterns):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            positions.append((match.start(), i, match.end()))

    # Sortiere nach Position im Text
    positions.sort(key=lambda x: x[0])

    # Extrahiere Abschnittsinhalte (inklusive Überschriften)
    for idx, (start_pos, section_idx, _) in enumerate(positions):
        if idx + 1 < len(positions):
            end_pos = positions[idx + 1][0]
        else:
            end_pos = len(text)

        content = text[start_pos:end_pos].strip()
        sections[section_idx] = content

    return sections


def send_progress(session_id, message):
    """Sendet eine Fortschrittsnachricht an alle aktiven SSE-Verbindungen."""
    if session_id in progress_queues:
        progress_queues[session_id].put(message)


def run_pass1(uploaded_files, paste_text, question, prompt1, model_name, combo_index=None, session_id=None):
    """Fuehrt Pass 1 (Fakten-Extraktion) aus.

    Ollama liefert unter Last (mehrere Modell-Container teilen sich die GPU)
    gelegentlich eine leere Antwort (HTTP 200, response="") statt eines Fehlers
    -- answer_question() wirft dabei keine Exception. Ohne Retry landete das
    bisher unbemerkt als leerer Abschnitt im fertigen Bericht, waehrend Pass 2
    fuer genau diesen Fall schon einen Retry-Mechanismus hat.
    """
    from werkzeug.datastructures import FileStorage

    # Wenn Text eingefuegt wurde, als virtuelle Datei behandeln
    files_to_use = uploaded_files
    if paste_text and not any(f.filename for f in uploaded_files):
        text_file = FileStorage(
            stream=io.BytesIO(paste_text.encode("utf-8")),
            filename="eingefuegter_text.txt",
            content_type="text/plain"
        )
        files_to_use = [text_file]

    max_attempts = 3
    result = ""
    for attempt in range(1, max_attempts + 1):
        # Streams stehen nach einem Versuch auf EOF (extract_text_from_files
        # liest sie einmal komplett) -- vor jedem (Retry-)Versuch zuruecksetzen.
        for f in files_to_use:
            f.stream.seek(0)

        result = answer_question(
            question=question,
            system_prompt=prompt1,
            uploaded_files=files_to_use,
            model_name=model_name,
        )

        if result and result.strip():
            break

        if attempt < max_attempts:
            debug_logger.warning(
                f"Pass 1 LEER (Versuch {attempt}/{max_attempts}), Modell {model_name} - erneuter Versuch"
            )

    # Ausgabelimit erreicht: meist eine Wiederholungsschleife (Lauf 9: gemma4:12b schrieb
    # in Abschnitt 4 immer neue K-Zeilen bis 4096 Tokens). Einmal mit Wiederholungsbremse.
    if result and result.strip() and last_done_reason() == "length":
        debug_logger.warning(f"Pass 1 im Ausgabelimit, Modell {model_name} - Wiederholung mit repeat_penalty")
        for f in files_to_use:
            f.stream.seek(0)
        retry = answer_question(
            question=question, system_prompt=prompt1, uploaded_files=files_to_use,
            model_name=model_name, repeat_penalty=1.15,
        )
        if retry and retry.strip() and not retry.startswith(("⏱️", "❌")):
            result = retry

    debug_logger.info(f"Pass 1 Ergebnis - Modell: {model_name} - Laenge: {len(result)} Zeichen")
    if not result or not result.strip():
        debug_logger.warning(
            f"Pass 1 LEER nach {max_attempts} Versuchen! Modell {model_name} hat nichts zurueckgegeben."
        )
        return f"❌ Pass 1 lieferte nach {max_attempts} Versuchen kein Ergebnis (Modell: {model_name})."
    elif result.startswith("⏱️") or result.startswith("❌"):
        debug_logger.warning(f"Pass 1 FEHLER: {result[:200]}")
    elif LOG_PATIENT_CONTENT:
        debug_logger.info(f"Pass 1 Vorschau:\n{result[:500]}\n[...gekuerzt...]")

    return result


def run_pass2(pass1_answer, prompt2, model_name, combo_index=None, session_id=None, base_temperature=None,
              context_note=None):
    """Fuehrt Pass 2 (Berichts-Formulierung) basierend auf Pass 1 aus.

    base_temperature: Basis-Temperatur fuer den ersten Versuch (Default: 0.1).
    context_note: optionaler Zusatz nach den Pass-1-Ergebnissen (z.B. heutiges Datum).
    Bei leerem Ergebnis wird mit alternativen Parametern wiederholt.
    """
    note = f"\n{context_note}\n" if context_note else ""
    pass2_question = f"""
=== ERGEBNISSE AUS PASS 1 (Fakten-Extraktion) ===

{pass1_answer}

=== ENDE PASS 1 ===
{note}
AUFGABE: Erstelle nun basierend auf diesen PASS 1 Ergebnissen den fertigen Bericht.
"""

    base_temp = base_temperature if base_temperature is not None else 0.1
    # Retry-Konfigurationen: (temperature, num_ctx_override, Beschreibung)
    retry_configs = [
        (base_temp, None, "Standard-Parameter"),
        (min(base_temp + 0.2, 0.8), 8192, "Retry 1: höhere Temperatur, reduzierter Context"),
        (min(base_temp + 0.4, 0.9), 4096, "Retry 2: noch höhere Temperatur, minimaler Context"),
    ]

    for temp, ctx, _ in retry_configs:
        result = answer_question(
            question=pass2_question,
            system_prompt=prompt2,
            uploaded_files=None,
            model_name=model_name,
            disable_rag=True,
            temperature=temp,
            num_ctx_override=ctx,
        )

        # Prüfe ob Ergebnis valide ist (nicht leer und keine Fehlermeldung)
        if result and len(result.strip()) > 50 and not result.startswith("❌") and not result.startswith("⏱️"):
            return result

    # Nach allen Versuchen das letzte Ergebnis zurückgeben
    return result


def is_pass1_failed(result):
    """Prueft ob Pass 1 fehlgeschlagen ist (Timeout oder Fehler)."""
    if not result:
        return True
    result_stripped = result.strip()
    return result_stripped.startswith("⏱️") or result_stripped.startswith("❌")


@app.route("/progress/<session_id>")
def progress_stream(session_id):
    """Server-Sent Events Endpunkt für Fortschrittsupdates."""
    import json

    def event_stream():
        # Erstelle Queue für diese Session
        q = queue.Queue()
        progress_queues[session_id] = q

        try:
            while True:
                # Warte auf Nachricht (mit Timeout um Connection zu prüfen)
                try:
                    message = q.get(timeout=30)
                    if message == "DONE":
                        yield f"data: {json.dumps({'status': 'done'})}\n\n"
                        break
                    yield f"data: {json.dumps(message)}\n\n"
                except queue.Empty:
                    # Keepalive Ping
                    yield f": keepalive\n\n"
        finally:
            # Cleanup
            if session_id in progress_queues:
                del progress_queues[session_id]

    return Response(event_stream(), mimetype='text/event-stream')


@app.route("/result/<session_id>")
def get_result(session_id):
    """Gibt das Ergebnis einer abgeschlossenen Berechnung zurück.

    Ermöglicht Ergebnis-Abruf nach Standby/Reconnect.
    """
    # Prüfe ob Ergebnis vorhanden
    if session_id in completed_results:
        result = completed_results[session_id]
        return jsonify({
            "status": "completed",
            "data": result
        })

    # Prüfe ob Berechnung noch läuft
    if session_id in running_tasks:
        task_info = running_tasks[session_id]
        if task_info["status"] == "error":
            return jsonify({
                "status": "error",
                "error": task_info.get("error", "Unbekannter Fehler")
            })
        return jsonify({
            "status": "running"
        })

    # Session nicht gefunden
    return jsonify({
        "status": "not_found"
    }), 404


def cleanup_old_results():
    """Entfernt alte Ergebnisse nach Ablauf der Expiry-Zeit."""
    current_time = time.time()
    expired_sessions = []

    for session_id, result in completed_results.items():
        if current_time - result.get("_timestamp", 0) > RESULT_EXPIRY_SECONDS:
            expired_sessions.append(session_id)

    for session_id in expired_sessions:
        del completed_results[session_id]
        if session_id in running_tasks:
            del running_tasks[session_id]


def run_computation_task(session_id, file_contents, paste_text):
    """Fuehrt die eigentliche Berechnung im Hintergrund-Thread aus.

    ABLAUF:
      Phase 1 — alle Pass-1-Laeufe, nach Modell gruppiert. Modelle, die auch in
                 Pass 2 gebraucht werden, kommen zuletzt (bleiben dann geladen).
      Phase 2 — alle Pass-2-Laeufe, nach Modell gruppiert, das geladene zuerst.
      Phase 3 — Namenpruefung je Kombi.
    Identische Laeufe (gleiches Modell, gleicher Abschnitt, gleiche Eingabe)
    werden nur einmal gerechnet und fuer alle betroffenen Kombis verwendet.

    Args:
        session_id: Session-ID fuer Fortschrittsupdates und Ergebnis-Speicherung
        file_contents: Liste von (filename, content_bytes) Tupeln
        paste_text: Eingefuegter Text
    """
    from werkzeug.datastructures import FileStorage

    try:
        def create_file_storages():
            files = []
            for filename, content_bytes in file_contents:
                file_obj = FileStorage(
                    stream=io.BytesIO(content_bytes),
                    filename=filename,
                    content_type="application/octet-stream"
                )
                files.append(file_obj)
            return files

        timing_log = []
        combos = MODEL_COMBINATIONS
        today = f"{date.today():%d.%m.%Y}"

        # BDI-Werte deterministisch aus der Akte lesen und Pass 1 als feste Liste geben
        source_texts = extract_text_from_files(create_file_storages())
        if paste_text:
            source_texts.append(paste_text)
        patient = extract_patient_identity("\n".join(source_texts))
        debug_logger.info(f"Patientenname im Aktenkopf {'gefunden' if patient else 'NICHT gefunden'}")
        bdi_values = extract_bdi_values("\n".join(source_texts))
        latest_bdi = bdi_values[-1] if bdi_values else None
        bdi_block = format_bdi_block(bdi_values)
        debug_logger.info(f"BDI-Werte aus der Akte:\n{bdi_block or '(keine gefunden)'}")
        bdi_suffix = f"\n\n{bdi_block}" if bdi_block else ""
        # Kodierte Diagnosen der Akte mit Datum und G/V fuer Pass 1 von Abschnitt 5 (Laeufe 14-18: F43.2)
        diag_block = format_diagnosis_block("\n".join(source_texts))
        diag_suffix = f"\n\n{diag_block}" if diag_block else ""

        # Kalenderwochen-Termine (z.B. "Reha in KW 4") als Datum fuer 3.3 und Abschnitt 6
        kw_block = format_calendar_week_block(extract_calendar_weeks("\n".join(source_texts)))
        debug_logger.info(f"Kalenderwochen-Termine: {kw_block.count(chr(10) + '- ')} gefunden")
        kw_suffix = f"\n\n{kw_block}" if kw_block else ""

        # (Abschnitt, Pass-1-Prompt, Pass-2-Prompt, Pass-1-Frage, Pass-2-Zusatz)
        # Das Modell kennt das aktuelle Datum nicht: ohne Stichtag rechnet es das
        # Alter falsch und fuehrt laengst vergangene Termine als "geplant".
        sections = [
            ("1-3", PROMPT1_PASS1, PROMPT1_PASS2,
             "Extrahiere alle relevanten Fakten fuer die Abschnitte 1-3. "
             f"Heutiges Datum (Stichtag fuer die Altersberechnung): {today}." + bdi_suffix + kw_suffix,
             f"HEUTIGES DATUM (fuer die Einordnung von Massnahmen in 3.3): {today}"),
            ("4", PROMPT4_PASS1, PROMPT4_PASS2,
             "Analysiere die Patientendaten fuer Abschnitt 4 (Lebensgeschichte/Bedingungsmodell).",
             None),
            ("5", PROMPT5_PASS1, PROMPT5_PASS2,
             "Extrahiere alle diagnostisch relevanten Informationen fuer Abschnitt 5 (Diagnose nach ICD-10)."
             + bdi_suffix + diag_suffix,
             None),
            ("6", PROMPT6_PASS1, PROMPT6_PASS2,
             "Analysiere die Patientendaten fuer Abschnitt 6 (Behandlungsplan/Prognose). "
             f"Heutiges Datum: {today}. Massnahmen, deren Durchfuehrung nicht dokumentiert ist, "
             "nicht als erfolgt darstellen." + kw_suffix,
             None),
        ]

        def combos_where(pred):
            return [i for i, c in enumerate(combos) if pred(c)]

        def progress(combo_idxs, section, pass_no, status):
            if status == "running":
                send_progress(session_id, {"combo": combo_idxs[0] + 1, "section": "start", "status": "starting"})
            for idx in combo_idxs:
                send_progress(session_id, {"combo": idx + 1, "section": section, "pass": pass_no, "status": status})

        def log_timing(combo_idxs, section, pass_no, model, t0):
            label = "shared" if len(combo_idxs) == len(combos) and len(combos) > 1 else combo_idxs[0] + 1
            timing_log.append({"combo": label, "section": section, "pass": pass_no, "model": model,
                               "duration": round(time.time() - t0, 1), "cached": False})

        # ================================================================
        # PHASE 1: Pass 1, nach Modell gruppiert. Modelle, die auch Pass 2
        # rechnen, zuletzt -- sie bleiben dann fuer Phase 2 geladen.
        # ================================================================
        pass2_models = {c["pass2"] for c in combos}
        pass1_models = sorted(
            dict.fromkeys(pass1_model_for(c, key) for c in combos for key, *_ in sections),
            key=lambda m: m in pass2_models,
        )

        pass1_results = {}  # (modell, abschnitt) -> Text
        consequences = {}   # pass1-modell -> (C-Block, Pruefhinweise) fuer Abschnitt 4
        for model in pass1_models:
            for key, prompt1, _, question, _ in sections:
                users = combos_where(lambda c: pass1_model_for(c, key) == model)
                if not users:
                    continue
                progress(users, key, 1, "running")
                t0 = time.time()
                text = run_pass1(
                    create_file_storages(), paste_text, question, prompt1, model, users[0] + 1, session_id
                )
                if key == "4" and not is_pass1_failed(text):
                    text, c_block, c_hints = build_consequence_lines(text)
                    consequences[model] = (c_block, c_hints)
                    if LOG_PATIENT_CONTENT:
                        debug_logger.info(f"SORC-Konsequenzen ({model}):\n{c_block}\nHinweise: {c_hints}")
                    else:
                        debug_logger.info(f"SORC-Konsequenzen ({model}): {len(c_hints)} Prüfhinweis(e)")
                pass1_results[(model, key)] = text
                log_timing(users, key, 1, model, t0)

        # ================================================================
        # PHASE 2: Pass 2, nach Modell gruppiert, das zuletzt geladene zuerst.
        # Die Ergebnisse werden per Kombi-Index abgelegt, die Reihenfolge der
        # Ausfuehrung beeinflusst die Spaltenreihenfolge also nicht.
        # ================================================================
        jobs = {}  # (pass1, pass2, temp, abschnitt) -> Kombi-Indizes
        for idx, c in enumerate(combos):
            for key, *_ in sections:
                jobs.setdefault((pass1_model_for(c, key), c["pass2"], c.get("pass2_temperature"), key), []).append(idx)

        loaded = pass1_models[-1]
        pass2_order = sorted(dict.fromkeys(c["pass2"] for c in combos), key=lambda m: m != loaded)

        section_results = [dict() for _ in combos]
        for model in pass2_order:
            for (p1, p2, temp, key), users in jobs.items():
                if p2 != model:
                    continue
                _, _, prompt2, _, note = next(s for s in sections if s[0] == key)
                pass1_text = pass1_results[(p1, key)]
                if key == "1-3":
                    # Nummerierte Pass-1-Gliederung liess qwen nach 2.3 abbrechen
                    pass1_text = strip_pass1_heading_numbers(pass1_text)
                if is_pass1_failed(pass1_text):
                    text = pass1_text
                else:
                    progress(users, key, 2, "running")
                    t0 = time.time()
                    text = run_pass2(pass1_text, prompt2, p2, users[0] + 1, session_id,
                                     base_temperature=temp, context_note=note)
                    if key == "1-3":
                        # Unvollstaendige Ausgabe (Lauf 8: Abbruch nach 2.3) bis zu zweimal
                        # neu rechnen, die vollstaendigste Fassung behalten
                        missing = missing_subsections_13(text)
                        for attempt in (1, 2):
                            if not missing:
                                break
                            debug_logger.warning(f"Pass 2 Abschnitt 1-3 ({p1} + {p2}) unvollständig, "
                                                 f"fehlt: {missing} — Wiederholung {attempt}/2")
                            retry = run_pass2(pass1_text, prompt2, p2, users[0] + 1, session_id,
                                              base_temperature=round((temp or 0.1) + 0.1 * attempt, 2),
                                              context_note=note)
                            retry_missing = missing_subsections_13(retry)
                            if len(retry_missing) < len(missing):
                                text, missing = retry, retry_missing
                        if missing:
                            debug_logger.warning(f"Pass 2 Abschnitt 1-3 ({p1} + {p2}) bleibt unvollständig: {missing}")
                    log_timing(users, key, 2, p2, t0)
                    text = normalize_markers(text)
                    if key == "1-3":
                        text = replace_bdi_in_25(text, bdi_values)
                        text = fix_past_planned(text, date.today())
                        text = mark_past_undocumented(text, date.today())
                    if key == "4":
                        if p1 in consequences:
                            text = apply_consequence_lines(text, *consequences[p1])
                        text = append_hints(text, sorc_structure_hints(text))
                        text = append_hints(text, check_diagnosis_certainty(text, "\n".join(source_texts)))
                    if key == "5":
                        text = add_diagnosis_hints(text, latest_bdi, "\n".join(source_texts))
                        text = append_hints(text, check_diagnosis_certainty(text, "\n".join(source_texts)))
                progress(users, key, 2, "section_done")
                for idx in users:
                    section_results[idx][key] = text

        # ================================================================
        # PHASE 3: Namenpruefung je Kombi
        # ================================================================
        results_by_combo = []
        for idx, res in enumerate(section_results):
            sections_13 = parse_sections(res["1-3"])
            if not is_pass1_failed(res["1-3"]):
                # Fehlende Abschnitte sichtbar machen statt einer leeren Tabellenzelle
                for i, hints in section13_hints(res["1-3"], "\n".join(source_texts), parse_sections).items():
                    sections_13[i] = append_hints(sections_13[i] or f"{i + 1}. {SECTION_HEADERS[i]}", hints)
            combo_sections = [sections_13[0], sections_13[1], sections_13[2], res["4"], res["5"], res["6"]]
            # Patientin/Patient selbst: deterministisch aus dem Aktenkopf
            combo_sections = [strip_initial_parens(anonymize_patient(s, patient)) for s in combo_sections]
            t0 = time.time()
            report_text = "\n\n".join(combo_sections)
            names = find_person_names(report_text, NAME_CHECK_MODEL)
            # Sicherheitsnetz: Namen, die die Akte selbst als Namen kennzeichnet (Lauf 15:
            # die Namenpruefung uebersah den Vornamen einer Freundin in 4.1)
            names += [n for n in source_names_in_report("\n".join(source_texts), report_text) if n not in names]
            timing_log.append({"combo": idx + 1, "section": "Namen", "pass": "Prüfung", "model": NAME_CHECK_MODEL,
                               "duration": round(time.time() - t0, 1), "cached": False})
            if names:
                debug_logger.info(f"Kombi {idx + 1}: {len(names)} identifizierende Name(n) im Bericht"
                                  + (f": {names}" if LOG_PATIENT_CONTENT else ""))
                # Dritte: gezielter Ersetzungsdurchgang nur fuer die Satzteile mit Namen
                t0 = time.time()
                combo_sections = anonymize_names(combo_sections, names, NAME_CHECK_MODEL)
                timing_log.append({"combo": idx + 1, "section": "Namen", "pass": "Ersetzung",
                                   "model": NAME_CHECK_MODEL, "duration": round(time.time() - t0, 1),
                                   "cached": False})
            results_by_combo.append(combo_sections)
            send_progress(session_id, {"combo": idx + 1, "section": "done", "status": "completed"})

        all_sections_by_combo = results_by_combo

        # Post-Processing
        parsed_results = []
        for combo_sections in all_sections_by_combo:
            processed_sections = []
            for section_text in combo_sections:
                pp_result = post_process_text(section_text, enable_repair=False, enable_validation=False)
                processed_sections.append(pp_result["text"])
            parsed_results.append(processed_sections)

        # HTML-formatierte Ergebnisse
        html_results = []
        for parsed_result in parsed_results:
            html_sections = [format_text_as_html(section) for section in parsed_result]
            html_results.append(html_sections)

        # Modellnamen fuer Spaltenheader (inkl. Abschnitts-Override und eigener Temperatur)
        model_names = [combo_label(c) for c in MODEL_COMBINATIONS]

        result_data = {
            "sections": SECTION_HEADERS,
            "models": model_names,
            "results": parsed_results,
            "html_results": html_results,
            "timing_log": timing_log,
            "session_id": session_id,
            "_timestamp": time.time()
        }
        completed_results[session_id] = result_data
        running_tasks[session_id] = {"status": "completed"}

        send_progress(session_id, "DONE")
        cleanup_old_results()

    except Exception as e:
        import traceback
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        running_tasks[session_id] = {"status": "error", "error": str(e)}
        send_progress(session_id, {"status": "error", "error": str(e)})
        print(f"[ERROR] Berechnung fuer Session {session_id} fehlgeschlagen: {error_msg}")


@app.route("/ask-compare", methods=["POST"])
def ask_compare():
    """Startet die Berechnung im Hintergrund und gibt sofort die Session-ID zurück.

    Das Ergebnis kann später über /result/<session_id> abgerufen werden.
    Dies ermöglicht Robustheit gegen Standby/Bildschirm-Aus.
    """
    import uuid

    uploaded_files = request.files.getlist("files")
    paste_text = request.form.get("paste_text", "").strip()
    session_id = request.form.get("session_id", str(uuid.uuid4()))

    if not PROMPT1_PASS1 or not PROMPT1_PASS2 or not PROMPT4_PASS1 or not PROMPT4_PASS2 or not PROMPT5_PASS1 or not PROMPT5_PASS2 or not PROMPT6_PASS1 or not PROMPT6_PASS2:
        return jsonify({"error": "Prompts nicht gefunden"}), 500

    # Dateien einlesen (müssen vor Thread-Start eingelesen werden, da Request-Kontext sonst weg ist)
    file_contents = []
    for f in uploaded_files:
        if f.filename:
            content = f.read()
            file_contents.append((f.filename, content))

    # Markiere Task als laufend
    running_tasks[session_id] = {"status": "running"}

    # Starte Berechnung in Hintergrund-Thread
    thread = threading.Thread(
        target=run_computation_task,
        args=(session_id, file_contents, paste_text),
        daemon=True
    )
    thread.start()

    # Sofortige Rückgabe der Session-ID (Frontend pollt für Ergebnis)
    return jsonify({
        "status": "started",
        "session_id": session_id
    })


@app.route("/create-text", methods=["POST"])
def create_text():
    """Erstellt ein Word-Dokument mit Fliesstext aus den ausgewaehlten Zellen."""
    data = request.get_json()

    if not data:
        return jsonify({"error": "Keine Daten empfangen"}), 400

    sections = data.get("sections", [])
    selected_texts = data.get("selected_texts", [])

    if not sections or not selected_texts:
        return jsonify({"error": "Abschnitte oder Texte fehlen"}), 400

    # DEBUG: Logge empfangene Daten
    print(f"\n[DEBUG /create-text]")
    print(f"Anzahl sections: {len(sections)}")
    print(f"Anzahl selected_texts: {len(selected_texts)}")
    for i, (sec, text) in enumerate(zip(sections, selected_texts), 1):
        preview = text[:80].replace('\n', ' ') if text else "[LEER]"
        print(f"  {i}. {sec}: {preview}...")

    # DOCX erstellen
    # Post-Processing aktivieren, um Formatierungsprobleme zu beheben
    docx_output = create_flowing_text_docx(sections, selected_texts, enable_post_processing=True)

    return send_file(
        docx_output,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name='bericht.docx'
    )


if __name__ == "__main__":
    # Auch der Einstiegspunkt im Container (Dockerfile: CMD python -m app.app).
    # Debug daher standardmaessig AUS: der Werkzeug-Debugger wuerde bei jeder
    # Exception eine interaktive Konsole ausliefern. Nur lokal per
    # FLASK_DEBUG=1 einschalten, nie im ueber Caddy erreichbaren Container.
    debug_mode = os.getenv("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "yes")

    # threaded=True ist Pflicht: die SSE-Route /progress/<session_id> haelt eine
    # Verbindung offen, waehrend die Berechnung im Hintergrund-Thread laeuft.
    app.run(host="0.0.0.0", port=5000, debug=debug_mode, threaded=True)
