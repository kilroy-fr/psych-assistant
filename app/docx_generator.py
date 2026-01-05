# app/docx_generator.py
"""DOCX-Generierung fuer Psychotherapie-Berichte."""

import io
import os
import re
import json
from docx import Document
from docx.shared import Pt, Cm
from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph

TEMPLATE_FILENAME = "report_template.docx"

# Lade Schema-Definitionen für Überschriften
def load_schema():
    """Lädt das Report-Schema für Überschriftenstruktur."""
    schema_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "data",
        "guidelines",
        "report_schema_vt_umwandlung.json"
    )
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None

# Erstelle Überschriften-Mapping aus Schema
def build_heading_map(schema):
    """Erstellt ein Mapping von Überschriften zu Hierarchieebenen.

    Returns:
        dict: {überschrift: level} wobei level = 2 (Hauptüberschrift), 3 (Unterüberschrift)
    """
    if not schema:
        return {}

    heading_map = {}
    for section in schema.get("sections", []):
        # Hauptüberschrift (Level 2 = h2)
        heading_map[section["title"]] = 2

        # Unterüberschriften (Level 3 = h3)
        for subsection in section.get("subsections", []):
            heading_map[subsection["title"]] = 3

    return heading_map

# Globales Schema und Heading-Map laden
SCHEMA = load_schema()
HEADING_MAP = build_heading_map(SCHEMA)

SENSITIVE_PATTERNS = [
    (re.compile(r"\[Anonymisiert\]"), "X."),
    (re.compile(r"\b(Frau|Herr)\s+[A-ZÄÖÜ][a-zäöüß-]+"), r"\1 X."),
    (re.compile(r"\b(Frau|Herr)\s+[A-ZÄÖÜ]\."), r"\1 X."),
    (re.compile(r"\b(Name|Vorname|Nachname|Geburtsname)\b\s*[:\-]\s*[^\n]+"), r"\1: X."),
    (re.compile(r"\b(Ort|Wohnort|Geburtsort|Adresse|Straße|Strasse|Stadt|PLZ)\b\s*[:\-]\s*[^\n]+"), r"\1: F."),
    (re.compile(r"\b[A-ZÄÖÜ]\.\s*[A-ZÄÖÜ]\.\b"), "X."),
]


# =============================================================================
# A) DETERMINISTISCHE FORMAT-SÄUBERUNG
# =============================================================================

def clean_output(text):
    """Entfernt Markdown-Artefakte und Listenoptik deterministisch.

    Diese Funktion wird vor sanitize_sensitive_text() ausgeführt und garantiert,
    dass kein Markdown mehr im Output verbleibt, unabhängig vom LLM-Output.

    Regel 1: Entferne Markdown-Überschriften am Zeilenanfang (^#{1,6}\s*)
    Regel 2: Entferne Bullet-Startzeichen am Zeilenanfang (^[-*•]\s+)
    Regel 3: Entferne Markdown-Formatierungen im Fließtext (**text**, *text*)
    Regel 4: Normalisiere Leerzeilen (max. eine aufeinanderfolgende Leerzeile)
    Regel 5: Entferne isolierte Markdown-Artefakte (einzelne #, ##, etc.)

    Args:
        text: Der zu säubernde Text

    Returns:
        str: Gesäuberter Text ohne Markdown und Listenoptik
    """
    if not text:
        return text

    lines = text.splitlines()
    cleaned_lines = []

    for line in lines:
        # Regel 1: Entferne Markdown-Überschriften am Zeilenanfang
        line = re.sub(r"^#{1,6}\s*", "", line)

        # Regel 2: Entferne Bullet-Startzeichen am Zeilenanfang
        line = re.sub(r"^[-*•]\s+", "", line)

        # Regel 3: Entferne Markdown-Formatierungen im Fließtext
        # **text** → text (Bold)
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
        # *text* → text (Italic) - aber nur wenn nicht Teil von **
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", line)

        # Regel 5: Entferne isolierte Markdown-Artefakte (nur #-Zeichen)
        if re.match(r"^#+$", line.strip()):
            continue  # Überspringe diese Zeile komplett

        cleaned_lines.append(line)

    # Regel 4: Normalisiere Leerzeilen (max. eine aufeinanderfolgende)
    result_lines = []
    prev_was_empty = False

    for line in cleaned_lines:
        is_empty = not line.strip()

        if is_empty:
            if not prev_was_empty:
                result_lines.append(line)
            prev_was_empty = True
        else:
            result_lines.append(line)
            prev_was_empty = False

    return "\n".join(result_lines)


# =============================================================================
# B) SCHEMA-VALIDATOR + REPAIR
# =============================================================================

def normalize_heading(text):
    """Normalisiert Überschriften für Vergleich (entfernt Nummerierung, Whitespace, Groß-/Kleinschreibung).

    Args:
        text: Der zu normalisierende Text

    Returns:
        str: Normalisierter Text (lowercase, keine Nummerierung, kein extra whitespace)
    """
    # Entferne Nummerierung am Anfang (z.B. "2.1 ", "4.2. ", "6.1 ", etc.)
    pattern = r"^(?:\d+\.?\d*\.?\s*)?(.+)$"
    match = re.match(pattern, text.strip())
    if match:
        text = match.group(1).strip()

    # Lowercase und normalisiere Whitespace (mehrfache Leerzeichen -> eins)
    return re.sub(r'\s+', ' ', text.lower().strip())


def build_heading_synonyms():
    """Erstellt ein Mapping von Synonymen/Varianten zu kanonischen Schema-Überschriften.

    Returns:
        dict: {variante_normalized: kanonische_überschrift}
    """
    synonyms = {}

    # Explizite Synonyme für bekannte Varianten
    known_variants = {
        "psychischer befund": "Psychopathologischer Befund",
        "psychopathologie": "Psychopathologischer Befund",
        "befund": "Psychopathologischer Befund",
        "somatischer befund / konsiliarbericht": "Somatischer Befund",
        "konsiliarbericht": "Somatischer Befund",
        "diagnose": "Diagnose nach ICD-10",
        "diagnosen": "Diagnose nach ICD-10",
        "icd-10": "Diagnose nach ICD-10",
        "therapieziele": "Therapieziele (mit der Patientin vereinbart)",
        "behandlungsplan": "Individueller Behandlungsplan",
    }

    # Füge bekannte Varianten hinzu
    for variant, canonical in known_variants.items():
        synonyms[normalize_heading(variant)] = canonical

    # Füge alle Schema-Überschriften als ihre eigenen kanonischen Versionen hinzu
    if SCHEMA:
        for section in SCHEMA.get("sections", []):
            normalized = normalize_heading(section["title"])
            synonyms[normalized] = section["title"]

            for subsection in section.get("subsections", []):
                normalized_sub = normalize_heading(subsection["title"])
                synonyms[normalized_sub] = subsection["title"]

    return synonyms


# Globales Synonym-Mapping
HEADING_SYNONYMS = build_heading_synonyms()


class ValidationResult:
    """Ergebnis der Schema-Validierung."""

    def __init__(self):
        self.is_valid = True
        self.errors = []
        self.warnings = []
        self.missing_sections = []
        self.missing_subsections = {}
        self.markdown_artifacts = []
        self.placeholder_locations = []

    def add_error(self, error):
        self.is_valid = False
        self.errors.append(error)

    def add_warning(self, warning):
        self.warnings.append(warning)


def validate_schema(text):
    """Validiert einen Text gegen das Report-Schema.

    Prüft:
    1. Sind alle 6 Hauptabschnitte vorhanden?
    2. Sind alle Pflicht-Unterüberschriften vorhanden?
    3. Enthält der Text noch Markdown-Artefakte?
    4. Enthält der Text Platzhalter wie [Angabe fehlt]?

    Args:
        text: Der zu validierende Text

    Returns:
        ValidationResult: Validierungsergebnis mit Fehlern und Warnungen
    """
    result = ValidationResult()

    if not text or not SCHEMA:
        result.add_error("Text oder Schema nicht verfügbar")
        return result

    lines = text.splitlines()
    normalized_lines = [normalize_heading(line) for line in lines]

    # Prüfung 1 & 2: Hauptabschnitte und Unterüberschriften
    for section in SCHEMA.get("sections", []):
        section_title = section["title"]
        section_normalized = normalize_heading(section_title)

        # Prüfe Hauptabschnitt
        if not any(section_normalized in norm_line for norm_line in normalized_lines):
            result.add_error(f"Hauptabschnitt fehlt: {section_title}")
            result.missing_sections.append(section_title)

        # Prüfe Unterüberschriften (nur wenn required=true)
        for subsection in section.get("subsections", []):
            if subsection.get("required", False):
                subsection_title = subsection["title"]
                subsection_normalized = normalize_heading(subsection_title)

                if not any(subsection_normalized in norm_line for norm_line in normalized_lines):
                    section_id = section["id"]
                    if section_id not in result.missing_subsections:
                        result.missing_subsections[section_id] = []
                    result.missing_subsections[section_id].append(subsection_title)
                    result.add_error(f"Unterüberschrift fehlt in Abschnitt {section_id}: {subsection_title}")

    # Prüfung 3: Markdown-Artefakte (sollte nach clean_output() keine mehr geben, aber double-check)
    for i, line in enumerate(lines, 1):
        # Markdown-Überschriften
        if re.match(r"^#{1,6}\s+", line):
            result.add_warning(f"Zeile {i}: Markdown-Überschrift gefunden: {line[:50]}")
            result.markdown_artifacts.append((i, line))

        # Bullet-Zeichen am Zeilenanfang
        if re.match(r"^[-*•]\s+", line):
            result.add_warning(f"Zeile {i}: Bullet-Zeichen gefunden: {line[:50]}")
            result.markdown_artifacts.append((i, line))

    # Prüfung 4: Platzhalter
    placeholder_pattern = re.compile(r"\[Angabe fehlt\]")
    for i, line in enumerate(lines, 1):
        if placeholder_pattern.search(line):
            result.add_warning(f"Zeile {i}: Platzhalter gefunden")
            result.placeholder_locations.append(i)

    return result


def repair_schema(text):
    """Repariert einen Text basierend auf Schema-Vorgaben.

    Automatische Korrekturen:
    1. Normalisiert Überschriften (Synonyme → kanonische Namen)
    2. Erzwingt Reihenfolge der Abschnitte gemäß Schema
    3. Fügt fehlende Abschnitte mit [Angabe fehlt] ein (außer Abschnitt 5)

    WICHTIG: Inhaltliche Ergänzungen werden NICHT vorgenommen.
    Fehlender Abschnitt 5 wird NICHT automatisch eingefügt (erfordert LLM-Run).

    Args:
        text: Der zu reparierende Text

    Returns:
        tuple: (reparierter_text, repair_log)
    """
    if not text or not SCHEMA:
        return text, []

    repair_log = []
    lines = text.splitlines()

    # Schritt 1: Normalisiere Überschriften (Synonyme → kanonische Namen)
    normalized_lines = []
    for line in lines:
        cleaned = line.strip()
        if not cleaned:
            normalized_lines.append(line)
            continue

        # Prüfe ob es eine Überschrift ist
        heading_level = detect_heading_level(cleaned)
        if heading_level:
            # Suche kanonische Version
            normalized_heading = normalize_heading(cleaned)
            if normalized_heading in HEADING_SYNONYMS:
                canonical = HEADING_SYNONYMS[normalized_heading]
                if canonical != cleaned:
                    repair_log.append(f"Überschrift normalisiert: '{cleaned}' → '{canonical}'")
                    normalized_lines.append(canonical)
                else:
                    normalized_lines.append(line)
            else:
                normalized_lines.append(line)
        else:
            normalized_lines.append(line)

    # Schritt 2: Extrahiere Abschnitte aus dem Text
    sections_found = {}
    current_section_id = None
    current_content = []

    for line in normalized_lines:
        cleaned = line.strip()

        # Prüfe ob es eine Hauptüberschrift ist (Level 2)
        if detect_heading_level(cleaned) == 2:
            # Speichere vorherigen Abschnitt
            if current_section_id:
                sections_found[current_section_id] = "\n".join(current_content)

            # Finde welchem Schema-Abschnitt diese Überschrift entspricht
            current_section_id = None
            for section in SCHEMA.get("sections", []):
                if normalize_heading(section["title"]) == normalize_heading(cleaned):
                    current_section_id = section["id"]
                    current_content = [line]
                    break

            if not current_section_id:
                # Unbekannte Überschrift - füge zu aktuellem Abschnitt hinzu
                if current_content:
                    current_content.append(line)
        else:
            current_content.append(line)

    # Speichere letzten Abschnitt
    if current_section_id:
        sections_found[current_section_id] = "\n".join(current_content)

    # Schritt 3: Baue Text in korrekter Schema-Reihenfolge neu auf
    reconstructed_lines = []

    for section in SCHEMA.get("sections", []):
        section_id = section["id"]

        if section_id in sections_found:
            # Abschnitt vorhanden - übernehme
            reconstructed_lines.append(sections_found[section_id])
        else:
            # Abschnitt fehlt
            if section_id == "5":
                # Abschnitt 5 (Diagnose) NICHT automatisch einfügen
                repair_log.append(f"WARNUNG: Abschnitt 5 (Diagnose) fehlt - erfordert LLM-Run")
            else:
                # Andere fehlende Abschnitte mit Platzhalter einfügen
                repair_log.append(f"Abschnitt {section_id} fehlt - Platzhalter eingefügt")
                reconstructed_lines.append(section["title"])
                reconstructed_lines.append("[Angabe fehlt]")

        # Füge Leerzeile zwischen Abschnitten hinzu
        if section_id != SCHEMA["sections"][-1]["id"]:
            reconstructed_lines.append("")

    return "\n".join(reconstructed_lines), repair_log


# =============================================================================
# C) HAUPT-POST-PROCESSING-PIPELINE
# =============================================================================

def post_process_text(text, enable_repair=True, enable_validation=True):
    """Führt vollständige Post-Processing-Pipeline aus.

    Pipeline-Schritte:
    1. Deterministische Format-Säuberung (clean_output)
    2. Schema-Reparatur (repair_schema) - optional (VORSICHT: kann Abschnitte reorganisieren!)
    3. Schema-Validierung (validate_schema) - optional
    4. Nummerierung hinzufügen (add_section_numbering) - immer
    5. Sensible Daten anonymisieren (sanitize_sensitive_text) - immer

    Args:
        text: Der zu verarbeitende Text
        enable_repair: Ob automatische Reparaturen durchgeführt werden sollen (Standard: True)
                       WARNUNG: repair_schema reorganisiert den gesamten Text!
        enable_validation: Ob Validierung durchgeführt werden soll (Standard: True)

    Returns:
        dict: {
            "text": verarbeiteter Text,
            "validation": ValidationResult (oder None),
            "repair_log": Liste der Reparatur-Aktionen,
            "cleaned": bool (ob Format-Säuberung durchgeführt wurde)
        }
    """
    result = {
        "text": text,
        "validation": None,
        "repair_log": [],
        "cleaned": False
    }

    if not text:
        return result

    # Schritt 1: Format-Säuberung (immer durchführen)
    cleaned_text = clean_output(text)
    result["cleaned"] = True
    result["text"] = cleaned_text

    # Schritt 2: Schema-Reparatur (optional, vor Validierung)
    if enable_repair:
        repaired_text, repair_log = repair_schema(cleaned_text)
        result["text"] = repaired_text
        result["repair_log"] = repair_log

    # Schritt 3: Schema-Validierung (optional)
    if enable_validation:
        validation_result = validate_schema(result["text"])
        result["validation"] = validation_result

    # Schritt 4: Nummerierung hinzufügen (immer durchführen)
    result["text"] = add_section_numbering(result["text"])

    # Schritt 5: Sensible Daten anonymisieren (immer durchführen)
    result["text"] = sanitize_sensitive_text(result["text"])

    return result


def add_section_numbering(text):
    """Fügt Nummerierung zu Überschriften hinzu basierend auf Schema.

    Hauptabschnitte: 1, 2, 3, 4, 5, 6
    Unterabschnitte: 1.1, 2.1, 2.2, etc.

    Args:
        text: Der Text mit Überschriften

    Returns:
        str: Text mit nummerierten Überschriften
    """
    if not text or not SCHEMA:
        return text

    lines = text.splitlines()
    result_lines = []

    # Tracking für aktuelle Abschnittsnummer
    current_main_section = None
    subsection_counters = {}  # {section_id: counter}

    for line in lines:
        cleaned = line.strip()
        if not cleaned:
            result_lines.append(line)
            continue

        # Prüfe ob es eine Überschrift ist
        heading_level = detect_heading_level(cleaned)

        if heading_level == 2:
            # Hauptüberschrift - finde Schema-Abschnitt
            for section in SCHEMA.get("sections", []):
                if normalize_heading(section["title"]) == normalize_heading(cleaned):
                    current_main_section = section["id"]
                    subsection_counters[current_main_section] = 0

                    # Entferne existierende Nummerierung
                    clean_title = re.sub(r"^(?:\d+\.?\d*\.?\s*)?(.+)$", r"\1", cleaned.strip())

                    # Füge neue Nummerierung hinzu
                    numbered_title = f"{section['id']}. {clean_title}"
                    result_lines.append(numbered_title)
                    break
            else:
                # Unbekannte Überschrift - unverändert lassen
                result_lines.append(line)

        elif heading_level == 3:
            # Unterüberschrift
            if current_main_section:
                subsection_counters[current_main_section] += 1
                counter = subsection_counters[current_main_section]

                # Entferne existierende Nummerierung
                clean_title = re.sub(r"^(?:\d+\.?\d*\.?\s*)?(.+)$", r"\1", cleaned.strip())

                # Füge neue Nummerierung hinzu
                numbered_title = f"{current_main_section}.{counter} {clean_title}"
                result_lines.append(numbered_title)
            else:
                # Keine aktive Hauptüberschrift - unverändert lassen
                result_lines.append(line)
        else:
            # Normaler Text oder unbekannte Überschrift
            result_lines.append(line)

    return "\n".join(result_lines)


def sanitize_sensitive_text(text):
    """Ersetzt Namen/Orte mit X. bzw. F., ohne [Anonymisiert] zu nutzen."""
    if not text:
        return text

    sanitized = text
    for pattern, repl in SENSITIVE_PATTERNS:
        sanitized = pattern.sub(repl, sanitized)
    return sanitized


def _insert_paragraph_after(paragraph, text, style=None):
    new_p = OxmlElement("w:p")
    paragraph._p.addnext(new_p)
    new_para = Paragraph(new_p, paragraph._parent)
    if text:
        new_para.add_run(text)
    if style:
        new_para.style = style
    return new_para


def detect_heading_level(text):
    """Erkennt die Überschriftenebene basierend auf Schema-Definitionen.

    Args:
        text: Der zu prüfende Text

    Returns:
        int oder None: 2 (h2/Heading 2), 3 (h3/Heading 3) oder None
    """
    cleaned = text.strip()
    normalized_input = normalize_heading(cleaned)

    # Exakte Übereinstimmung mit Schema (case-insensitive, whitespace-tolerant)
    for heading, level in HEADING_MAP.items():
        normalized_heading = normalize_heading(heading)

        # Exakter Match oder der Input enthält die Überschrift
        if normalized_input == normalized_heading or normalized_heading in normalized_input:
            return level

    return None


def _replace_placeholder_with_text(doc, placeholder, text):
    """Ersetzt Platzhalter mit formatiertem Text.

    Überschriften werden automatisch mit passenden Styles versehen:
    - Hauptüberschriften: Heading 2
    - Unterüberschriften: Heading 3
    - Sub-Unterüberschriften: Heading 4
    - Normaler Text: Normal
    """
    for paragraph in doc.paragraphs:
        if paragraph.text.strip() == placeholder:
            anchor = paragraph
            lines = [ln.rstrip() for ln in (text or "").splitlines()]
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()

            if not lines:
                _insert_paragraph_after(anchor, "[Angabe fehlt]")
            else:
                for line in lines:
                    cleaned = line.strip()
                    if not cleaned:
                        _insert_paragraph_after(anchor, "")
                        continue

                    # Erkenne Überschriftenebene
                    heading_level = detect_heading_level(cleaned)
                    if heading_level == 2:
                        style = "Heading 2"
                    elif heading_level == 3:
                        style = "Heading 3"
                    elif heading_level == 4:
                        style = "Heading 4"
                    else:
                        style = "Normal"

                    _insert_paragraph_after(anchor, cleaned, style=style)

            anchor._p.getparent().remove(anchor._p)
            return


def _remove_leading_heading(text, heading):
    if not text or not heading:
        return text

    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip() == heading:
        lines = lines[1:]
    return "\n".join(lines)


def ensure_report_template(template_path):
    """Erstellt das DOCX-Template, falls es nicht existiert."""
    if os.path.exists(template_path):
        return

    os.makedirs(os.path.dirname(template_path), exist_ok=True)
    doc = Document()

    doc.add_paragraph("Bericht an den Gutachter", style="Title")

    doc.add_paragraph("1. Relevante soziodemographische Daten", style="Heading 1")
    doc.add_paragraph("{{SECTION_1}}")

    doc.add_paragraph("2. Symptomatik und psychischer Befund", style="Heading 1")
    doc.add_paragraph("{{SECTION_2}}")

    doc.add_paragraph("3. Somatischer Befund / Konsiliarbericht", style="Heading 1")
    doc.add_paragraph("{{SECTION_3}}")

    doc.add_paragraph(
        "4. Behandlungsrelevante Angaben zur Lebensgeschichte, "
        "Krankheitsanamnese, funktionales Bedingungsmodell (VT)",
        style="Heading 1",
    )
    doc.add_paragraph("{{SECTION_4}}")

    doc.add_paragraph("5. Diagnose zum Zeitpunkt der Antragsstellung", style="Heading 1")
    doc.add_paragraph("{{SECTION_5}}")

    doc.add_paragraph("6. Behandlungsplan und Prognose", style="Heading 1")
    doc.add_paragraph("{{SECTION_6}}")

    for paragraph in doc.paragraphs:
        if paragraph.style.name == "Normal":
            for run in paragraph.runs:
                run.font.size = Pt(11)

    doc.save(template_path)


def create_comparison_docx(results, model_combinations, section_headers, parse_sections_func, enable_post_processing=True):
    """Erstellt eine DOCX-Datei mit Vergleichstabelle.

    Args:
        results: Liste mit Ergebnis-Strings (einer pro Modellkombination)
        model_combinations: Liste der Modellkombinationen [{"pass1": ..., "pass2": ...}, ...]
        section_headers: Liste der Abschnitts-Ueberschriften
        parse_sections_func: Funktion zum Parsen der Abschnitte aus dem Text
        enable_post_processing: Ob Post-Processing (Säuberung, Validierung, Repair) durchgeführt werden soll

    Returns:
        BytesIO mit dem DOCX-Dokument
    """
    doc = Document()

    num_combos = len(model_combinations)
    # Tabelle: Header + Abschnittszeilen, Spalten = 1 Ueberschrift + n Kombis + 1 Leer
    cols = num_combos + 2
    table = doc.add_table(rows=len(section_headers) + 1, cols=cols)
    table.style = "Table Grid"

    # Spaltenbreiten setzen (robust bei variabler Kombi-Anzahl)
    for row in table.rows:
        if cols > 0:
            row.cells[0].width = Cm(4)    # Ueberschriften
        for idx in range(1, cols - 1):
            row.cells[idx].width = Cm(4)  # Kombis
        row.cells[cols - 1].width = Cm(1)  # Leer

    # Header-Zeile (erste Zeile leer in Spalte 1)
    header_row = table.rows[0]
    header_row.cells[0].text = ""
    for idx, combo in enumerate(model_combinations, start=1):
        header_row.cells[idx].text = f"{combo['pass1']} + {combo['pass2']}"
    header_row.cells[cols - 1].text = ""

    # Extrahiere Abschnitte aus jedem Ergebnis
    parsed_results = [parse_sections_func(result) for result in results]

    # Fuelle die Tabelle (Zeilen 2..n)
    for row_idx in range(1, len(section_headers) + 1):
        section_idx = row_idx - 1
        row = table.rows[row_idx]

        # Spalte 1: Ueberschrift
        row.cells[0].text = section_headers[section_idx]

        # Spalten 2..(n+1): Ergebnisse (eine pro Kombination)
        for combo_idx in range(num_combos):
            cell_text = parsed_results[combo_idx][section_idx]

            # Post-Processing-Pipeline anwenden (falls aktiviert)
            if enable_post_processing:
                pp_result = post_process_text(
                    cell_text,
                    enable_repair=True,
                    enable_validation=False
                )
                cell_text = pp_result["text"]
            else:
                # Legacy-Pfad ohne Post-Processing
                cell_text = sanitize_sensitive_text(cell_text)

            row.cells[combo_idx + 1].text = cell_text

        # Letzte Spalte: leer
        row.cells[cols - 1].text = ""

    # Formatierung
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.space_after = Pt(0)
                for run in paragraph.runs:
                    run.font.size = Pt(9)

    # In BytesIO speichern
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output


def format_text_as_html(text):
    """Formatiert Text mit HTML-Tags für Überschriften (h2-h4).

    Args:
        text: Der zu formatierende Text

    Returns:
        str: HTML-formatierter Text
    """
    if not text:
        return ""

    lines = text.splitlines()
    html_lines = []

    for line in lines:
        cleaned = line.strip()
        if not cleaned:
            html_lines.append("<p></p>")
            continue

        # Erkenne Überschriftenebene
        heading_level = detect_heading_level(cleaned)
        if heading_level == 2:
            html_lines.append(f"<h2>{cleaned}</h2>")
        elif heading_level == 3:
            html_lines.append(f"<h3>{cleaned}</h3>")
        elif heading_level == 4:
            html_lines.append(f"<h4>{cleaned}</h4>")
        else:
            html_lines.append(f"<p>{cleaned}</p>")

    return "\n".join(html_lines)


def create_flowing_text_docx(sections, selected_texts, enable_post_processing=True):
    """Erstellt eine DOCX-Datei mit Fliesstext aus ausgewaehlten Abschnitten.

    Args:
        sections: Liste der Abschnitts-Ueberschriften
        selected_texts: Liste der ausgewaehlten Texte (einer pro Abschnitt)
        enable_post_processing: Ob Post-Processing (Säuberung, Validierung, Repair) durchgeführt werden soll

    Returns:
        BytesIO mit dem DOCX-Dokument
    """
    template_path = os.path.join(os.path.dirname(__file__), "templates", TEMPLATE_FILENAME)
    ensure_report_template(template_path)
    doc = Document(template_path)

    for idx, section_text in enumerate(selected_texts, 1):
        placeholder = f"{{{{SECTION_{idx}}}}}"
        heading = sections[idx - 1] if idx - 1 < len(sections) else None

        # Post-Processing-Pipeline anwenden (falls aktiviert)
        if enable_post_processing:
            pp_result = post_process_text(
                section_text,
                enable_repair=True,
                enable_validation=False  # Validierung nur auf Gesamtdokument, nicht einzelne Abschnitte
            )
            section_text = pp_result["text"]

            # Log repair actions (falls vorhanden)
            if pp_result["repair_log"]:
                print(f"[Post-Processing] Abschnitt {idx}: {len(pp_result['repair_log'])} Reparaturen")
                for log_entry in pp_result["repair_log"]:
                    print(f"  - {log_entry}")
        else:
            # Legacy-Pfad ohne Post-Processing
            section_text = sanitize_sensitive_text(section_text)

        section_text = _remove_leading_heading(section_text, heading)
        _replace_placeholder_with_text(doc, placeholder, section_text)

    # In BytesIO speichern
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output
