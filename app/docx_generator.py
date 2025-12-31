# app/docx_generator.py
"""DOCX-Generierung fuer Psychotherapie-Berichte."""

import io
import os
import re
from docx import Document
from docx.shared import Pt, Cm
from docx.oxml import OxmlElement
from docx.text.paragraph import Paragraph

TEMPLATE_FILENAME = "report_template.docx"

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


def _replace_placeholder_with_text(doc, placeholder, text):
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
                    style = "Heading 2" if cleaned in SUBHEADINGS else "Normal"
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
        results: Liste mit 4 Ergebnis-Strings (einer pro Modellkombination)
        model_combinations: Liste der Modellkombinationen [{"pass1": ..., "pass2": ...}, ...]
        section_headers: Liste der Abschnitts-Ueberschriften
        parse_sections_func: Funktion zum Parsen der Abschnitte aus dem Text

    Returns:
        BytesIO mit dem DOCX-Dokument
    """
    doc = Document()

    # Tabelle: Header + Abschnittszeilen, 6 Spalten (1 Ueberschrift + 4 Kombis + 1 Leer)
    table = doc.add_table(rows=len(section_headers) + 1, cols=6)
    table.style = "Table Grid"

    # Spaltenbreiten setzen
    for row in table.rows:
        row.cells[0].width = Cm(4)    # Ueberschriften
        row.cells[1].width = Cm(4)    # Kombi 1
        row.cells[2].width = Cm(4)    # Kombi 2
        row.cells[3].width = Cm(4)    # Kombi 3
        row.cells[4].width = Cm(4)    # Kombi 4
        row.cells[5].width = Cm(1)    # Leer

    # Header-Zeile (erste Zeile leer in Spalte 1)
    header_row = table.rows[0]
    header_row.cells[0].text = ""
    header_row.cells[1].text = f"{model_combinations[0]['pass1']} + {model_combinations[0]['pass2']}"
    header_row.cells[2].text = f"{model_combinations[1]['pass1']} + {model_combinations[1]['pass2']}"
    header_row.cells[3].text = f"{model_combinations[2]['pass1']} + {model_combinations[2]['pass2']}"
    header_row.cells[4].text = f"{model_combinations[3]['pass1']} + {model_combinations[3]['pass2']}"
    header_row.cells[5].text = ""

    # Extrahiere Abschnitte aus jedem Ergebnis
    parsed_results = [parse_sections_func(result) for result in results]

    # Fuelle die Tabelle (Zeilen 2..n)
    for row_idx in range(1, len(section_headers) + 1):
        section_idx = row_idx - 1
        row = table.rows[row_idx]

        # Spalte 1: Ueberschrift
        row.cells[0].text = section_headers[section_idx]

        # Spalten 2-5: Ergebnisse (4 Kombinationen)
        for combo_idx in range(4):
            cell_text = parsed_results[combo_idx][section_idx]
            row.cells[combo_idx + 1].text = sanitize_sensitive_text(cell_text)

        # Spalte 6: leer
        row.cells[5].text = ""

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
