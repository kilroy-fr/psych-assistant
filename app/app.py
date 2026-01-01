# app/app.py
import os
import re
import io
import base64
import logging
import requests
import queue
from flask import Flask, render_template, request, jsonify, send_file, Response
from app.rag.query_engine import answer_question
from app.docx_generator import create_comparison_docx, create_flowing_text_docx, sanitize_sensitive_text, format_text_as_html

app = Flask(__name__)

# Globaler Event-Queue für Fortschrittsupdates
progress_queues = {}

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

# Feste Modellkombinationen (2 Kombinationen)
# Beide teilen sich Pass1 (qwen3:14b) für Effizienz
MODEL_COMBINATIONS = [
    {"pass1": "qwen3:14b", "pass2": "gpt-oss:20b"},         # Kombi 1
    {"pass1": "qwen3:14b", "pass2": "deepseek-r1:14b"},     # Kombi 2 (nutzt Pass1 von Kombi 1)
]

# Spezielle Modelle für komplexe Abschnitte 4 und 6
# Diese nutzen größere Modelle wegen der Komplexität (Bedingungsmodell, Behandlungsplan)
MODEL_COMBINATIONS_SECTION4_6 = [
    {"pass1": "qwen3:14b", "pass2": "gpt-oss:20b"},                # Kombi 1
    {"pass1": "qwen3:14b", "pass2": "deepseek-r1:14b"},            # Kombi 2
]

# Abschnitts-Ueberschriften (6 Abschnitte)
SECTION_HEADERS = [
    "Relevante soziodemographische Daten",                                      # 1
    "Symptomatik und psychischer Befund",                                       # 2
    "Somatischer Befund",                                                       # 3
    "Behandlungsrelevante Angaben zur Lebensgeschichte, Krankheitsanamnese, funktionales Bedingungsmodell (VT)",  # 4
    "Diagnose nach ICD-10",                                                     # 5
    "Behandlungsplan und Prognose",                                             # 6
]

# Prompts aus Dateien laden (2-Pass-System)
def load_prompt(filename):
    """Lädt einen Prompt aus der angegebenen Datei"""
    prompt_path = os.path.join(os.path.dirname(__file__), "..", filename)
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

PROMPT1 = load_prompt("prompt1.txt")  # Pass 1: Fakten-Extraktion (Abschnitte 1-3, 5)
PROMPT2 = load_prompt("prompt2.txt")  # Pass 2: Berichts-Formulierung (Abschnitte 1-3, 5)

# Prompts für Abschnitt 4 (Lebensgeschichte/Bedingungsmodell)
PROMPT4_PASS1 = load_prompt("prompt4-1.txt")  # Pass 1: Fakten-Extraktion für Abschnitt 4
PROMPT4_PASS2 = load_prompt("prompt4-2.txt")  # Pass 2: Berichts-Formulierung für Abschnitt 4

# Prompts für Abschnitt 6 (Behandlungsplan/Prognose)
PROMPT6_PASS1 = load_prompt("prompt6-1.txt")  # Pass 1: Fakten-Extraktion für Abschnitt 6
PROMPT6_PASS2 = load_prompt("prompt6-2.txt")  # Pass 2: Berichts-Formulierung für Abschnitt 6

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
    """Extrahiert die 6 Abschnitte aus dem Antworttext."""
    sections = [""] * 6  # 6 Abschnitte

    # Muster fuer Abschnitts-Ueberschriften (flexibel)
    # WICHTIG: Abschnitt 5 muss "Diagnose nach ICD" erkennen (nicht "zum Zeitpunkt der Antragsstellung")
    patterns = [
        r"(?:1\.|I\.?|1\)|\*\*1\.?\*\*|##?\s*1\.?)?\s*(?:Relevante\s+)?soziodemographische\s+Daten",
        r"(?:2\.|II\.?|2\)|\*\*2\.?\*\*|##?\s*2\.?)?\s*Symptomatik\s+und\s+psychischer\s+Befund",
        r"(?:3\.|III\.?|3\)|\*\*3\.?\*\*|##?\s*3\.?)?\s*Somatischer\s+Befund",
        r"(?:4\.|IV\.?|4\)|\*\*4\.?\*\*|##?\s*4\.?)?\s*Behandlungsrelevante\s+Angaben\s+zur\s+Lebensgeschichte",  # Abschnitt 4
        r"(?:5\.|V\.?|5\)|\*\*5\.?\*\*|##?\s*5\.?)?\s*Diagnose\s+nach\s+ICD(?:-10)?",  # Abschnitt 5 (mit optionalem "-10")
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

    return result


def run_pass2(pass1_answer, prompt2, model_name, combo_index=None, session_id=None):
    """Fuehrt Pass 2 (Berichts-Formulierung) basierend auf Pass 1 aus.

    Bei leerem Ergebnis wird mit alternativen Parametern wiederholt.
    """
    pass2_question = f"""
=== ERGEBNISSE AUS PASS 1 (Fakten-Extraktion) ===

{pass1_answer}

=== ENDE PASS 1 ===

AUFGABE: Erstelle nun basierend auf diesen PASS 1 Ergebnissen den fertigen Bericht.
"""

    # Retry-Konfigurationen: (temperature, num_ctx_override, Beschreibung)
    retry_configs = [
        (None, None, "Standard-Parameter"),
        (0.3, 8192, "Retry 1: höhere Temperatur, reduzierter Context"),
        (0.5, 4096, "Retry 2: noch höhere Temperatur, minimaler Context"),
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


def run_model_combination(combo, uploaded_files, paste_text, question, prompt1, prompt2, pass1_cache=None, combo_index=None, session_id=None):
    """Fuehrt einen 2-Pass-Durchlauf mit einer Modellkombination aus.

    Args:
        pass1_cache: Optional dict zum Cachen/Wiederverwenden von Pass1-Ergebnissen.
                     Key = Pass1-Modellname, Value = Pass1-Ergebnis.
        combo_index: Index der Kombination (1-4) für Logging.
        session_id: Session-ID für Fortschrittsupdates.
    """
    pass1_model = combo["pass1"]

    # Pass1: Aus Cache nehmen oder neu berechnen
    if pass1_cache is not None and pass1_model in pass1_cache:
        pass1_answer = pass1_cache[pass1_model]
    else:
        send_progress(session_id, {"combo": combo_index, "section": "1-3, 5", "pass": 1, "status": "running"})
        pass1_answer = run_pass1(uploaded_files, paste_text, question, prompt1, pass1_model, combo_index, session_id)
        if pass1_cache is not None:
            pass1_cache[pass1_model] = pass1_answer

    # Bei Pass1-Fehler (Timeout/Error) Pass2 ueberspringen
    if is_pass1_failed(pass1_answer):
        return pass1_answer  # Fehlermeldung durchreichen

    # Pass2: Immer neu berechnen (unterschiedliche Modelle)
    send_progress(session_id, {"combo": combo_index, "section": "1-3, 5", "pass": 2, "status": "running"})
    final_answer = run_pass2(pass1_answer, prompt2, combo["pass2"], combo_index, session_id)

    return final_answer


def run_section4(combo, combo_section4_6, uploaded_files, paste_text, pass1_cache_section4=None, combo_index=None, session_id=None):
    """Generiert Abschnitt 4 (Lebensgeschichte/Bedingungsmodell) mit 2-Pass-System.

    Nutzt größere/bessere Modelle aus combo_section4_6 für komplexere Analyse.
    """
    pass1_model = combo_section4_6["pass1"]

    # Pass1: Aus Cache nehmen oder neu berechnen
    if pass1_cache_section4 is not None and pass1_model in pass1_cache_section4:
        pass1_answer = pass1_cache_section4[pass1_model]
    else:
        send_progress(session_id, {"combo": combo_index, "section": "4", "pass": 1, "status": "running"})
        pass1_answer = run_pass1(
            uploaded_files, paste_text,
            "Analysiere die Patientendaten für Abschnitt 4 (Lebensgeschichte/Bedingungsmodell).",
            PROMPT4_PASS1, pass1_model, combo_index, session_id
        )
        if pass1_cache_section4 is not None:
            pass1_cache_section4[pass1_model] = pass1_answer

    # Bei Pass1-Fehler Pass2 überspringen
    if is_pass1_failed(pass1_answer):
        return pass1_answer

    # Pass2
    send_progress(session_id, {"combo": combo_index, "section": "4", "pass": 2, "status": "running"})
    final_answer = run_pass2(pass1_answer, PROMPT4_PASS2, combo_section4_6["pass2"], combo_index, session_id)
    return final_answer


def run_section6(combo, combo_section4_6, uploaded_files, paste_text, pass1_cache_section6=None, combo_index=None, session_id=None):
    """Generiert Abschnitt 6 (Behandlungsplan/Prognose) mit 2-Pass-System.

    Nutzt größere/bessere Modelle aus combo_section4_6 für komplexere Analyse.
    """
    pass1_model = combo_section4_6["pass1"]

    # Pass1: Aus Cache nehmen oder neu berechnen
    if pass1_cache_section6 is not None and pass1_model in pass1_cache_section6:
        pass1_answer = pass1_cache_section6[pass1_model]
    else:
        send_progress(session_id, {"combo": combo_index, "section": "6", "pass": 1, "status": "running"})
        pass1_answer = run_pass1(
            uploaded_files, paste_text,
            "Analysiere die Patientendaten für Abschnitt 6 (Behandlungsplan/Prognose).",
            PROMPT6_PASS1, pass1_model, combo_index, session_id
        )
        if pass1_cache_section6 is not None:
            pass1_cache_section6[pass1_model] = pass1_answer

    # Bei Pass1-Fehler Pass2 überspringen
    if is_pass1_failed(pass1_answer):
        return pass1_answer

    # Pass2
    send_progress(session_id, {"combo": combo_index, "section": "6", "pass": 2, "status": "running"})
    final_answer = run_pass2(pass1_answer, PROMPT6_PASS2, combo_section4_6["pass2"], combo_index, session_id)
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


@app.route("/ask-compare", methods=["POST"])
def ask_compare():
    """Fuehrt alle 2 Modellkombinationen aus und gibt DOCX und JSON zurueck."""
    import uuid

    question = "Analysiere die hochgeladenen Patientendaten."
    uploaded_files = request.files.getlist("files")
    paste_text = request.form.get("paste_text", "").strip()
    session_id = request.form.get("session_id", str(uuid.uuid4()))

    if not PROMPT1 or not PROMPT2 or not PROMPT4_PASS1 or not PROMPT4_PASS2 or not PROMPT6_PASS1 or not PROMPT6_PASS2:
        return jsonify({"error": "Prompts nicht gefunden"}), 500

    # Caches fuer Pass1-Ergebnisse (fuer jede Abschnittsgruppe separat)
    pass1_cache = {}  # Fuer Abschnitte 1-3, 5
    pass1_cache_section4 = {}  # Fuer Abschnitt 4
    pass1_cache_section6 = {}  # Fuer Abschnitt 6

    # Vollstaendige Ergebnisse: Pro Kombination ein String mit allen 6 Abschnitten
    full_results = []

    for i, combo in enumerate(MODEL_COMBINATIONS, 1):
        # Hole entsprechende Kombination für Abschnitte 4 und 6
        combo_section4_6 = MODEL_COMBINATIONS_SECTION4_6[i-1]

        send_progress(session_id, {"combo": i, "section": "start", "status": "starting"})

        # Abschnitte 1-3, 5 generieren (Standard-Workflow mit schnelleren Modellen)
        result_135 = run_model_combination(
            combo, uploaded_files, paste_text, question, PROMPT1, PROMPT2,
            pass1_cache=pass1_cache, combo_index=i, session_id=session_id
        )

        # Dateizeiger zurücksetzen
        for f in uploaded_files:
            if hasattr(f, 'stream'):
                f.stream.seek(0)

        # Abschnitt 4 separat generieren (mit größeren Modellen)
        result_4 = run_section4(combo, combo_section4_6, uploaded_files, paste_text, pass1_cache_section4, combo_index=i, session_id=session_id)

        # Dateizeiger zurücksetzen
        for f in uploaded_files:
            if hasattr(f, 'stream'):
                f.stream.seek(0)

        # Abschnitt 6 separat generieren (mit größeren Modellen)
        result_6 = run_section6(combo, combo_section4_6, uploaded_files, paste_text, pass1_cache_section6, combo_index=i, session_id=session_id)

        # Dateizeiger zurücksetzen für nächste Kombination
        for f in uploaded_files:
            if hasattr(f, 'stream'):
                f.stream.seek(0)

        send_progress(session_id, {"combo": i, "section": "done", "status": "completed"})

        # Parse result_135 um Abschnitte 1-3 und 5 zu extrahieren
        sections_135 = parse_sections(result_135)

        # Alle 6 Abschnitte in korrekter Reihenfolge zusammenstellen
        # sections_135[0] = Abschnitt 1
        # sections_135[1] = Abschnitt 2
        # sections_135[2] = Abschnitt 3
        # sections_135[3] = Abschnitt 5 (war im ursprünglichen parse_sections an Position 3)
        # result_4 = Abschnitt 4
        # result_6 = Abschnitt 6

        # Zusammenführen in Reihenfolge 1, 2, 3, 4, 5, 6
        full_text = "\n\n".join([
            sections_135[0],  # Abschnitt 1
            sections_135[1],  # Abschnitt 2
            sections_135[2],  # Abschnitt 3
            result_4,         # Abschnitt 4
            sections_135[3],  # Abschnitt 5
            result_6          # Abschnitt 6
        ])

        full_results.append(full_text)

    sanitized_results = [sanitize_sensitive_text(result) for result in full_results]

    # DOCX erstellen (via docx_generator Modul)
    docx_output = create_comparison_docx(
        sanitized_results, MODEL_COMBINATIONS, SECTION_HEADERS, parse_sections
    )

    # Parsed results fuer JSON-Response
    parsed_results = [parse_sections(result) for result in sanitized_results]

    # HTML-formatierte Ergebnisse für bessere Darstellung im Frontend
    html_results = []
    for parsed_result in parsed_results:
        html_sections = [format_text_as_html(section) for section in parsed_result]
        html_results.append(html_sections)

    # Modellnamen fuer Frontend
    model_names = [
        f"{combo['pass1']} + {combo['pass2']}" for combo in MODEL_COMBINATIONS
    ]

    # Response als JSON mit DOCX als Base64
    docx_bytes = docx_output.read()
    docx_base64 = base64.b64encode(docx_bytes).decode('utf-8')

    # Signalisiere Fertigstellung
    send_progress(session_id, "DONE")

    return jsonify({
        "docx_base64": docx_base64,
        "sections": SECTION_HEADERS,
        "models": model_names,
        "results": parsed_results,  # [[6 sections], [6 sections]] - Plain text
        "html_results": html_results,  # [[6 sections], [6 sections]] - HTML-formatiert
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

    # DOCX erstellen
    docx_output = create_flowing_text_docx(sections, selected_texts)

    return send_file(
        docx_output,
        mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        as_attachment=True,
        download_name='bericht.docx'
    )


if __name__ == "__main__":
    # fuer lokale Tests ohne Docker
    app.run(host="0.0.0.0", port=5000, debug=True)
