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
import json
from datetime import date, timedelta
from difflib import SequenceMatcher
from flask import Flask, render_template, request, jsonify, send_file, Response
from app.rag.query_engine import answer_question, extract_text_from_files, last_done_reason
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

# Versionsparameter fuer main.js/style.css: aendert sich mit jedem Image-Neubau,
# damit der Browser nach einem Update nicht die alte Datei aus dem Cache nimmt.
_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_STATIC_VERSION = str(int(max(
    (os.path.getmtime(os.path.join(_STATIC_DIR, f)) for f in os.listdir(_STATIC_DIR)), default=0
)))


@app.context_processor
def inject_static_version():
    return {"static_version": _STATIC_VERSION}

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

# Inhalte (Pass-1-Vorschau, SORC-Block, gefundene Namen) nur auf ausdruecklichen
# Wunsch ins Log: sie enthalten Echtdaten, das Log liegt dauerhaft in ./data.
LOG_PATIENT_CONTENT = os.environ.get("LOG_PATIENT_CONTENT") == "1"

# Modellkombinationen. Jede Kombi rechnet alle Abschnitte mit ihrem Pass-1- und
# Pass-2-Modell; die Ergebnisse stehen in der Vergleichstabelle nebeneinander.
# Laeufe mit identischem Modell und Abschnitt werden nur einmal gerechnet.
# "pass2_temperature": optionale Basis-Temperatur fuer Pass 2 (Default: 0.1)
# "pass1_override": optionales Pass-1-Modell je Abschnitt, z.B. {"6": "gemma4:12b"}
MODEL_COMBINATIONS = [
    # Abschnitt 6 mit 12b: spart ca. 2 Min., gemma4:26b bringt dort kaum Mehrwert.
    # Abschnitt 6 ist damit in beiden Spalten gleich (wird nur einmal gerechnet).
    {"pass1": "gemma4:26b", "pass2": "qwen3:14b", "pass1_override": {"6": "gemma4:12b"}},
    {"pass1": "gemma4:12b", "pass2": "qwen3:14b"},  # schnelle Vergleichsspalte
]


def pass1_model_for(combo, section_key):
    """Pass-1-Modell einer Kombi fuer einen Abschnitt (beruecksichtigt pass1_override)."""
    return combo.get("pass1_override", {}).get(section_key, combo["pass1"])


def combo_label(combo):
    """Spaltenname fuer UI und DOCX, z.B. "gemma4:26b (Abschn. 6: gemma4:12b) + qwen3:14b"."""
    overrides = ", ".join(f"Abschn. {k}: {m}" for k, m in combo.get("pass1_override", {}).items())
    pass1 = f"{combo['pass1']} ({overrides})" if overrides else combo["pass1"]
    label = f"{pass1} + {combo['pass2']}"
    if combo.get("pass2_temperature") is not None:
        label += f" (T={combo['pass2_temperature']})"
    return label


def labeled_combos():
    return [dict(c, label=combo_label(c)) for c in MODEL_COMBINATIONS]

# Modell fuer die Namenpruefung (listet Namen Dritter im fertigen Bericht auf)
NAME_CHECK_MODEL = "qwen3:14b"

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


_ICD_CODE_RE = re.compile(r"\bF\d{2}(?:\.[0-9xX]{1,2})?")

_MONTH_NAMES = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
                "August", "September", "Oktober", "November", "Dezember"]


def _bdi_band(score):
    """BDI-II-Bereich nach Beck/Hautzinger: 0-13 minimal, 14-19 leicht, 20-28 mittel, ab 29 schwer."""
    if score <= 13:
        return "minimal"
    if score <= 19:
        return "leicht"
    if score <= 28:
        return "mittel"
    return "schwer"


# Offizielle Bezeichnungen (ICD-10-GM) haeufiger Codes fuer den Klartext-Abgleich.
# Fuenfstellige Codes (F40.01) werden gegen den vierstelligen Titel geprueft.
ICD10_TITLES = {
    "F32.0": "Leichte depressive Episode",
    "F32.1": "Mittelgradige depressive Episode",
    "F32.2": "Schwere depressive Episode ohne psychotische Symptome",
    "F32.3": "Schwere depressive Episode mit psychotischen Symptomen",
    "F32.9": "Depressive Episode, nicht näher bezeichnet",
    "F33.0": "Rezidivierende depressive Störung, gegenwärtig leichte Episode",
    "F33.1": "Rezidivierende depressive Störung, gegenwärtig mittelgradige Episode",
    "F33.2": "Rezidivierende depressive Störung, gegenwärtig schwere Episode ohne psychotische Symptome",
    "F33.3": "Rezidivierende depressive Störung, gegenwärtig schwere Episode mit psychotischen Symptomen",
    "F33.4": "Rezidivierende depressive Störung, gegenwärtig remittiert",
    "F34.1": "Dysthymia",
    "F40.0": "Agoraphobie",
    "F40.1": "Soziale Phobien",
    "F40.2": "Spezifische (isolierte) Phobien",
    "F41.0": "Panikstörung [episodisch paroxysmale Angst]",
    "F41.1": "Generalisierte Angststörung",
    "F41.2": "Angst und depressive Störung, gemischt",
    "F42.0": "Vorwiegend Zwangsgedanken oder Grübelzwang",
    "F42.1": "Vorwiegend Zwangshandlungen [Zwangsrituale]",
    "F42.2": "Zwangsgedanken und -handlungen, gemischt",
    "F43.0": "Akute Belastungsreaktion",
    "F43.1": "Posttraumatische Belastungsstörung",
    "F43.2": "Anpassungsstörungen",
    "F45.0": "Somatisierungsstörung",
    "F50.0": "Anorexia nervosa",
    "F50.1": "Atypische Anorexia nervosa",
    "F50.2": "Bulimia nervosa",
    "F50.3": "Atypische Bulimia nervosa",
    "F51.0": "Nichtorganische Insomnie",
    "F60.3": "Emotional instabile Persönlichkeitsstörung",
    "F90.0": "Einfache Aktivitäts- und Aufmerksamkeitsstörung",
    "F10.2": "Psychische und Verhaltensstörungen durch Alkohol: Abhängigkeitssyndrom",
    "F45.41": "Chronische Schmerzstörung mit somatischen und psychischen Faktoren",
    "F48.0": "Neurasthenie",
    "F60.6": "Ängstliche (vermeidende) Persönlichkeitsstörung",
    "F63.0": "Pathologisches Spielen",
    "F63.2": "Pathologisches Stehlen [Kleptomanie]",
    # Dreistellige Codes (in Differenzialdiagnosen oft ohne Subtyp)
    "F32": "Depressive Episode",
    "F33": "Rezidivierende depressive Störung",
    "F40": "Phobische Störungen",
    "F41": "Andere Angststörungen",
    "F42": "Zwangsstörung",
    "F43": "Reaktionen auf schwere Belastungen und Anpassungsstörungen",
    "F50": "Essstörungen",
    "F90": "Hyperkinetische Störungen",
}


def _norm_title(s):
    """Klammerzusaetze, Satzzeichen und Gross-/Kleinschreibung fuer den Vergleich entfernen."""
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s.lower())
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def _title_overlap(official, label):
    """Anteil der Woerter der offiziellen Bezeichnung, die (ueber die ersten 6 Buchstaben)
    auch im Klartext stehen. "aktuell in einer remittierten Episode" trifft "remittiert"."""
    want = {w[:6] for w in _norm_title(official).split()}
    have = {w[:6] for w in _norm_title(label).split()}
    return len(want & have) / len(want) if want else 0.0


# ICD-Code mit folgendem Klartext bis zum Satzende. Klartext muss gross beginnen,
# sonst ist es Fliesstext ("F42 wurde erwogen").
_CODE_LABEL_RE = re.compile(r"\b(F\d{2}(?:\.\d{1,2})?)[ \t]+([A-ZÄÖÜ][^\n.;]*)")


def fix_icd_titles(text):
    """Gleicht den Klartext hinter jedem ICD-Code mit ICD10_TITLES ab.

    Nur umformuliert (>= 60 % der Woerter der offiziellen Bezeichnung vorhanden):
    offizielle Bezeichnung einsetzen. Sonst Hinweis, in Differenzialdiagnosen
    mit "Code pruefen" (Lauf 9: "F63.2 Zwangsstoerung", F63.2 ist Kleptomanie).
    Rueckgabe: (Text, [(Code, Hinweis)]).
    """
    dd_match = re.search(r"Differen[zt]ialdiagnose", text, re.IGNORECASE)
    dd_start = dd_match.start() if dd_match else len(text)
    hints, replacements = [], []
    for m in _CODE_LABEL_RE.finditer(text):
        code, label = m.group(1).upper(), m.group(2).rstrip(" ,")
        official = ICD10_TITLES.get(code) or ICD10_TITLES.get(code[:5])
        if not official or _norm_title(official) in _norm_title(label):
            continue
        in_dd = m.start() >= dd_start
        if _title_overlap(official, label) >= 0.6:
            replacements.append((m.start(2), m.start(2) + len(label), official))
        elif in_dd:
            hints.append((code, f"{code} heißt in der ICD-10 \"{official}\", im Text steht \"{label}\" "
                                "— Code prüfen"))
        else:
            hints.append((code, f"{code} heißt in der ICD-10 \"{official}\", im Text steht \"{label}\" "
                                "— Code und Bezeichnung prüfen"))
    for start, end, official in reversed(replacements):
        text = text[:start] + official + text[end:]
    if replacements:
        debug_logger.info(f"Abschnitt 5: {len(replacements)} ICD-Bezeichnung(en) vereinheitlicht")
    return text, hints


# Episodenschwere laut ICD-Code (F32.x / F33.x)
_EPISODE_SEVERITY = {"0": "leicht", "1": "mittel", "2": "schwer", "3": "schwer", "4": "remittiert"}


def add_diagnosis_hints(text, latest_bdi=None):
    """Haengt an Abschnitt 5 Pruefhinweise fuer formale Diagnosefehler an.

    Die Regeln stehen auch in prompt5-1/5-2, werden vom Modell aber nicht
    zuverlaessig befolgt. Diagnosen werden nicht veraendert, nur markiert --
    ausser rein umformulierte ICD-Bezeichnungen (fix_icd_titles).
    latest_bdi: juengster Eintrag aus extract_bdi_values() fuer den Abgleich
    Schweregrad <-> Testwert.
    """
    text, title_hints = fix_icd_titles(text)
    dd_match = re.search(r"Differen[zt]ialdiagnose", text, re.IGNORECASE)
    coded_part = text[:dd_match.start()] if dd_match else text
    dd_part = text[dd_match.start():] if dd_match else ""

    def codes(part):
        return [c[:3].upper() + c[3:].lower() for c in _ICD_CODE_RE.findall(part)]

    coded = codes(coded_part)
    dd_specific = {c for c in codes(dd_part) if "." in c}

    hints = []
    for c in dict.fromkeys(coded):
        if c.endswith(".x"):
            hints.append((c, f"{c} ist kein vollständiger Code, Subtyp angeben oder Diagnose streichen"))
    if "F43.2" in coded and any(c.startswith(("F32", "F33")) for c in coded):
        hints.append(("F43.2", "F43.2 wird neben einer depressiven Episode (F32/F33) nicht kodiert"))
    for c in dict.fromkeys(coded):
        if c in dd_specific:
            hints.append((c, f"{c} steht zugleich als Diagnose und als Differenzialdiagnose"))

    hints += title_hints

    # Vorlagenrest: "[DD nicht in Daten genannt]" obwohl darueber DDs stehen (Lauf 9)
    if dd_part and _ICD_CODE_RE.search(dd_part) and "[DD nicht in Daten genannt]" in dd_part:
        text = text[:dd_match.start()] + re.sub(r"\s*\[DD nicht in Daten genannt\]", "", dd_part)

    if latest_bdi:
        band = _bdi_band(latest_bdi["score"])
        when = _bdi_when(latest_bdi)
        for c in dict.fromkeys(coded):
            if not re.fullmatch(r"F3[23]\.[0-4]", c) or c == "F32.4":
                continue
            severity = _EPISODE_SEVERITY[c[-1]]
            fits = severity == "remittiert" if band == "minimal" else severity == band
            if not fits:
                hints.append((c, f"{c} ({severity}) passt nicht zum jüngsten BDI-II-Wert "
                                 f"({when}: {latest_bdi['score']} Punkte, Bereich {band}) — Schweregrad prüfen"))

    existing = " ".join(re.findall(r"\[Prüfhinweis[^\]]*\]", text))
    new_hints = [msg for code, msg in hints if code not in existing]
    if not new_hints:
        return text
    debug_logger.info(f"Abschnitt 5 Pruefhinweise: {new_hints}")
    return text.rstrip() + "\n\n" + "\n".join(f"[Prüfhinweis: {h}]" for h in new_hints)


# --- Abschnitt 1-3: Vollstaendigkeit, Befundbegriffe, Vermerke -------------

# Unterabschnitte, die in der Pass-2-Ausgabe fuer 1-3 stehen muessen.
# In Lauf 8 brach qwen3:14b einmal nach 2.3 ab, ohne dass es auffiel.
_REQUIRED_13 = [
    ("1", r"soziodemographische\s+Daten"),
    ("2.1", r"^\s*2\.1\b"), ("2.2", r"^\s*2\.2\b"), ("2.3", r"^\s*2\.3\b"),
    ("2.4", r"^\s*2\.4\b"), ("2.5", r"^\s*2\.5\b"),
    ("3", r"Somatischer\s+Befund"),
    ("3.1", r"^\s*3\.1\b"), ("3.2", r"^\s*3\.2\b"), ("3.3", r"^\s*3\.3\b"),
]


def missing_subsections_13(text):
    return [n for n, rx in _REQUIRED_13 if not re.search(rx, text, re.IGNORECASE | re.MULTILINE)]


_BEFUND_TERMS = ["Bewusstsein", "Orientierung", "Kognition", "Denken formal und inhaltlich", "Ängste",
                 "Wahrnehmung", "Ich-Störungen", "Affekt", "Antrieb", "zirkadiane Besonderheiten",
                 "Suizidalität"]


def _norm_label(s):
    return " ".join(re.sub(r"[^\w\s]", " ", s.lower()).split())


def check_befund_23(text):
    """Prueft 2.3 auf genau die 11 Begriffe in fester Reihenfolge und darauf,
    dass kein Begriff unter einem anderen mitbefundet wird."""
    m = re.search(r"^\s*2\.3\b.*?$(.*?)(?=^\s*(?:2\.4|3\.?\s|3\.\d)|\Z)", text, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    items = []
    for seg in re.split(r";|\n", m.group(1)):
        if ":" in seg:
            label, value = seg.split(":", 1)
            if 0 < len(label.strip()) <= 40:
                items.append((label.strip(), value.strip()))
    labels = [_norm_label(l) for l, _ in items]
    terms = [_norm_label(t) for t in _BEFUND_TERMS]
    hints = []
    missing = [t for t, n in zip(_BEFUND_TERMS, terms) if n not in labels]
    extra = [l for l, n in zip((l for l, _ in items), labels) if n not in terms]
    if missing:
        hints.append(f"2.3: Befund fehlt für {', '.join(missing)}")
    if extra:
        hints.append(f"2.3: zusätzliche Begriffe außerhalb des Schemas ({', '.join(extra)})")
    known = [l for l in labels if l in terms]
    if not missing and known != terms:
        hints.append("2.3: Reihenfolge der Befundbegriffe weicht vom Schema ab")
    for term in _BEFUND_TERMS:
        for label, value in items:
            if _norm_label(label) != _norm_label(term) and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", value, re.IGNORECASE):
                hints.append(f"2.3: \"{term}\" wird auch unter \"{label}\" befundet — Widerspruch prüfen")
    return hints


def _subsection_span(text, number, next_pattern):
    """(Start, Ende) des Inhalts von Unterabschnitt `number` (ohne Ueberschriftzeile)."""
    m = re.search(rf"^\s*{re.escape(number)}\b.*?$", text, re.MULTILINE)
    if not m:
        return None
    end = re.compile(rf"^\s*(?:{next_pattern})", re.MULTILINE).search(text, m.end())
    return m.end(), (end.start() if end else len(text))


# Psychische Vorerkrankungen/Belastungen gehoeren nicht in den somatischen Befund (Lauf 9:
# "Essstoerung" in 3.1 bei beiden Kombis). \b vorne, damit "antidepressiv" nicht trifft.
_PSYCH_IN_31_RE = re.compile(
    r"\b(Essstörung|Anorexi\w*|Bulimi\w*|Depression\w*|depressiv\w*|Angststörung|Panikstörung|"
    r"Trauma\w*|traumati\w*|Missbrauch\w*|Gewalt\w*|Sucht\w*|Suizid\w*)", re.IGNORECASE)


def check_somatic_31(text):
    span = _subsection_span(text, "3.1", r"3\.2|3\.3")
    if not span:
        return []
    found = list(dict.fromkeys(m.group(1) for m in _PSYCH_IN_31_RE.finditer(text[span[0]:span[1]])))
    if not found:
        return []
    return [f"3.1: psychische Vorerkrankung oder Belastung im somatischen Befund ({', '.join(found)}) "
            "— gehört in Abschnitt 4"]


_FAMILY_STATUS_RE = re.compile(r"\b(verheiratet|geschieden|verwitwet|ledig|getrennt lebend|getrennt)\b",
                               re.IGNORECASE)


def check_family_status(section1, source_text):
    """Familienstand im Bericht muss als eigenes Wort in der Akte stehen.
    Lauf 9: "verheiratet" kam aus "einen verheirateten Mann" (andere Person)."""
    hints = []
    for word in dict.fromkeys(m.group(1).lower() for m in _FAMILY_STATUS_RE.finditer(section1)):
        if not re.search(rf"\b{re.escape(word)}\b", source_text, re.IGNORECASE):
            hints.append(f"Familienstand \"{word}\" steht so nicht in der Akte — prüfen")
    return hints


def section13_hints(text, source_text=""):
    """Pruefhinweise je Abschnitt (1, 2, 3) fuer die fertige 1-3-Ausgabe."""
    by_section = {0: [], 1: [], 2: []}
    missing = missing_subsections_13(text)
    for n in missing:
        by_section[int(n[0]) - 1].append(n)
    hints = {i: ([f"Abschnitt {', '.join(ns)} fehlt in der Modellausgabe — manuell ergänzen"] if ns else [])
             for i, ns in by_section.items()}
    if source_text:
        hints[0] += check_family_status(parse_sections(text)[0], source_text)
    hints[1] += check_befund_23(text)
    hints[2] += check_somatic_31(text)
    return hints


def _bdi_line(v):
    """Eine BDI-Zeile. Dieselbe Form in der Liste fuer Pass 1 und in 2.5 des Berichts."""
    line = f"{_bdi_when(v)}: BDI-II mit {v['score']} Punkten"
    if v["interpretation"]:
        line += f" ({v['interpretation']})"
        akte_band = next((b for rx, b in _INTERPRETATION_BANDS if rx.search(v["interpretation"])), None)
        band = _bdi_band(v["score"])
        if akte_band and akte_band != band:
            line += f" [Einordnung prüfen: nach Beck/Hautzinger Bereich {band}]"
    return line


# Andere Testverfahren in 2.5 erkennt man an ihrem Kuerzel (BAI, PHQ-9, SCL-90 ...)
_TEST_ACRONYM_RE = re.compile(r"\b[A-Z]{2,6}(?:-[IVX0-9]+)?\b")


def replace_bdi_in_25(text, bdi_values):
    """Ersetzt alle BDI-Angaben in 2.5 durch die Liste aus der Akte.
    Lauf 9: Die Modelle machten aus "mit 33 Punkten" wieder "33 Punkte" und liessen
    "[Einordnung pruefen]" weg. Andere Testverfahren bleiben stehen."""
    if not bdi_values:
        return text
    span = _subsection_span(text, "2.5", r"3\.|3\s|2\.6")
    if not span:
        return text
    body = text[span[0]:span[1]]
    others = []
    for seg in re.split(r";|\n", body):
        seg = seg.strip(" .")
        if not seg:
            continue
        is_bdi = "BDI" in seg.upper() or (
            re.search(r"\d+\s*Punkt", seg) and not _TEST_ACRONYM_RE.search(seg))
        if not is_bdi:
            others.append(seg)
    new_body = "\n" + "\n".join(_bdi_line(v) for v in bdi_values)
    if others:
        new_body += "\n" + "; ".join(others) + "."
    return text[:span[0]] + new_body + "\n\n" + text[span[1]:].lstrip("\n")


_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_MONTH_YEAR_RE = re.compile(rf"\b({'|'.join(_MONTH_NAMES)})\s+(\d{{4}})\b")


def _dates_with_pos(text):
    """[(Start, Ende, Datum)] aller Zeitpunkte; Monat+Jahr zaehlt als Monatsende."""
    found = []
    for m in _DATE_RE.finditer(text):
        try:
            found.append((m.start(), m.end(), date(int(m.group(3)), int(m.group(2)), int(m.group(1)))))
        except ValueError:
            pass
    for m in _MONTH_YEAR_RE.finditer(text):
        mo, y = _MONTH_NAMES.index(m.group(1)) + 1, int(m.group(2))
        found.append((m.start(), m.end(), date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)))
    return found


def fix_past_planned(text, today):
    """3.3: "geplant" bei einem Zeitpunkt, der schon vorbei ist -> "[Durchführung prüfen]".
    Lauf 9: "Reha ab 19.01.2026, geplant" im September 2026. Pass 2 vergleicht Daten nicht.
    Jedes "geplant" wird dem naechsten Zeitpunkt im selben Teilsatz zugeordnet, weil
    mehrere Massnahmen oft in einer Kommaliste stehen."""
    span = _subsection_span(text, "3.3", r"\d+\.\s|\d+\.\d")
    if not span:
        return text
    body = text[span[0]:span[1]]
    dates = _dates_with_pos(body)
    for m in reversed(list(re.finditer(r"\bgeplant\b(\s+für\s+)?", body))):
        # Teilsatzgrenzen: Semikolon, Zeilenende, Satzende nach einem Wort
        left = max(body.rfind(";", 0, m.start()), body.rfind("\n", 0, m.start()),
                   max((s.end() for s in re.finditer(r"[A-Za-zäöüßÄÖÜ\)]\.\s", body[:m.start()])), default=-1))
        right_m = re.compile(r";|\n|[A-Za-zäöüßÄÖÜ\)]\.\s").search(body, m.end())
        right = right_m.start() if right_m else len(body)
        candidates = [d for d in dates if d[0] >= left and d[1] <= right + 1]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda d: min(abs(d[0] - m.end()), abs(m.start() - d[1])))
        if nearest[2] >= today:
            continue
        if m.group(1) and nearest[0] >= m.end():
            # "geplant für 01.03.2026" -> "01.03.2026, [Durchführung prüfen]"
            body = (body[:m.start()] + body[nearest[0]:nearest[1]] + ", [Durchführung prüfen]"
                    + body[nearest[1]:])
        else:
            start, end = m.start(), m.end()
            if start > 0 and body[start - 1] == "(":
                # "(geplant)" -> "[Durchführung prüfen]", "(geplant, 5 Wochen)" -> "([…], 5 Wochen)"
                if body[end:end + 1] == ")":
                    start, end = start - 1, end + 1
                    while start > 0 and body[start - 1] == " ":
                        start -= 1
                    body = body[:start] + " [Durchführung prüfen]" + body[end:]
                else:
                    body = body[:start] + "[Durchführung prüfen]" + body[end:]
            else:
                while start > 0 and body[start - 1] in " ,":
                    start -= 1
                body = body[:start] + ", [Durchführung prüfen]" + body[end:]
        dates = _dates_with_pos(body)
    return text[:span[0]] + body + text[span[1]:]


# Vermerke, die das Modell in eigenen Worten schreibt, auf die erlaubten Marker bringen
_MARKER_FIXES = [
    (re.compile(r"\b(?:unbekannter|nicht angegebener) Zeitraum\b"
                r"|\bZeitraum (?:nicht (?:angegeben|dokumentiert|bekannt)|unbekannt|unklar)\b", re.IGNORECASE),
     "[Zeitraum prüfen]"),
    (re.compile(r"\bJahr (?:nicht (?:angegeben|dokumentiert|bekannt)|unbekannt|unklar)\b", re.IGNORECASE),
     "[Jahr prüfen]"),
    (re.compile(r"\bStand nicht dokumentiert\b", re.IGNORECASE), "[Durchführung prüfen]"),
    # Vorlagenreste wie "DD-Überlegungen aus Pass 1:" (Lauf 8)
    (re.compile(r"(?:DD-Überlegungen\s+)?(?:aus|laut|gemäß)\s+Pass[- ]?1\s*:?\s*", re.IGNORECASE), ""),
    # Wörtlich übernommene Vorlagen-Anweisung (Lauf 10)
    (re.compile(r"\s*Gibt es keine [\w-]+,?\s+(?:unter [^\n.]*?\s+)?nur (?:das Wort:?\s*)?\"?Keine\.?\"?\.?",
                re.IGNORECASE), ""),
]


def _append_hints(text, hints):
    if not hints:
        return text
    return text.rstrip() + "\n\n" + "\n".join(f"[Prüfhinweis: {h}]" for h in hints)


def normalize_markers(text):
    for rx, repl in _MARKER_FIXES:
        text = rx.sub(repl, text)
    # "(Jahr prüfen)" / "…, Jahr prüfen)" -> "[Jahr prüfen]" (Lauf 9)
    text = re.sub(r"(?<!\[)\b(Jahr|Zeitraum|Durchführung|Einordnung) prüfen\b(?![^\[\]]*\])", r"[\1 prüfen]", text)
    # "([Zeitraum prüfen])" -> "[Zeitraum prüfen]"
    return re.sub(r"\(\s*(\[[^\]]+\])\s*\)", r"\1", text)


# --- BDI-Werte deterministisch aus der Akte lesen ---------------------------
# Das Modell trug die ueber viele Sitzungsnotizen verstreuten Werte in jedem
# Lauf anders zusammen (andere Werte, doppelte Daten, falsche Reihenfolge).

_MONTHS = {
    "januar": 1, "jan": 1, "februar": 2, "feb": 2, "märz": 3, "maerz": 3, "mär": 3, "mrz": 3,
    "april": 4, "apr": 4, "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "oktober": 10, "okt": 10,
    "november": 11, "nov": 11, "dezember": 12, "dez": 12,
}
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ENTRY_DATE_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})")
_BDI_RE = re.compile(r"BDI(?:[\s-]*(?:II|2)(?!\d))?", re.IGNORECASE)
_SCORE_RE = re.compile(r"(\d{1,2})\s*(?:Punkte?n?|Pkt\.?)", re.IGNORECASE)
_NUM_DATE_RE = re.compile(r"(\d{1,2})\.\s?(\d{1,2})\.(?:\s?(\d{4}|\d{2})(?!\d))?")
_DAY_MONTH_RE = re.compile(rf"(\d{{1,2}})\.\s*({_MONTH_ALT})\.?\s*(\d{{4}})?", re.IGNORECASE)
_MONTH_RE = re.compile(rf"\b({_MONTH_ALT})\b\.?\s*(\d{{4}})?", re.IGNORECASE)


def _full_year(y):
    y = int(y)
    return y + 2000 if y < 100 else y


def _infer_year(month, day, entry):
    """Jahr fuer Datumsangaben ohne Jahr: Jahr des Eintrags, bzw. Vorjahr,
    wenn das Datum sonst nach dem Eintrag laege (13.12. im Eintrag vom 13.01.2025)."""
    if not entry:
        return None
    e_day, e_month, e_year = entry
    return e_year - 1 if (month, day or 0) > (e_month, e_day) else e_year


def extract_bdi_values(text):
    """Liest BDI-Werte mit Datum aus Freitext-Notizen.

    Erkennt u.a. "BDI 2 vom 13.12. mit 47 Punkten", "BDI 2 10.1.25 33 Punkte",
    "BDI 2 von Januar mit 40 Punkten", "BDI 2 vom 5. März 2025 mit 27 Punkten".
    Fehlt das Jahr, wird es aus dem Datum am Zeilenanfang (Eintragsdatum) abgeleitet.
    """
    results = []
    entry = None
    for line in text.splitlines():
        m = _ENTRY_DATE_RE.match(line)
        if m:
            entry = (int(m.group(1)), int(m.group(2)), _full_year(m.group(3)))
        matches = list(_BDI_RE.finditer(line))
        for i, bdi in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            segment = line[bdi.end():min(end, bdi.end() + 200)]
            score = _SCORE_RE.search(segment)
            if not score:
                continue
            before = segment[:score.start()]
            day = month = year = None
            if (d := _NUM_DATE_RE.search(before)):
                day, month = int(d.group(1)), int(d.group(2))
                year = _full_year(d.group(3)) if d.group(3) else None
            elif (d := _DAY_MONTH_RE.search(before)):
                day, month = int(d.group(1)), _MONTHS[d.group(2).lower()]
                year = int(d.group(3)) if d.group(3) else None
            elif (d := _MONTH_RE.search(before)):
                month = _MONTHS[d.group(1).lower()]
                year = int(d.group(2)) if d.group(2) else None
            elif entry:
                day, month, year = entry
            if month is None or not 1 <= month <= 12:
                continue
            if year is None:
                year = _infer_year(month, day, entry)

            interpretation = re.split(r"[,;]", segment[score.end():], maxsplit=1)[0].strip(" .")
            results.append({
                "day": day, "month": month, "year": year,
                "score": int(score.group(1)),
                "interpretation": interpretation[:80],
            })

    unique = {}
    for r in results:
        key = (r["year"] or 0, r["month"], r["day"] or 0, r["score"])
        if key not in unique or (not unique[key]["interpretation"] and r["interpretation"]):
            unique[key] = r
    return [unique[k] for k in sorted(unique)]


# Schweregrad-Woerter in der Einordnung der Akte -> BDI-Bereich
_INTERPRETATION_BANDS = [
    (re.compile(r"schwer", re.IGNORECASE), "schwer"),
    (re.compile(r"mittel", re.IGNORECASE), "mittel"),
    (re.compile(r"leicht", re.IGNORECASE), "leicht"),
    (re.compile(r"\bkein|minimal|unauff|remitt", re.IGNORECASE), "minimal"),
]


def _bdi_when(v):
    # Ohne Tagesangabe den Monat ausschreiben: aus "01/2025" machte Pass 2 "01.01.2025"
    year = v["year"] or "[Jahr prüfen]"
    if v["day"]:
        return f"{v['day']:02d}.{v['month']:02d}.{year}"
    return f"{_MONTH_NAMES[v['month'] - 1]} {year}"


def format_bdi_block(values):
    if not values:
        return ""
    dates = [(v["year"], v["month"], v["day"]) for v in values]
    lines = []
    for v in values:
        line = f"- {_bdi_line(v)}"
        if dates.count((v["year"], v["month"], v["day"])) > 1:
            line += " [widersprüchliche Angaben zu diesem Datum in der Akte]"
        lines.append(line)
    return (
        "BDI-II-WERTE (automatisch aus der Akte gelesen, chronologisch, VERBINDLICH):\n"
        + "\n".join(lines)
        + "\nFür BDI-Ergebnisse AUSSCHLIESSLICH diese Liste verwenden, in dieser Reihenfolge. "
        "Keine Werte ergänzen, weglassen oder umdatieren. Datumsangaben genau so übernehmen "
        "(steht nur ein Monat da, KEINEN Tag ergänzen). Hinweise in eckigen Klammern unverändert übernehmen."
    )


# --- Kalenderwochen in Daten umrechnen --------------------------------------
# Die Akte plant Massnahmen oft per Kalenderwoche ("Reha in KW 4"). Das Modell
# kann das nicht in ein Datum umrechnen und schrieb "Zeitraum nicht angegeben".

_KW_RE = re.compile(r"\b(?:Kalenderwoche|KW)\s*(\d{1,2})(?:\s*[/.]\s*(\d{4}|\d{2})(?!\d))?", re.IGNORECASE)


def extract_calendar_weeks(text):
    """Findet KW-Angaben und rechnet sie in den Montag der Woche um.

    Ohne Jahresangabe gilt die erste KW mit dieser Nummer ab dem Eintragsdatum
    (KW 4 in einem Eintrag vom Oktober 2025 -> Januar 2026). Zeilen ohne
    Eintragsdatum und ohne Jahr werden uebersprungen.
    """
    results = []
    entry = None
    for line in text.splitlines():
        m = _ENTRY_DATE_RE.match(line)
        if m:
            try:
                entry = date(_full_year(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                entry = None
        for kw in _KW_RE.finditer(line):
            week = int(kw.group(1))
            try:
                if kw.group(2):
                    year = _full_year(kw.group(2))
                    monday = date.fromisocalendar(year, week, 1)
                elif entry:
                    year = entry.year
                    monday = date.fromisocalendar(year, week, 1)
                    if monday < entry - timedelta(days=6):
                        year += 1
                        monday = date.fromisocalendar(year, week, 1)
                else:
                    continue
            except ValueError:
                continue
            start = max(0, kw.start() - 70)
            snippet = line[start:kw.end() + 30].strip()
            snippet = snippet.split(" ", 1)[-1] if start else snippet  # angeschnittenes Wort weg
            results.append({"entry": entry, "week": week, "year": year, "monday": monday,
                            "snippet": re.sub(r"^\d{1,2}\.\d{1,2}\.\d{2,4}\s*", "", snippet)})

    # Kopierte Notizen wiederholen denselben Satz, teils mit laengerem Ausschnitt davor
    unique = []
    for r in results:
        norm = _norm_label(r["snippet"])
        if not any(u["monday"] == r["monday"] and (norm.endswith(_norm_label(u["snippet"]))
                                                   or _norm_label(u["snippet"]).endswith(norm))
                   for u in unique):
            unique.append(r)
    return sorted(unique, key=lambda r: r["monday"])


def format_calendar_week_block(weeks):
    if not weeks:
        return ""
    lines = []
    for w in weeks:
        src = f"Eintrag vom {w['entry']:%d.%m.%Y}" if w["entry"] else "Akte"
        lines.append(f"- {src}: \"{w['snippet']}\" -> KW {w['week']}/{w['year']} = Woche ab {w['monday']:%d.%m.%Y}")
    return (
        "TERMINE MIT KALENDERWOCHE (automatisch in ein Datum umgerechnet, VERBINDLICH):\n"
        + "\n".join(lines)
        + "\nFür diese Maßnahmen dieses Datum als Zeitraum verwenden und NICHT \"Zeitraum nicht angegeben\" schreiben."
    )


# --- Namenpruefung -----------------------------------------------------------
# Die Pass-2-Prompts verlangen Rollen statt Namen, das Modell haelt sich aber
# nicht zuverlaessig daran. Namen AUFZULISTEN klappt dagegen gut.

NAME_CHECK_PROMPT = """Du bist ein Prüfwerkzeug für die Anonymisierung von Berichten.
Liste ALLE Eigennamen auf, mit denen man die Patientin oder ihr Umfeld identifizieren könnte:
- Vor- und Nachnamen realer Personen (Kinder, Partner, Angehörige, Freunde, Kollegen, Behandler),
- Namen von Firmen und Arbeitgebern (z.B. "Müller Logistik"),
- Namen von Einrichtungen (Kliniken, Schulen, Praxen, Vereine),
- Namen von Orten (Städte, Dörfer, Stadtteile).
NICHT auflisten: "Frau X.", "Herr X.", "F.", allgemeine Begriffe ohne Eigennamen (z.B. "der Sohn", "Dorfladen", "Jugendamt", "psychosomatische Klinik"), Methoden, Testverfahren, Medikamente, Autorennamen in Methodenbezeichnungen (z.B. "nach Sulz").
Gib jeden Namen genau so an, wie er im Text steht.
Antworte AUSSCHLIESSLICH mit einem JSON-Array von Strings, z.B. ["Anna", "Müller Logistik"]. Kommen keine Namen vor, antworte mit []."""

# Anonymisierte Platzhalter, die das Modell trotz Anweisung gelegentlich meldet
# Rolle + Initial ("Partner Y.", Lauf 9) ist ein Platzhalter, Vorname + Initial ("Anna B.") nicht
_PLACEHOLDER_RE = re.compile(
    r"^(?:(?:Frau|Herr|Fr\.|Hr\.|Dr\.|Partner|Partnerin|Ehemann|Ehefrau|Ex-Partner|Ex-Partnerin|Sohn|"
    r"Tochter|Bruder|Schwester|Mutter|Vater|Freund|Freundin|Kollege|Kollegin|Patient|Patientin)\s+)?"
    r"(?:[A-ZÄÖÜ]\.|[XF])$")
_ARTICLE_BEFORE_RE = re.compile(
    r"\b(?:der|die|das|den|dem|des|ein|eine|einem|einen|einer|eines|im|am|vom|zum|zur|ins|beim)\s+$",
    re.IGNORECASE,
)


def find_person_names(text, model_name):
    """Laesst das Modell Personennamen auflisten und behaelt nur die, die
    tatsaechlich als ganzes Wort im Text stehen (keine erfundenen Treffer)."""
    answer = answer_question(
        question=text, system_prompt=NAME_CHECK_PROMPT, uploaded_files=None,
        model_name=model_name, disable_rag=True, temperature=0.0,
    )
    try:
        names = json.loads(answer[answer.index("["):answer.rindex("]") + 1])
    except (ValueError, TypeError):
        debug_logger.warning("Namenpruefung: Antwort nicht lesbar"
                             + (f": {answer[:200]}" if LOG_PATIENT_CONTENT else ""))
        return []
    found = []
    for name in names:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if len(name) < 2 or _PLACEHOLDER_RE.match(name) or not name[0].isupper():
            continue
        hits = list(re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", text))
        if not hits or name in found:
            continue
        # Einzelwort, das nur mit Artikel vorkommt ("im Dorfladen"), ist ein Gattungsbegriff.
        # Das Modell meldete "Dorfladen" trotz ausdruecklicher Ausnahme im Prompt.
        if " " not in name and all(_ARTICLE_BEFORE_RE.search(text[max(0, h.start() - 10):h.start()]) for h in hits):
            continue
        found.append(name)
    return found


def add_name_hints(sections, names):
    """Haengt an jeden Abschnitt, der einen der Namen enthaelt, einen Pruefhinweis an."""
    result = []
    for section in sections:
        hits = [n for n in names if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", section)]
        if hits:
            section = section.rstrip() + (
                f"\n\n[Prüfhinweis: identifizierende Namen im Text ({', '.join(hits)}) — anonymisieren]"
            )
        result.append(section)
    return result


# --- Anonymisierung ----------------------------------------------------------

# Aktenkopf aus der Praxissoftware: "Nachname,  Vorname geb.  19.09.1975"
_PATIENT_HEAD_RE = re.compile(
    r"^\s*([A-ZÄÖÜ][\w'-]+(?:[ -][A-ZÄÖÜ][\w'-]+)?),\s+([A-ZÄÖÜ][\w'-]+(?:[ -][A-ZÄÖÜ][\w'-]+)?)"
    r"\s+geb\.?\s*(?:am\s+)?(\d{1,2}\.\d{1,2}\.\d{2,4})", re.MULTILINE)


def extract_patient_identity(source_text):
    """Name und Geburtsdatum aus den ersten Zeilen der Akte, sonst None."""
    head = "\n".join(source_text.splitlines()[:5])
    m = _PATIENT_HEAD_RE.search(head)
    if not m:
        return None
    d, mo, y = m.group(3).split(".")
    return {"last": m.group(1), "first": m.group(2), "dob": (int(d), int(mo), _full_year(y))}


def anonymize_patient(text, identity):
    """Ersetzt Name, Initialen und Geburtsdatum der Patientin/des Patienten deterministisch.
    Lauf 10: Pass 1 uebernahm den Namen aus dem Aktenkopf, im Bericht stand "Frau X. D."."""
    if not identity or not text:
        return text
    first, last = re.escape(identity["first"]), re.escape(identity["last"])
    fi, li = re.escape(identity["first"][0]), re.escape(identity["last"][0])
    title = "Frau" if "Frau X." in text else ("Herr" if "Herr X." in text else None)
    plain = f"{title} X." if title else "X."
    rules = [
        (rf"\b(Frau|Herr)\s+(?:{first}\s+)?{last}\b", r"\1 X."),
        (rf"\b{first}\s+{last}\b", plain),
        (rf"\b{last},?\s+{first}\b", plain),
        (rf"\b{last}\b", "X."),
        (rf"\b{first}\b", "X."),
        (rf"\b{fi}\.\s*{li}\.(?!\w)", "X."),
        (rf"\b(Frau|Herr)\s+X\.\s*(?:X\.|{li}\.)(?!\w)", r"\1 X."),
    ]
    for rx, repl in rules:
        text = re.sub(rx, repl, text)
    d, mo, y = identity["dob"]
    text = re.sub(rf"\b0?{d}\.\s?0?{mo}\.\s?(?:{y}|{y % 100:02d})\b", "[Geburtsdatum entfernt]", text)
    return text


ANONYMIZE_PROMPT = """Du bist ein Werkzeug zur Anonymisierung von Berichten.
Du erhältst ein kurzes Textstück und eine Liste von Eigennamen.
Ersetze JEDEN dieser Namen durch eine passende Rollen- oder Gattungsbezeichnung aus dem Zusammenhang, z.B. "der jüngere Sohn", "ein früherer Partner", "eine Freundin", "ein Arbeitgeber", "eine Klinik".
- Steht die Rolle schon daneben, den Namen nur streichen.
- Steht der Name in Klammern, die Klammer samt Namen streichen.
- Artikel und Endungen an den Satz anpassen ("bei einem Arbeitgeber", nicht "bei ein Arbeitgeber").
Ändere SONST NICHTS: keine anderen Wörter, keine Umformulierung, keine Kürzung, keine Ergänzung.
Antworte AUSSCHLIESSLICH mit dem geänderten Textstück, ohne Anführungszeichen und ohne Erklärung.

BEISPIELE (erfundene Namen):
NAMEN: Lena | TEXTSTÜCK: Streit mit der Tochter Lena am Wochenende
-> Streit mit der Tochter am Wochenende
NAMEN: Paul | TEXTSTÜCK: eine zweite Beziehung (Paul) endete durch Druck der Familie
-> eine zweite Beziehung endete durch Druck der Familie
NAMEN: Schmidt Bau | TEXTSTÜCK: arbeitet seit Mai bei Schmidt Bau als Bürokraft
-> arbeitet seit Mai bei einem Bauunternehmen als Bürokraft
NAMEN: Karl | TEXTSTÜCK: Karl habe sie oft kritisiert
-> Ein Bekannter habe sie oft kritisiert"""


_FIELD_LABEL_RE = re.compile(r"^(?:[A-ZÄÖÜ][\w+/()-]{0,25}\s*:\s*)+")
# Satzende nur nach mindestens zwei Kleinbuchstaben vor dem Punkt ("z. B.", "Dr.", "ca. 12."
# trennen nicht) und vor einem Grossbuchstaben
_FRAGMENT_SEP_RE = re.compile(r";|\n|(?<=[a-zäöüß]{2}[.!?])\s+(?=[A-ZÄÖÜ„\"])")
# Initial in Klammern hinter einer Rolle: "Beziehung zu einem Partner (Y.)" (Lauf 11)
_INITIAL_PARENS_RE = re.compile(r"\s*\((?:[A-ZÄÖÜ]\.\s*){1,2}\)")


def _paren_balance(s):
    return s.count("(") - s.count(")")


def strip_initial_parens(text):
    return _INITIAL_PARENS_RE.sub("", text)


def _name_re(name):
    return re.compile(rf"(?<!\w){re.escape(name)}(?!\w)")


def anonymize_names(sections, names, model_name):
    """Ersetzt Namen Dritter durch Rollen: Nur die Satzteile mit einem Namen gehen ans
    Modell (T=0), der Code prueft, dass der Name weg ist und sich sonst kaum etwas
    geaendert hat. Sonst wird der Name auf die Initiale gekuerzt.
    Rueckgabe: neue Abschnitte. Die Namen selbst stehen danach nirgends mehr, auch
    nicht im Pruefhinweis."""
    result = []
    for section in sections:
        hits = [n for n in names if _name_re(n).search(section)]
        if not hits:
            result.append(section)
            continue
        replaced, shortened = 0, 0
        # Satzteile mit einem Namen. Getrennt wird an Semikolon, Zeilenende und Satzende --
        # nicht an Abkuerzungen: Lauf 11 trennte "(z. B. durch die Kollegin <Name>)" nach
        # "z.", das Modell bekam eine einzelne ")" und liess sie weg.
        pieces = _FRAGMENT_SEP_RE.split(section)
        for frag in dict.fromkeys(p for p in pieces if any(_name_re(n).search(p) for n in hits)):
            frag_names = [n for n in hits if _name_re(n).search(frag)]
            # Feldbezeichnungen ("S: Extern:", "C+:") nicht mitschicken, das Modell liess sie weg
            label = _FIELD_LABEL_RE.match(frag.strip())
            label = label.group(0) if label and not any(_name_re(n).search(label.group(0)) for n in frag_names) else ""
            core = frag.strip()[len(label):]
            answer = answer_question(
                question=f"NAMEN: {', '.join(frag_names)} | TEXTSTÜCK: {core}",
                system_prompt=ANONYMIZE_PROMPT, uploaded_files=None,
                model_name=model_name, disable_rag=True, temperature=0.0,
            ).strip().removeprefix("->").strip().strip('"„“')
            ok = (answer and not answer.startswith(("❌", "⏱️"))
                  and not any(_name_re(n).search(answer) for n in frag_names)
                  and "\n" not in answer
                  and 0.5 * len(core) <= len(answer) <= len(core) + 60
                  and _paren_balance(answer) == _paren_balance(core)
                  and SequenceMatcher(None, core, answer).ratio() >= 0.7)
            if ok:
                new = frag.replace(core, answer)
                replaced += len(frag_names)
            else:
                new = frag
                for n in frag_names:
                    new = _name_re(n).sub(n[0] + ".", new)
                shortened += len(frag_names)
            section = section.replace(frag, new, 1)
        notes = []
        if replaced:
            notes.append(f"{replaced} Name(n) automatisch durch Rollen ersetzt — Formulierung prüfen")
        if shortened:
            notes.append(f"{shortened} Name(n) auf die Initiale gekürzt — durch Rolle ersetzen")
        result.append(_append_hints(section, notes))
    return result


# --- SORC-Konsequenzen aus zerlegten Angaben ----------------------------------
# Kein Modell ordnete C+/C-/C+//C-/ zuverlaessig zu (auch gemma4:26b nicht).
# Pass 1 beantwortet deshalb je Konsequenz nur "tritt ein / faellt weg" und
# "angenehm / unangenehm" (K-Zeilen, prompt4-1.txt); die Zuordnung macht der Code.

_K_LINE_RE = re.compile(r"^\s*[-*]?\s*K\s*:\s*(.+)$")
_C_LINE_RE = re.compile(r"^\s*C\s*[+-]\s*/?\s*:")
_KONSEQUENZEN_HEADER_RE = re.compile(r"^\s*KONSEQUENZEN\s*:?\s*$", re.IGNORECASE)
# Wer das als eintretende angenehme Folge beschreibt, meint meist negative Verstaerkung
_RELIEF_RE = re.compile(r"entlast|erleichter|\bruhe\b|beruhig|vermeidung|spannungsredu", re.IGNORECASE)
_PLEASANT_RE = re.compile(
    r"stabilit|kontrolle\b|sicherheit|anerkennung|\blob\b|zuwendung|autonomie|bestätigung|"
    r"wertschätzung|selbstwert|zugehörigkeit", re.IGNORECASE)
_NEGATED_RE = re.compile(r"verlust|mangel|fehlend|\bkein|\bohne\b|weniger|sinkend|verlier|unsicher|instabil",
                         re.IGNORECASE)
# "kurzfristige Ruhe" -> "Ruhe": die Phrase setzt "kurzfristig"/"langfristig" selbst davor
_TIME_PREFIX_RE = re.compile(r"^\s*(?:kurz|lang)fristig(?:e[nmrs]?)?\b[\s:,]*", re.IGNORECASE)

_C_TYPES = [
    # (Label, faellt weg?, angenehm?)
    ("C+", False, True),
    ("C-", False, False),
    ("C+/", True, True),
    ("C-/", True, False),
]


def _consequence_phrase(label, behavior, short, long):
    # Doppelpunkt-Form, weil die Modellangaben im Nominativ kommen
    # ("zu soziale Isolation" waere falsch dekliniert).
    if label == "C+/":
        text = f"{behavior}: kurzfristig Wegfall von Angenehmem ({short})"
    elif label == "C-/":
        text = f"{behavior}: kurzfristig Wegfall von Unangenehmem ({short}), dadurch aufrechterhalten"
    else:
        text = f"{behavior}: kurzfristig {short}"
    if long and long.strip("-– ").strip():
        text += f", langfristig {long}"
    return text


_AVOIDANCE_RE = re.compile(
    r"^(?:(?:Vermeidung|Reduktion|Reduzierung|Verringerung|Verminderung|Ausbleiben|Wegfall|Abnahme|"
    r"Nachlassen|Abbau)\s+(?:von|der|des)|(?:Schutz|Bewahrung)\s+vor)\s+(?:ein(?:e[mnrs]?)?\s+)?(.+)$",
    re.IGNORECASE)
# Mehr als 8 K-Zeilen deuten auf eine Wiederholungsschleife hin (prompt4-1 verlangt 3-8)
_MAX_CONSEQUENCES = 8


def _lower_leading_adjective(s):
    """"Erhöhte Belastung" -> "erhöhte Belastung" (steht hinter "kurzfristig"/"langfristig").
    Nur wenn ein grossgeschriebenes Wort mit Adjektivendung vor einem weiteren
    grossgeschriebenen Wort steht, damit Substantive ("Ruhe") gross bleiben."""
    words = s.split(" ", 2)
    if (len(words) >= 2 and words[0][:1].isupper() and words[1][:1].isupper()
            and re.search(r"(?:e|en|er|es|em)$", words[0])):
        return s[0].lower() + s[1:]
    return s


def _similar_consequence(a, b):
    """Gleiches Verhalten und fast gleiche Folge (Tippfehler). Kurze Folgen nur exakt,
    sonst wuerden "Wut" und "Mut" zusammenfallen."""
    (beh_a, short_a), (beh_b, short_b) = a, b
    if SequenceMatcher(None, beh_a, beh_b).ratio() < 0.85 or min(len(short_a), len(short_b)) < 8:
        return False
    return SequenceMatcher(None, short_a, short_b).ratio() >= 0.85


def sorc_structure_hints(section_text):
    """S-intern darf nicht einfach die Ueberlebensregel aus O wiederholen (Lauf 8)."""
    intern = re.search(r"Intern\s*:\s*([^\n]+?)\s*(?:\n|$)", section_text)
    rule = re.search(r"Überlebensregel\s*:\s*([^\n;]+)", section_text)
    if intern and rule:
        a, b = set(_norm_label(intern.group(1)).split()), set(_norm_label(rule.group(1)).split())
        shorter = min(len(a), len(b))
        # Wortueberlappung, weil S-intern oft eine gekuerzte Fassung der Regel ist
        if shorter >= 4 and len(a & b) / shorter >= 0.8:
            return ["S-intern wiederholt die Überlebensregel — innere Auslöser im Moment nennen "
                    "(Gedanken, Körperempfindungen, Gefühle direkt vor dem Verhalten)"]
    return []


def build_consequence_lines(pass1_text):
    """Ersetzt die K-Zeilen aus Pass 1 durch fertige C+/C-/C+//C-/-Zeilen.

    Rueckgabe: (neuer Pass-1-Text, C-Block oder None, Pruefhinweise).
    Ohne gueltige K-Zeilen bleibt der Text unveraendert (C-Block None).
    """
    lines = pass1_text.splitlines()
    k_idx = [i for i, l in enumerate(lines) if _K_LINE_RE.match(l)]
    if not k_idx:
        return pass1_text, None, ["Konsequenzen nicht im erwarteten Format (K-Zeilen) — C-Zeilen prüfen"]

    by_type = {label: [] for label, _, _ in _C_TYPES}
    hints = []
    malformed = 0
    reinterpreted = 0
    moved_to_plus = 0
    overflow = 0
    seen = {}  # (Verhalten, kurzfristige Folge) -> Label
    reported = set()
    for i in k_idx:
        fields = [f.strip() for f in _K_LINE_RE.match(lines[i]).group(1).split("|")]
        if len(fields) < 4:
            malformed += 1
            continue
        behavior, short, direction, valence = fields[:4]
        short = _lower_leading_adjective(_TIME_PREFIX_RE.sub("", short))
        long = _lower_leading_adjective(_TIME_PREFIX_RE.sub("", fields[4])) if len(fields) > 4 else ""
        gone = "weg" in direction.lower()
        pleasant = not valence.lower().lstrip().startswith("un")

        # "Vermeidung von Kritik | tritt ein | angenehm" meint "Kritik | faellt weg | unangenehm".
        # Lauf 9: gemma4:12b schrieb so 9 Konsequenzen unter C+. Umkehren statt nur markieren.
        avoided = _AVOIDANCE_RE.match(short)
        if avoided and gone != pleasant:  # C+ (tritt ein, angenehm) oder C-/ (faellt weg, unangenehm)
            if not gone:
                reinterpreted += 1
            short, gone, pleasant = avoided.group(1).strip(), True, False
        label = next(lb for lb, g, p in _C_TYPES if g == gone and p == pleasant)
        # Angenehmer Zustand unter C-/C-/ ("Funktionieren -> Wegfall von Unangenehmem
        # (Stabilitaet)", Laeufe 8-10): gemeint ist, dass er eintritt bzw. erhalten bleibt -> C+
        if (label in ("C-", "C-/") and _PLEASANT_RE.search(short)
                and not _NEGATED_RE.search(short) and not _RELIEF_RE.search(short)):
            label = "C+"
            moved_to_plus += 1

        key = tuple(_norm_label(s) for s in (behavior, short))
        # Unscharf vergleichen: "Vermeidung" und "Vermehmung von Konfrontation" (Tippfehler, Lauf 8)
        key = next((k for k in seen if k == key or _similar_consequence(k, key)), key)
        if key in seen:
            # Exakte Dublette still verwerfen, widerspruechliche Zuordnung einmal melden
            if seen[key] != label and (key, label) not in reported:
                reported.add((key, label))
                hints.append(f"\"{behavior}: {short}\" steht widersprüchlich unter {seen[key]} und {label}")
            continue
        if len(seen) >= _MAX_CONSEQUENCES:
            overflow += 1
            continue
        seen[key] = label

        by_type[label].append(_consequence_phrase(label, behavior, short, long))
        # Entlastung ist negative Verstaerkung: das Unangenehme faellt weg (C-/).
        # Die Entlastung selbst ist aber nie die Folge, auch nicht unter C-/.
        if _RELIEF_RE.search(short):
            if label == "C-/":
                hints.append(f"C-/ \"{short}\" ist die Entlastung selbst — als Folge das wegfallende "
                             "Unangenehme nennen (z.B. Anspannung, Angst)")
            else:
                hints.append(f"{label} \"{short}\" beschreibt vermutlich Entlastung — gehört eher zu C-/")
    if malformed:
        hints.append(f"{malformed} Konsequenz(en) unvollständig angegeben und weggelassen")
    if moved_to_plus:
        hints.append(f"{moved_to_plus} Konsequenz(en) mit angenehmer Folge (z.B. Stabilität, Anerkennung) "
                     "automatisch als C+ eingeordnet — Zuordnung prüfen")
    if reinterpreted:
        hints.append(f"{reinterpreted} Konsequenz(en) der Form \"Vermeidung von …\" automatisch als C-/ "
                     "(Unangenehmes fällt weg) eingeordnet — Zuordnung prüfen")
    if overflow:
        hints.append(f"{overflow} weitere Konsequenz(en) verworfen, nur die ersten {_MAX_CONSEQUENCES} übernommen")

    c_block = "\n".join(
        f"{label}: {'; '.join(by_type[label]) or '[Angabe fehlt]'}" for label, _, _ in _C_TYPES
    )

    # K-Block (samt Ueberschrift und evtl. vom Modell geschriebenen C-Zeilen) durch C-Block ersetzen
    drop = set(k_idx)
    drop |= {i for i, l in enumerate(lines) if _KONSEQUENZEN_HEADER_RE.match(l) or _C_LINE_RE.match(l)}
    first = min(drop)
    new_lines = [l for i, l in enumerate(lines) if i not in drop]
    new_lines.insert(first, c_block)
    return "\n".join(new_lines), c_block, hints


def apply_consequence_lines(section_text, c_block, hints):
    """Setzt den C-Block unveraendert in den fertigen Abschnitt 4 (nach der R-Zeile),
    falls Pass 2 die Zeilen umformuliert hat, und haengt Pruefhinweise an."""
    if c_block:
        lines = [l for l in section_text.splitlines() if not _C_LINE_RE.match(l)]
        r_idx = [i for i, l in enumerate(lines) if re.match(r"^\s*R\s*:", l)]
        pos = r_idx[-1] + 1 if r_idx else len(lines)
        lines.insert(pos, c_block)
        section_text = "\n".join(lines)
    if hints:
        section_text = section_text.rstrip() + "\n\n" + "\n".join(f"[Prüfhinweis: {h}]" for h in hints)
    return section_text


def run_model_combination(combo, uploaded_files, paste_text, question, prompt1, prompt2, pass1_cache=None, combo_index=None, session_id=None, timing_log=None):
    """Fuehrt einen 1-Pass-Durchlauf mit einer Modellkombination aus.

    WICHTIG: Fuer Abschnitte 1-3 wird nur Pass 1 ausgefuehrt. Das Ergebnis ist direkt der fertige Bericht.
    prompt2 wird ignoriert, da kein zweiter Pass mehr stattfindet.

    Args:
        pass1_cache: Optional dict zum Cachen/Wiederverwenden von Pass1-Ergebnissen.
                     Key = Pass1-Modellname, Value = Pass1-Ergebnis.
        combo_index: Index der Kombination (ab 1) für Logging.
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

    Nutzt gemma4:12b für direkte Berichtserstellung.
    """
    model = "gemma4:12b"

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
             + bdi_suffix,
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
                    if key == "4":
                        if p1 in consequences:
                            text = apply_consequence_lines(text, *consequences[p1])
                        text = _append_hints(text, sorc_structure_hints(text))
                    if key == "5":
                        text = add_diagnosis_hints(text, latest_bdi)
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
                for i, hints in section13_hints(res["1-3"], "\n".join(source_texts)).items():
                    sections_13[i] = _append_hints(sections_13[i] or f"{i + 1}. {SECTION_HEADERS[i]}", hints)
            combo_sections = [sections_13[0], sections_13[1], sections_13[2], res["4"], res["5"], res["6"]]
            # Patientin/Patient selbst: deterministisch aus dem Aktenkopf
            combo_sections = [strip_initial_parens(anonymize_patient(s, patient)) for s in combo_sections]
            t0 = time.time()
            names = find_person_names("\n\n".join(combo_sections), NAME_CHECK_MODEL)
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

        # DOCX erstellen
        post_processed_results = ["\n\n".join(sections) for sections in parsed_results]
        docx_output = create_comparison_docx(
            post_processed_results, labeled_combos(), SECTION_HEADERS, parse_sections,
            enable_post_processing=False
        )

        # HTML-formatierte Ergebnisse
        html_results = []
        for parsed_result in parsed_results:
            html_sections = [format_text_as_html(section) for section in parsed_result]
            html_results.append(html_sections)

        # Modellnamen fuer Spaltenheader (inkl. Abschnitts-Override und eigener Temperatur)
        model_names = [combo_label(c) for c in MODEL_COMBINATIONS]

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
    # Auch der Einstiegspunkt im Container (Dockerfile: CMD python -m app.app).
    # Debug daher standardmaessig AUS: der Werkzeug-Debugger wuerde bei jeder
    # Exception eine interaktive Konsole ausliefern. Nur lokal per
    # FLASK_DEBUG=1 einschalten, nie im ueber Caddy erreichbaren Container.
    debug_mode = os.getenv("FLASK_DEBUG", "0").strip().lower() in ("1", "true", "yes")

    # threaded=True ist Pflicht: die SSE-Route /progress/<session_id> haelt eine
    # Verbindung offen, waehrend die Berechnung im Hintergrund-Thread laeuft.
    app.run(host="0.0.0.0", port=5000, debug=debug_mode, threaded=True)
