# app/app.py
import os
import re
import io
import base64
import logging
import requests
import queue
import threading
import time
from flask import Flask, render_template, request, jsonify, send_file, Response
from app.rag.query_engine import answer_question
from app.docx_generator import (
    create_comparison_docx,
    create_flowing_text_docx,
    sanitize_sensitive_text,
    format_text_as_html,
    post_process_text,
    validate_schema,
    ValidationResult,
)

app = Flask(__name__)

# Globaler Event-Queue für Fortschrittsupdates
progress_queues = {}

# Globaler Speicher für abgeschlossene Berechnungen (Session-ID -> Ergebnis)
# Ermöglicht Ergebnis-Abruf nach Standby/Reconnect
completed_results = {}
# Status für laufende Berechnungen
running_tasks = {}  # session_id -> {"status": "running"|"completed"|"error", "error": str|None}

# Cleanup-Interval für alte Ergebnisse (1 Stunde)
RESULT_EXPIRY_SECONDS = 3600

# Werkzeug Request-Logging reduzieren (verhindert doppelte Logs)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# Debug-Logging für Zwischenergebnisse
debug_logger = logging.getLogger("psych_debug")
debug_logger.setLevel(logging.DEBUG)

# Log-Datei im gemounteten data-Verzeichnis (./data auf Host = /app/data/data im Container)
log_dir = "/app/data/data"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "debug_results.log")

file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
debug_logger.addHandler(file_handler)

# Feste Modellkombinationen (3 Kombinationen)
# WICHTIG: Fuer Abschnitte 1-3 und 5 wird nur "pass1" verwendet (1-Pass-System)
# "pass2" wird nur fuer Abschnitte 4 und 6 verwendet
# "pass2_temperature": optionale Basis-Temperatur fuer Pass 2 (Default: 0.1)
MODEL_COMBINATIONS = [
    {"pass1": "gemma4:e4b", "pass2": "gpt-oss:20b"},                              # Kombi 1
    {"pass1": "gemma4:e4b", "pass2": "deepseek-r1:14b"},                          # Kombi 2
    {"pass1": "gemma4:e4b", "pass2": "gemma4:e4b", "pass2_temperature": 0.65},   # Kombi 3: hoehere Temperatur
]

# Spezielle Modelle fuer komplexe Abschnitte 4, 5 und 6
MODEL_COMBINATIONS_SECTION4_5_6 = [
    {"pass1": "gemma4:e4b", "pass2": "gpt-oss:20b"},                              # Kombi 1
    {"pass1": "gemma4:e4b", "pass2": "deepseek-r1:14b"},                          # Kombi 2
    {"pass1": "gemma4:e4b", "pass2": "gemma4:e4b", "pass2_temperature": 0.65},   # Kombi 3
]

# Abschnitts-Ueberschriften (6 Abschnitte)
# WICHTIG: Muessen mit report_schema_vt_umwandlung.json uebereinstimmen
SECTION_HEADERS = [
    "Relevante soziodemographische Daten",                                      # 1
    "Symptomatik und psychischer Befund",                                       # 2
    "Somatischer Befund",                                                       # 3
    "Lebensgeschichte und psychodynamische bzw. verhaltenstherapeutische Zusammenhänge",  # 4
    "Diagnose nach ICD-10",                                                     # 5
    "Behandlungsplan und Prognose",                                             # 6
]

# Prompts aus Dateien laden
def load_prompt(filename):
    """Lädt einen Prompt aus der angegebenen Datei"""
    prompt_path = os.path.join(os.path.dirname(__file__), "..", filename)
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

# Prompts für Abschnitte 1-3 (2-Pass: Extraktion -> Formatierung)
PROMPT1 = load_prompt("prompt1.txt")           # Nur für DEFAULT_SYSTEM_PROMPT (UI-Anzeige)
PROMPT1_PASS1 = load_prompt("prompt1-1.txt")  # Pass 1: Fakten-Extraktion
PROMPT1_PASS2 = load_prompt("prompt1-2.txt")  # Pass 2: Formatierung

# Prompts für Abschnitt 4 (2-Pass)
PROMPT4_PASS1 = load_prompt("prompt4-1.txt")  # Pass 1: Fakten-Extraktion
PROMPT4_PASS2 = load_prompt("prompt4-2.txt")  # Pass 2: Polishing

# Prompts für Abschnitt 5 (2-Pass: Extraktion -> Formatierung)
PROMPT5_PASS1 = load_prompt("prompt5-1.txt")  # Pass 1: Fakten-Extraktion
PROMPT5_PASS2 = load_prompt("prompt5-2.txt")  # Pass 2: Formatierung

# Prompts für Abschnitt 6 (2-Pass)
PROMPT6_PASS1 = load_prompt("prompt6-1.txt")  # Pass 1: Fakten-Extraktion
PROMPT6_PASS2 = load_prompt("prompt6-2.txt")  # Pass 2: Polishing

# Fallback für altes System
DEFAULT_SYSTEM_PROMPT = PROMPT1 if PROMPT1 else load_prompt("prompt.txt")

@app.route("/")
def index():
    # default_prompt geht in das Template
    return render_template("index.html", default_prompt=DEFAULT_SYSTEM_PROMPT)


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
    """Fuehrt Pass 1 (Fakten-Extraktion) aus."""
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

    result = answer_question(
        question=question,
        system_prompt=prompt1,
        uploaded_files=files_to_use,
        model_name=model_name,
    )

    debug_logger.info(f"Pass 1 Ergebnis - Modell: {model_name} - Laenge: {len(result)} Zeichen")
    if not result or not result.strip():
        debug_logger.warning(f"Pass 1 LEER! Modell {model_name} hat nichts zurueckgegeben.")
    elif result.startswith("⏱️") or result.startswith("❌"):
        debug_logger.warning(f"Pass 1 FEHLER: {result[:200]}")
    else:
        debug_logger.info(f"Pass 1 Vorschau:\n{result[:500]}\n[...gekuerzt...]")

    return result


def run_pass2(pass1_answer, prompt2, model_name, combo_index=None, session_id=None, base_temperature=None):
    """Fuehrt Pass 2 (Berichts-Formulierung) basierend auf Pass 1 aus.

    base_temperature: Basis-Temperatur fuer den ersten Versuch (Default: 0.1).
    Bei leerem Ergebnis wird mit alternativen Parametern wiederholt.
    """
    pass2_question = f"""
=== ERGEBNISSE AUS PASS 1 (Fakten-Extraktion) ===

{pass1_answer}

=== ENDE PASS 1 ===

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


def run_model_combination(combo, uploaded_files, paste_text, question, prompt1, prompt2, pass1_cache=None, combo_index=None, session_id=None, timing_log=None):
    """Fuehrt einen 1-Pass-Durchlauf mit einer Modellkombination aus.

    WICHTIG: Fuer Abschnitte 1-3 wird nur Pass 1 ausgefuehrt. Das Ergebnis ist direkt der fertige Bericht.
    prompt2 wird ignoriert, da kein zweiter Pass mehr stattfindet.

    Args:
        pass1_cache: Optional dict zum Cachen/Wiederverwenden von Pass1-Ergebnissen.
                     Key = Pass1-Modellname, Value = Pass1-Ergebnis.
        combo_index: Index der Kombination (1-2) für Logging.
        session_id: Session-ID für Fortschrittsupdates.
        timing_log: Optional list zum Sammeln von Timing-Einträgen.
    """
    pass1_model = combo["pass1"]

    # Pass1: Aus Cache nehmen oder neu berechnen (direkt fertiger Bericht)
    if pass1_cache is not None and pass1_model in pass1_cache:
        final_answer = pass1_cache[pass1_model]
        if timing_log is not None:
            timing_log.append({"combo": combo_index, "section": "1-3", "pass": 1, "model": pass1_model, "duration": 0, "cached": True})
    else:
        send_progress(session_id, {"combo": combo_index, "section": "1-3", "pass": 1, "status": "running"})
        t0 = time.time()
        final_answer = run_pass1(uploaded_files, paste_text, question, prompt1, pass1_model, combo_index, session_id)
        if timing_log is not None:
            timing_log.append({"combo": combo_index, "section": "1-3", "pass": 1, "model": pass1_model, "duration": round(time.time() - t0, 1), "cached": False})
        if pass1_cache is not None:
            pass1_cache[pass1_model] = final_answer

    # Bei Pass1-Fehler Fehlermeldung durchreichen
    if is_pass1_failed(final_answer):
        return final_answer

    # Kein Pass2 mehr - das Ergebnis aus Pass1 ist bereits der fertige Bericht
    return final_answer


def run_section4(combo, combo_section4_5_6, uploaded_files, paste_text, pass1_cache_section4=None, combo_index=None, session_id=None, timing_log=None):
    """Generiert Abschnitt 4 (Lebensgeschichte/Bedingungsmodell) mit 2-Pass-System.

    Nutzt größere/bessere Modelle aus combo_section4_5_6 für komplexere Analyse.
    """
    pass1_model = combo_section4_5_6["pass1"]

    # Pass1: Aus Cache nehmen oder neu berechnen
    if pass1_cache_section4 is not None and pass1_model in pass1_cache_section4:
        pass1_answer = pass1_cache_section4[pass1_model]
        if timing_log is not None:
            timing_log.append({"combo": combo_index, "section": "4", "pass": 1, "model": pass1_model, "duration": 0, "cached": True})
    else:
        send_progress(session_id, {"combo": combo_index, "section": "4", "pass": 1, "status": "running"})
        t0 = time.time()
        pass1_answer = run_pass1(
            uploaded_files, paste_text,
            "Analysiere die Patientendaten für Abschnitt 4 (Lebensgeschichte/Bedingungsmodell).",
            PROMPT4_PASS1, pass1_model, combo_index, session_id
        )
        if timing_log is not None:
            timing_log.append({"combo": combo_index, "section": "4", "pass": 1, "model": pass1_model, "duration": round(time.time() - t0, 1), "cached": False})
        if pass1_cache_section4 is not None:
            pass1_cache_section4[pass1_model] = pass1_answer

    # Bei Pass1-Fehler Pass2 überspringen
    if is_pass1_failed(pass1_answer):
        return pass1_answer

    # Pass2
    send_progress(session_id, {"combo": combo_index, "section": "4", "pass": 2, "status": "running"})
    t0 = time.time()
    final_answer = run_pass2(pass1_answer, PROMPT4_PASS2, combo_section4_5_6["pass2"], combo_index, session_id)
    if timing_log is not None:
        timing_log.append({"combo": combo_index, "section": "4", "pass": 2, "model": combo_section4_5_6["pass2"], "duration": round(time.time() - t0, 1), "cached": False})
    return final_answer


def run_section5(combo, combo_section4_5_6, uploaded_files, paste_text, pass1_cache_section5=None, combo_index=None, session_id=None, timing_log=None):
    """Generiert Abschnitt 5 (Diagnose nach ICD-10) mit 1-Pass-System.

    Nutzt gemma4:e4b für direkte Berichtserstellung (MoE: schnell wie 4B, Qualität wie 27B).
    """
    model = "gemma4:e4b"

    # Einziger Pass: Aus Cache nehmen oder neu berechnen (direkt fertiger Bericht)
    if pass1_cache_section5 is not None and model in pass1_cache_section5:
        final_answer = pass1_cache_section5[model]
        if timing_log is not None:
            timing_log.append({"combo": combo_index, "section": "5", "pass": 1, "model": model, "duration": 0, "cached": True})
    else:
        send_progress(session_id, {"combo": combo_index, "section": "5", "pass": 1, "status": "running"})
        t0 = time.time()
        final_answer = run_pass1(
            uploaded_files, paste_text,
            "Erstelle Abschnitt 5 (Diagnose nach ICD-10) für die Patientendaten.",
            PROMPT5_PASS1, model, combo_index, session_id
        )
        if timing_log is not None:
            timing_log.append({"combo": combo_index, "section": "5", "pass": 1, "model": model, "duration": round(time.time() - t0, 1), "cached": False})
        if pass1_cache_section5 is not None:
            pass1_cache_section5[model] = final_answer

    # Bei Fehler Fehlermeldung durchreichen
    if is_pass1_failed(final_answer):
        return final_answer

    # Kein Pass2 mehr - das Ergebnis ist bereits der fertige Bericht
    return final_answer


def run_section6(combo, combo_section4_5_6, uploaded_files, paste_text, pass1_cache_section6=None, combo_index=None, session_id=None, timing_log=None):
    """Generiert Abschnitt 6 (Behandlungsplan/Prognose) mit 2-Pass-System.

    Nutzt größere/bessere Modelle aus combo_section4_5_6 für komplexere Analyse.
    """
    pass1_model = combo_section4_5_6["pass1"]

    # Pass1: Aus Cache nehmen oder neu berechnen
    if pass1_cache_section6 is not None and pass1_model in pass1_cache_section6:
        pass1_answer = pass1_cache_section6[pass1_model]
        if timing_log is not None:
            timing_log.append({"combo": combo_index, "section": "6", "pass": 1, "model": pass1_model, "duration": 0, "cached": True})
    else:
        send_progress(session_id, {"combo": combo_index, "section": "6", "pass": 1, "status": "running"})
        t0 = time.time()
        pass1_answer = run_pass1(
            uploaded_files, paste_text,
            "Analysiere die Patientendaten für Abschnitt 6 (Behandlungsplan/Prognose).",
            PROMPT6_PASS1, pass1_model, combo_index, session_id
        )
        if timing_log is not None:
            timing_log.append({"combo": combo_index, "section": "6", "pass": 1, "model": pass1_model, "duration": round(time.time() - t0, 1), "cached": False})
        if pass1_cache_section6 is not None:
            pass1_cache_section6[pass1_model] = pass1_answer

    # Bei Pass1-Fehler Pass2 überspringen
    if is_pass1_failed(pass1_answer):
        return pass1_answer

    # Pass2
    send_progress(session_id, {"combo": combo_index, "section": "6", "pass": 2, "status": "running"})
    t0 = time.time()
    final_answer = run_pass2(pass1_answer, PROMPT6_PASS2, combo_section4_5_6["pass2"], combo_index, session_id)
    if timing_log is not None:
        timing_log.append({"combo": combo_index, "section": "6", "pass": 2, "model": combo_section4_5_6["pass2"], "duration": round(time.time() - t0, 1), "cached": False})
    return final_answer


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
      Phase 1 — Alle gemma4:e4b Pass1-Laeufe (Abschnitte 1-3, 4, 5, 6).
                 Modell bleibt im VRAM, kein Modell-Swap.
      Phase 2 — Pass2-Laeufe fuer Abschnitte 4 und 6, je Kombi anderes Modell.

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

        # ================================================================
        # PHASE 1: Alle gemma4:e4b Pass1-Laeufe (Modell bleibt im VRAM)
        # Jeder Abschnitt wird fuer ALLE Kombis gleichzeitig als "running" markiert,
        # da das Ergebnis geteilt wird.
        # ================================================================

        n_combos = len(MODEL_COMBINATIONS)

        # 1a: Abschnitte 1-3 Pass1 (Fakten-Extraktion)
        for i in range(1, n_combos + 1):
            send_progress(session_id, {"combo": i, "section": "1-3", "pass": 1, "status": "running"})
        t0 = time.time()
        pass1_result_13 = run_pass1(
            create_file_storages(), paste_text,
            "Extrahiere alle relevanten Fakten fuer die Abschnitte 1-3.",
            PROMPT1_PASS1, "gemma4:e4b", 1, session_id
        )
        timing_log.append({"combo": "shared", "section": "1-3", "pass": 1, "model": "gemma4:e4b",
                            "duration": round(time.time() - t0, 1), "cached": False})

        # 1b: Abschnitt 4 Pass1
        for i in range(1, n_combos + 1):
            send_progress(session_id, {"combo": i, "section": "4", "pass": 1, "status": "running"})
        t0 = time.time()
        pass1_section4 = run_pass1(
            create_file_storages(), paste_text,
            "Analysiere die Patientendaten fuer Abschnitt 4 (Lebensgeschichte/Bedingungsmodell).",
            PROMPT4_PASS1, "gemma4:e4b", 1, session_id
        )
        timing_log.append({"combo": "shared", "section": "4", "pass": 1, "model": "gemma4:e4b",
                            "duration": round(time.time() - t0, 1), "cached": False})
        for i in range(1, n_combos + 1):
            send_progress(session_id, {"combo": i, "section": "4", "pass": 1, "status": "section_done", "cached": i > 1})

        # 1c: Abschnitt 5 Pass1 (Fakten-Extraktion)
        for i in range(1, n_combos + 1):
            send_progress(session_id, {"combo": i, "section": "5", "pass": 1, "status": "running"})
        t0 = time.time()
        pass1_result_5 = run_pass1(
            create_file_storages(), paste_text,
            "Extrahiere alle diagnostisch relevanten Informationen fuer Abschnitt 5 (Diagnose nach ICD-10).",
            PROMPT5_PASS1, "gemma4:e4b", 1, session_id
        )
        timing_log.append({"combo": "shared", "section": "5", "pass": 1, "model": "gemma4:e4b",
                            "duration": round(time.time() - t0, 1), "cached": False})

        # 1d: Abschnitt 6 Pass1
        for i in range(1, n_combos + 1):
            send_progress(session_id, {"combo": i, "section": "6", "pass": 1, "status": "running"})
        t0 = time.time()
        pass1_section6 = run_pass1(
            create_file_storages(), paste_text,
            "Analysiere die Patientendaten fuer Abschnitt 6 (Behandlungsplan/Prognose).",
            PROMPT6_PASS1, "gemma4:e4b", 1, session_id
        )
        timing_log.append({"combo": "shared", "section": "6", "pass": 1, "model": "gemma4:e4b",
                            "duration": round(time.time() - t0, 1), "cached": False})
        for i in range(1, n_combos + 1):
            send_progress(session_id, {"combo": i, "section": "6", "pass": 1, "status": "section_done", "cached": i > 1})

        # 1e: Abschnitte 1-3 Pass2 (Formatierung, gemma4:e4b, geteilt fuer alle Kombis)
        for i in range(1, n_combos + 1):
            send_progress(session_id, {"combo": i, "section": "1-3", "pass": 2, "status": "running"})
        t0 = time.time()
        if is_pass1_failed(pass1_result_13):
            result_13 = pass1_result_13
        else:
            result_13 = run_pass2(pass1_result_13, PROMPT1_PASS2, "gemma4:e4b")
        timing_log.append({"combo": "shared", "section": "1-3", "pass": 2, "model": "gemma4:e4b",
                            "duration": round(time.time() - t0, 1), "cached": False})
        for i in range(1, n_combos + 1):
            send_progress(session_id, {"combo": i, "section": "1-3", "pass": 2, "status": "section_done", "cached": i > 1})

        # 1f: Abschnitt 5 Pass2 (Formatierung, gemma4:e4b, geteilt fuer alle Kombis)
        for i in range(1, n_combos + 1):
            send_progress(session_id, {"combo": i, "section": "5", "pass": 2, "status": "running"})
        t0 = time.time()
        if is_pass1_failed(pass1_result_5):
            result_5 = pass1_result_5
        else:
            result_5 = run_pass2(pass1_result_5, PROMPT5_PASS2, "gemma4:e4b")
        timing_log.append({"combo": "shared", "section": "5", "pass": 2, "model": "gemma4:e4b",
                            "duration": round(time.time() - t0, 1), "cached": False})
        for i in range(1, n_combos + 1):
            send_progress(session_id, {"combo": i, "section": "5", "pass": 2, "status": "section_done", "cached": i > 1})

        # ================================================================
        # PHASE 2: Pass2-Laeufe (je Kombi anderes Modell)
        # ================================================================

        all_sections_by_combo = []

        for i, combo in enumerate(MODEL_COMBINATIONS, 1):
            combo_s456 = MODEL_COMBINATIONS_SECTION4_5_6[i - 1]
            pass2_model = combo_s456["pass2"]
            base_temp = combo_s456.get("pass2_temperature")

            send_progress(session_id, {"combo": i, "section": "start", "status": "starting"})

            # Abschnitt 4: Pass2
            if is_pass1_failed(pass1_section4):
                result_4 = pass1_section4
                timing_log.append({"combo": i, "section": "4", "pass": 2, "model": pass2_model,
                                    "duration": 0, "cached": False, "skipped": True})
            else:
                send_progress(session_id, {"combo": i, "section": "4", "pass": 2, "status": "running"})
                t0 = time.time()
                result_4 = run_pass2(pass1_section4, PROMPT4_PASS2, pass2_model, i, session_id,
                                     base_temperature=base_temp)
                timing_log.append({"combo": i, "section": "4", "pass": 2, "model": pass2_model,
                                    "duration": round(time.time() - t0, 1), "cached": False})

            # Abschnitt 5: Pass2 bereits in Phase 1 abgeschlossen (gemma4:e4b, geteilt)

            # Abschnitt 6: Pass2
            if is_pass1_failed(pass1_section6):
                result_6 = pass1_section6
                timing_log.append({"combo": i, "section": "6", "pass": 2, "model": pass2_model,
                                    "duration": 0, "cached": False, "skipped": True})
            else:
                send_progress(session_id, {"combo": i, "section": "6", "pass": 2, "status": "running"})
                t0 = time.time()
                result_6 = run_pass2(pass1_section6, PROMPT6_PASS2, pass2_model, i, session_id,
                                     base_temperature=base_temp)
                timing_log.append({"combo": i, "section": "6", "pass": 2, "model": pass2_model,
                                    "duration": round(time.time() - t0, 1), "cached": False})

            send_progress(session_id, {"combo": i, "section": "done", "status": "completed"})

            sections_13 = parse_sections(result_13)
            combo_sections = [
                sections_13[0], sections_13[1], sections_13[2],
                result_4, result_5, result_6
            ]
            all_sections_by_combo.append(combo_sections)

        # Post-Processing
        parsed_results = []
        for combo_sections in all_sections_by_combo:
            processed_sections = []
            for section_text in combo_sections:
                pp_result = post_process_text(section_text, enable_repair=False, enable_validation=False)
                processed_sections.append(pp_result["text"])
            parsed_results.append(processed_sections)

        # DOCX erstellen
        post_processed_results = ["\n\n".join(sections) for sections in parsed_results]
        docx_output = create_comparison_docx(
            post_processed_results, MODEL_COMBINATIONS, SECTION_HEADERS, parse_sections,
            enable_post_processing=False
        )

        # HTML-formatierte Ergebnisse
        html_results = []
        for parsed_result in parsed_results:
            html_sections = [format_text_as_html(section) for section in parsed_result]
            html_results.append(html_sections)

        # Modellnamen fuer Spaltenheader (Kombi 3 mit Temperatur kennzeichnen)
        model_names = []
        for combo in MODEL_COMBINATIONS:
            temp = combo.get("pass2_temperature")
            if temp is not None:
                model_names.append(f"{combo['pass1']} + {combo['pass2']} (T={temp})")
            else:
                model_names.append(f"{combo['pass1']} + {combo['pass2']}")

        docx_bytes = docx_output.read()
        docx_base64 = base64.b64encode(docx_bytes).decode('utf-8')

        result_data = {
            "docx_base64": docx_base64,
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
    # fuer lokale Tests ohne Docker
    app.run(host="0.0.0.0", port=5000, debug=True)
