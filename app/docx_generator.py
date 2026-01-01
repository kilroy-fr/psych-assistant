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
        dict: {überschrift: level} wobei level = 2 (Hauptüberschrift), 3 (Unterüberschrift), 4 (Sub-Unterüberschrift)
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

SUBHEADINGS = {
    "Symptomatik",
    "Psychischer Befund",
    "Krankheitsverständnis der Patientin",
    "Ergebnisse psychodiagnostischer Testverfahren",
    "Aktuelle psychopharmakologische Medikation",
    "Psychotherapeutische, psychosomatische oder psychiatrische Vorbehandlungen",
    "Funktionales Modell:",
    "Differenzialdiagnostische Angaben",
    "Therapieziele  (mit der Patientin vereinbart)",
    "Individueller Behandlungsplan",
    "Begründung des Settings, Sitzungszahl, Frequenz",
    "Prognose",
}

SENSITIVE_PATTERNS = [
    (re.compile(r"\[Anonymisiert\]"), "X."),
    (re.compile(r"\b(Frau|Herr)\s+[A-ZÄÖÜ][a-zäöüß-]+"), r"\1 X."),
    (re.compile(r"\b(Frau|Herr)\s+[A-ZÄÖÜ]\."), r"\1 X."),
    (re.compile(r"\b(Name|Vorname|Nachname|Geburtsname)\b\s*[:\-]\s*[^\n]+"), r"\1: X."),
    (re.compile(r"\b(Ort|Wohnort|Geburtsort|Adresse|Straße|Strasse|Stadt|PLZ)\b\s*[:\-]\s*[^\n]+"), r"\1: F."),
    (re.compile(r"\b[A-ZÄÖÜ]\.\s*[A-ZÄÖÜ]\.\b"), "X."),
    (re.compile(r"\b(in|aus|bei|nach|von|im|am)\s+[A-ZÄÖÜ][a-zäöüß-]+"), r"\1 F."),
]


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
        int oder None: 2 (h2/Heading 2), 3 (h3/Heading 3), 4 (h4/Heading 4) oder None
    """
    cleaned = text.strip()

    # Exakte Übereinstimmung mit Schema
    if cleaned in HEADING_MAP:
        return HEADING_MAP[cleaned]

    # Fuzzy Matching: Prüfe ob Text eine Überschrift enthält (tolerant gegenüber Präfixen wie "2.1", etc.)
    for heading, level in HEADING_MAP.items():
        # Entferne Nummerierung am Anfang (z.B. "2.1 ", "4.2. ", etc.)
        pattern = r"^(?:\d+\.?\d*\.?\s*)?(.+)$"
        match = re.match(pattern, cleaned)
        if match:
            text_without_number = match.group(1).strip()
            if text_without_number == heading or heading in text_without_number:
                return level

    # Fallback: Alte Logik für Subheadings
    if cleaned in SUBHEADINGS:
        return 3

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


def create_comparison_docx(results, model_combinations, section_headers, parse_sections_func):
    """Erstellt eine DOCX-Datei mit Vergleichstabelle.

    Args:
        results: Liste mit Ergebnis-Strings (einer pro Modellkombination)
        model_combinations: Liste der Modellkombinationen [{"pass1": ..., "pass2": ...}, ...]
        section_headers: Liste der Abschnitts-Ueberschriften
        parse_sections_func: Funktion zum Parsen der Abschnitte aus dem Text

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
            row.cells[combo_idx + 1].text = sanitize_sensitive_text(cell_text)

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


def create_flowing_text_docx(sections, selected_texts):
    """Erstellt eine DOCX-Datei mit Fliesstext aus ausgewaehlten Abschnitten.

    Args:
        sections: Liste der Abschnitts-Ueberschriften
        selected_texts: Liste der ausgewaehlten Texte (einer pro Abschnitt)

    Returns:
        BytesIO mit dem DOCX-Dokument
    """
    template_path = os.path.join(os.path.dirname(__file__), "templates", TEMPLATE_FILENAME)
    ensure_report_template(template_path)
    doc = Document(template_path)

    for idx, section_text in enumerate(selected_texts, 1):
        placeholder = f"{{{{SECTION_{idx}}}}}"
        heading = sections[idx - 1] if idx - 1 < len(sections) else None
        section_text = sanitize_sensitive_text(section_text)
        section_text = _remove_leading_heading(section_text, heading)
        _replace_placeholder_with_text(doc, placeholder, section_text)

    # In BytesIO speichern
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output
