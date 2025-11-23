# app/app.py
import os
import re
import io
import base64
import logging
import requests
from flask import Flask, render_template, request, jsonify, send_file
from app.rag.query_engine import answer_question
from app.docx_generator import create_comparison_docx, create_flowing_text_docx

app = Flask(__name__)

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

# Feste Modellkombinationen
# Reihenfolge optimiert: Kombination 3 und 4 teilen sich Pass1 (qwen3:14b)
MODEL_COMBINATIONS = [
    {"pass1": "qwen2.5:14b", "pass2": "gpt-oss:20b"},       # Kombi 1
    {"pass1": "qwen2.5:14b", "pass2": "deepseek-r1:14b"},   # Kombi 2 (nutzt Pass1 von Kombi 1)
    {"pass1": "qwen3:14b", "pass2": "gpt-oss:20b"},         # Kombi 3
    {"pass1": "qwen3:14b", "pass2": "deepseek-r1:14b"},     # Kombi 4 (nutzt Pass1 von Kombi 3)
]

# Abschnitts-Ueberschriften (7 Abschnitte)
SECTION_HEADERS = [
    "Relevante soziodemographische Daten",
    "Symptomatik und psychischer Befund",
    "Somatischer Befund / Konsiliarbericht",
    "Behandlungsrelevante Angaben zur Lebensgeschichte, Krankheitsanamnese, funktionales Bedingungsmodell (VT)",
    "Diagnose zum Zeitpunkt der Antragsstellung",
    "Behandlungsplan und Prognose",
    "Zusätzlich erforderliche Angaben bei einem Umwandlungsantrag",
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

PROMPT1 = load_prompt("prompt1.txt")  # Pass 1: Fakten-Extraktion
PROMPT2 = load_prompt("prompt2.txt")  # Pass 2: Berichts-Formulierung

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
    """Extrahiert die 7 Abschnitte aus dem Antworttext."""
    sections = [""] * 7  # 7 Abschnitte

    # Muster fuer Abschnitts-Ueberschriften (flexibel)
    patterns = [
        r"(?:1\.|I\.?|1\)|\*\*1\.?\*\*|##?\s*1\.?)?\s*(?:Relevante\s+)?soziodemographische\s+Daten",
        r"(?:2\.|II\.?|2\)|\*\*2\.?\*\*|##?\s*2\.?)?\s*Symptomatik\s+und\s+psychischer\s+Befund",
        r"(?:3\.|III\.?|3\)|\*\*3\.?\*\*|##?\s*3\.?)?\s*Somatischer\s+Befund",
        r"(?:4\.|IV\.?|4\)|\*\*4\.?\*\*|##?\s*4\.?)?\s*Behandlungsrelevante\s+Angaben",
        r"(?:5\.|V\.?|5\)|\*\*5\.?\*\*|##?\s*5\.?)?\s*Diagnose\s+zum\s+Zeitpunkt",
        r"(?:6\.|VI\.?|6\)|\*\*6\.?\*\*|##?\s*6\.?)?\s*Behandlungsplan\s+und\s+Prognose",
        r"(?:7\.|VII\.?|7\)|\*\*7\.?\*\*|##?\s*7\.?)?\s*(?:Zusaetzlich|Zusätzlich)\s+erforderliche\s+Angaben",
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


def run_pass1(uploaded_files, paste_text, question, prompt1, model_name, combo_index=None):
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

    # Debug-Logging
    debug_logger.debug(f"\n{'='*80}")
    debug_logger.debug(f"Kombination {combo_index} - Modell: {model_name}")
    debug_logger.debug(f"{'='*80}")
    debug_logger.debug(f"Ergebnis:\n{result}")
    debug_logger.debug(f"{'='*80}\n")

    return result


def run_pass2(pass1_answer, prompt2, model_name, combo_index=None):
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

    for attempt, (temp, ctx, desc) in enumerate(retry_configs, 1):
        debug_logger.debug(f"Pass 2 Versuch {attempt}: {desc}")

        result = answer_question(
            question=pass2_question,
            system_prompt=prompt2,
            uploaded_files=None,
            model_name=model_name,
            disable_rag=True,
            temperature=temp,
            num_ctx_override=ctx,
        )

        # Debug-Logging
        debug_logger.debug(f"\n{'='*80}")
        debug_logger.debug(f"Kombination {combo_index} - PASS 2 - Modell: {model_name} - Versuch {attempt}")
        debug_logger.debug(f"Parameter: {desc}")
        debug_logger.debug(f"{'='*80}")
        debug_logger.debug(f"Ergebnis:\n{result}")
        debug_logger.debug(f"{'='*80}\n")

        # Prüfe ob Ergebnis valide ist (nicht leer und keine Fehlermeldung)
        if result and len(result.strip()) > 50 and not result.startswith("❌") and not result.startswith("⏱️"):
            if attempt > 1:
                debug_logger.debug(f"Erfolg nach {attempt} Versuchen!")
            return result

        debug_logger.debug(f"Versuch {attempt} lieferte leeres/fehlerhaftes Ergebnis, wiederhole...")

    # Nach allen Versuchen das letzte Ergebnis zurückgeben
    debug_logger.debug(f"Alle {len(retry_configs)} Versuche fehlgeschlagen")
    return result


def is_pass1_failed(result):
    """Prueft ob Pass 1 fehlgeschlagen ist (Timeout oder Fehler)."""
    if not result:
        return True
    result_stripped = result.strip()
    return result_stripped.startswith("⏱️") or result_stripped.startswith("❌")


def run_model_combination(combo, uploaded_files, paste_text, question, prompt1, prompt2, pass1_cache=None, combo_index=None):
    """Fuehrt einen 2-Pass-Durchlauf mit einer Modellkombination aus.

    Args:
        pass1_cache: Optional dict zum Cachen/Wiederverwenden von Pass1-Ergebnissen.
                     Key = Pass1-Modellname, Value = Pass1-Ergebnis.
        combo_index: Index der Kombination (1-4) für Logging.
    """
    pass1_model = combo["pass1"]

    # Debug-Logging: Start der Kombination
    debug_logger.debug(f"\n{'#'*80}")
    debug_logger.debug(f"KOMBINATION {combo_index}: {combo['pass1']} -> {combo['pass2']}")
    debug_logger.debug(f"{'#'*80}")

    # Pass1: Aus Cache nehmen oder neu berechnen
    if pass1_cache is not None and pass1_model in pass1_cache:
        pass1_answer = pass1_cache[pass1_model]
        debug_logger.debug(f"Pass 1 aus Cache (Modell {pass1_model} bereits berechnet)")
    else:
        pass1_answer = run_pass1(uploaded_files, paste_text, question, prompt1, pass1_model, combo_index)
        if pass1_cache is not None:
            pass1_cache[pass1_model] = pass1_answer

    # Bei Pass1-Fehler (Timeout/Error) Pass2 ueberspringen
    if is_pass1_failed(pass1_answer):
        debug_logger.debug(f"Pass 1 fehlgeschlagen - ueberspringe Pass 2 fuer Kombination {combo_index}")
        return pass1_answer  # Fehlermeldung durchreichen

    # Pass2: Immer neu berechnen (unterschiedliche Modelle)
    final_answer = run_pass2(pass1_answer, prompt2, combo["pass2"], combo_index)

    return final_answer


@app.route("/ask-compare", methods=["POST"])
def ask_compare():
    """Fuehrt alle 4 Modellkombinationen aus und gibt DOCX und JSON zurueck."""
    question = "Analysiere die hochgeladenen Patientendaten."
    uploaded_files = request.files.getlist("files")
    paste_text = request.form.get("paste_text", "").strip()

    if not PROMPT1 or not PROMPT2:
        return jsonify({"error": "Prompts nicht gefunden"}), 500

    # Cache fuer Pass1-Ergebnisse: Kombinationen mit gleichem Pass1-Modell
    # (z.B. Kombi 1 und 2 nutzen beide qwen2.5:14b) sparen Zeit
    pass1_cache = {}

    results = []
    debug_logger.debug(f"\n{'*'*80}")
    debug_logger.debug(f"NEUE ANFRAGE GESTARTET")
    debug_logger.debug(f"{'*'*80}\n")

    for i, combo in enumerate(MODEL_COMBINATIONS, 1):
        result = run_model_combination(
            combo, uploaded_files, paste_text, question, PROMPT1, PROMPT2,
            pass1_cache=pass1_cache, combo_index=i
        )
        results.append(result)
        # Dateizeiger zuruecksetzen fuer naechsten Durchlauf
        for f in uploaded_files:
            if hasattr(f, 'stream'):
                f.stream.seek(0)

    # DOCX erstellen (via docx_generator Modul)
    docx_output = create_comparison_docx(
        results, MODEL_COMBINATIONS, SECTION_HEADERS, parse_sections
    )

    # Parsed results fuer JSON-Response
    parsed_results = [parse_sections(result) for result in results]

    # Modellnamen fuer Frontend
    model_names = [
        f"{combo['pass1']} + {combo['pass2']}" for combo in MODEL_COMBINATIONS
    ]

    # Response als JSON mit DOCX als Base64
    docx_bytes = docx_output.read()
    docx_base64 = base64.b64encode(docx_bytes).decode('utf-8')

    return jsonify({
        "docx_base64": docx_base64,
        "sections": SECTION_HEADERS,
        "models": model_names,
        "results": parsed_results  # [[7 sections], [7 sections], [7 sections], [7 sections]]
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
