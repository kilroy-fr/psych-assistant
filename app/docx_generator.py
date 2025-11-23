# app/docx_generator.py
"""DOCX-Generierung für Psychotherapie-Berichte."""

import io
from docx import Document
from docx.shared import Pt, Cm


def create_comparison_docx(results, model_combinations, section_headers, parse_sections_func):
    """Erstellt eine DOCX-Datei mit Vergleichstabelle.

    Args:
        results: Liste mit 4 Ergebnis-Strings (einer pro Modellkombination)
        model_combinations: Liste der Modellkombinationen [{"pass1": ..., "pass2": ...}, ...]
        section_headers: Liste der Abschnitts-Überschriften
        parse_sections_func: Funktion zum Parsen der Abschnitte aus dem Text

    Returns:
        BytesIO mit dem DOCX-Dokument
    """
    doc = Document()

    # Tabelle: 8 Zeilen x 6 Spalten (1 Ueberschrift + 4 Kombis + 1 Leer)
    table = doc.add_table(rows=8, cols=6)
    table.style = 'Table Grid'

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

    # Fuelle die Tabelle (Zeilen 2-8 = Index 1-7)
    for row_idx in range(1, 8):
        section_idx = row_idx - 1  # 0-6
        row = table.rows[row_idx]

        # Spalte 1: Ueberschrift (section_headers hat Index 0-6)
        row.cells[0].text = section_headers[section_idx]

        # Spalten 2-5: Ergebnisse (4 Kombinationen)
        for combo_idx in range(4):
            row.cells[combo_idx + 1].text = parsed_results[combo_idx][section_idx]

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
        sections: Liste der Abschnitts-Überschriften
        selected_texts: Liste der ausgewählten Texte (einer pro Abschnitt)

    Returns:
        BytesIO mit dem DOCX-Dokument
    """
    doc = Document()

    for i, section_header in enumerate(sections):
        # Ueberschrift hinzufuegen
        doc.add_heading(section_header, level=2)

        # Ausgewaehlten Text hinzufuegen
        if i < len(selected_texts) and selected_texts[i]:
            doc.add_paragraph(selected_texts[i])
        else:
            doc.add_paragraph("")

        # Abstand nach jedem Abschnitt
        doc.add_paragraph("")

    # In BytesIO speichern
    output = io.BytesIO()
    doc.save(output)
    output.seek(0)
    return output
