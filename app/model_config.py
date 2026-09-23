# app/model_config.py
"""Modellkombinationen, Abschnitts-Header und Prompts.

Einzige Stelle fuer Pass-1-/Pass-2-Modelle je Kombi; UI (index.html) und DOCX
lesen ihre Spaltenbeschriftung ueber combo_label()/labeled_combos() daraus.
"""
import os

# Modellkombinationen. Jede Kombi rechnet alle Abschnitte mit ihrem Pass-1- und
# Pass-2-Modell; die Ergebnisse stehen in der Vergleichstabelle nebeneinander.
# Laeufe mit identischem Modell und Abschnitt werden nur einmal gerechnet.
# "pass2_temperature": optionale Basis-Temperatur fuer Pass 2 (Default: 0.1)
# "pass1_override": optionales Pass-1-Modell je Abschnitt, z.B. {"6": "gemma4:12b"}
MODEL_COMBINATIONS = [
    # Abschnitt 6 mit 12b: spart ca. 2 Min., gemma4:26b bringt dort kaum Mehrwert.
    {"pass1": "gemma4:26b", "pass2": "qwen3:14b", "pass1_override": {"6": "gemma4:12b"}},
]
# Kombi 2 (gemma4:12b fuer alle Abschnitte) wurde im September 2026 nach Vergleichslaeufen mit
# zwei Akten entfernt: leere Abschnitte 3.1/3.2/2.4, falscher Familienstand, F33.4 bei BDI 22,
# F50.x als Nebendiagnose. Abschnitt 6 mit 26b statt 12b brachte keinen klaren Mehrwert (+80 s).


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
