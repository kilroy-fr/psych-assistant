# app/report_checks.py
"""Deterministische Pruefungen und Datenextraktion aus der Akte ("Absicherungen im Code").

Manche Regeln befolgt das Modell trotz Prompt nicht zuverlaessig. Sie werden deshalb
hier im Code geprueft/erzwungen statt nur im Prompt verlangt: ICD-Diagnoseregeln,
Vollstaendigkeit der Abschnitte 1-3, BDI-II-Werte, Medikamentendosis und
Kalenderwochen-Termine. Siehe CLAUDE.md ("Absicherungen im Code") fuer die
Lauf-Historie hinter jeder einzelnen Regel.
"""
import re
from datetime import date, timedelta

from app.logging_setup import debug_logger

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
    "F84.0": "Frühkindlicher Autismus",
    "F84.1": "Atypischer Autismus",
    "F84.5": "Asperger-Syndrom",
    "F84.9": "Tiefgreifende Entwicklungsstörung, nicht näher bezeichnet",
    # Dreistellige Codes (in Differenzialdiagnosen oft ohne Subtyp)
    "F32": "Depressive Episode",
    "F33": "Rezidivierende depressive Störung",
    "F40": "Phobische Störungen",
    "F41": "Andere Angststörungen",
    "F42": "Zwangsstörung",
    "F43": "Reaktionen auf schwere Belastungen und Anpassungsstörungen",
    "F50": "Essstörungen",
    "F84": "Tiefgreifende Entwicklungsstörungen",
    "F90": "Hyperkinetische Störungen",
}


def norm_title(s):
    """Klammerzusaetze, Satzzeichen und Gross-/Kleinschreibung fuer den Vergleich entfernen."""
    s = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", s.lower())
    return " ".join(re.sub(r"[^\w\s]", " ", s).split())


def _title_overlap(official, label):
    """Anteil der Woerter der offiziellen Bezeichnung, die (ueber die ersten 6 Buchstaben)
    auch im Klartext stehen. "aktuell in einer remittierten Episode" trifft "remittiert"."""
    want = {w[:6] for w in norm_title(official).split()}
    have = {w[:6] for w in norm_title(label).split()}
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
        if not official or norm_title(official) in norm_title(label):
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

# Die Akutdiagnosen-Liste der Akte markiert jeden Code mit "{F84.0 V}" (Verdacht)
# oder "{F33.1 G}" (gesichert). Codes, die dort NIE als G stehen, sind im Bericht
# keine gesicherte Diagnose - unabhaengig davon, wie sicher das Modell klingt.
_AKTE_DIAGNOSIS_MARK_RE = re.compile(r"\{(F\d{2}(?:\.\d{1,2})?)\s+([VG])\}")
_CERTAINTY_RE = re.compile(r"\bgesichert\w*|\bbestätigt\w*", re.IGNORECASE)
_NEGATION_RE = re.compile(r"\bnicht\b", re.IGNORECASE)


def _suspected_only_codes(source_text):
    """Codes, die in der Akte durchgaengig nur als Verdachtsdiagnose (V) markiert sind."""
    seen = {}
    for code, status in _AKTE_DIAGNOSIS_MARK_RE.findall(source_text):
        seen.setdefault(code.upper(), set()).add(status)
    return {code for code, stati in seen.items() if stati == {"V"}}


def check_diagnosis_certainty(text, source_text):
    """Warnt, wenn "gesichert"/"bestaetigt" in der Naehe der Bezeichnung einer Diagnose
    steht, die die Akte durchgaengig nur als Verdacht (V) fuehrt. Anwendbar auf jeden
    Abschnitt, nicht nur Abschnitt 5 - Lauf 12: Kombi 2 schrieb "eine gesicherte
    Diagnose im Autismus-Spektrum" in Abschnitt 4, obwohl die Akte Autismus/Asperger
    nirgends als "G" (gesichert) markiert.
    Lauf 13: "nicht gesichert"/"nicht eindeutig gesichert" wurden faelschlich mit
    angemeckert, obwohl das korrekt gehedgt ist - "nicht" im kurzen Vorlauf vor dem
    Treffer schliesst den Hinweis deshalb aus."""
    suspected = _suspected_only_codes(source_text)
    if not suspected:
        return []
    label_words = {w[:6] for code in suspected for w in norm_title(ICD10_TITLES.get(code, "")).split()
                   if len(w) >= 4}
    if not label_words:
        return []
    hints = []
    for m in _CERTAINTY_RE.finditer(text):
        if _NEGATION_RE.search(text[max(0, m.start() - 30):m.start()]):
            continue
        window = norm_title(text[max(0, m.start() - 60):m.end() + 60])
        if any(w in window for w in label_words):
            hints.append(f"\"{m.group(0)}\" steht nahe einer Diagnose, die die Akte nur als "
                         "Verdacht führt — prüfen")
    return hints


def check_diagnosis_placement(text, source_text):
    """Warnt, wenn ein Code unter Haupt-/Nebendiagnose(n) steht, den die Akte
    durchgaengig nur als Verdachtsdiagnose (V) fuehrt - nur fuer Abschnitt 5.
    Lauf 12: Kombi 2 fuehrte F84.0 unter Nebendiagnose(n) statt unter
    Differenzialdiagnose(n)."""
    suspected = _suspected_only_codes(source_text)
    if not suspected:
        return []
    dd_match = re.search(r"Differen[zt]ialdiagnose", text, re.IGNORECASE)
    coded_part = text[:dd_match.start()] if dd_match else text
    hints = []
    for code in dict.fromkeys(c[:3].upper() + c[3:].lower() for c in _ICD_CODE_RE.findall(coded_part)):
        if code in suspected:
            hints.append((code, f"{code} steht unter Haupt-/Nebendiagnose(n), die Akte führt es aber "
                         "durchgängig nur als Verdachtsdiagnose (V) — gehört das nicht eher unter "
                         "Differenzialdiagnose(n)?"))
    return hints


def add_diagnosis_hints(text, latest_bdi=None, source_text=""):
    """Haengt an Abschnitt 5 Pruefhinweise fuer formale Diagnosefehler an.

    Die Regeln stehen auch in prompt5-1/5-2, werden vom Modell aber nicht
    zuverlaessig befolgt. Diagnosen werden nicht veraendert, nur markiert --
    ausser rein umformulierte ICD-Bezeichnungen (fix_icd_titles).
    latest_bdi: juengster Eintrag aus extract_bdi_values() fuer den Abgleich
    Schweregrad <-> Testwert.
    source_text: Akte fuer check_diagnosis_placement/check_diagnosis_certainty.
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
    if source_text:
        hints += check_diagnosis_placement(text, source_text)

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


# Nummerierte Ueberschriftszeile ("2.4 Krankheitsverstaendnis der Patientin"): kurz, ohne Satzende
_PASS1_HEADING_RE = re.compile(r"^[ \t]*\d(?:\.\d)?\.?[ \t]+([^\n]{3,100}?)[ \t]*$", re.MULTILINE)


def strip_pass1_heading_numbers(text):
    """Entfernt die Nummern aus Ueberschriften im Pass-1-Text fuer 1-3, bevor er an Pass 2 geht.

    gemma4:26b gliedert Pass 1 trotz "Reiner Fliesstext" schon wie den fertigen Bericht
    ("1.1 …", "2.4 …", "3.3 …"). qwen3:14b kopiert diese Vorlage dann bis 2.3 fast
    woertlich und hoert auf -- reproduzierbar bei jeder Temperatur, obwohl 2.4-3.3 im
    Pass-1-Text vollstaendig stehen. Ohne Nummern ("Krankheitsverstaendnis der
    Patientin:") schreibt Pass 2 alle Abschnitte (Test September 2026, je 2 Laeufe).
    """
    return _PASS1_HEADING_RE.sub(
        lambda m: m.group(0) if m.group(1).endswith((".", ";", ",")) else m.group(1).rstrip(":") + ":",
        text,
    )


_BEFUND_TERMS = ["Bewusstsein", "Orientierung", "Kognition", "Denken formal und inhaltlich", "Ängste",
                 "Wahrnehmung", "Ich-Störungen", "Affekt", "Antrieb", "zirkadiane Besonderheiten",
                 "Suizidalität"]


def norm_label(s):
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
    labels = [norm_label(l) for l, _ in items]
    terms = [norm_label(t) for t in _BEFUND_TERMS]
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
            if norm_label(label) != norm_label(term) and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", value, re.IGNORECASE):
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
    r"Trauma\w*|traumati\w*|Missbrauch\w*|Gewalt\w*|Sucht\w*|Suizid\w*|Zwang\w*|Autismus\w*|"
    r"Asperger\w*|Symmetrie\w*)", re.IGNORECASE)


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


def section13_hints(text, source_text="", parse_sections=None):
    """Pruefhinweise je Abschnitt (1, 2, 3) fuer die fertige 1-3-Ausgabe.

    parse_sections: app.py's parse_sections(), fuer check_family_status auf
    den bereits geparsten Abschnitt 1 (vermeidet einen Zirkelimport).
    """
    by_section = {0: [], 1: [], 2: []}
    missing = missing_subsections_13(text)
    for n in missing:
        by_section[int(n[0]) - 1].append(n)
    hints = {i: ([f"Abschnitt {', '.join(ns)} fehlt in der Modellausgabe — manuell ergänzen"] if ns else [])
             for i, ns in by_section.items()}
    if source_text:
        if parse_sections:
            hints[0] += check_family_status(parse_sections(text)[0], source_text)
        hints[2] += check_medication_currency(text, source_text)
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
# BD[IT] statt nur BDI: Lauf 13 schrieb "BDT-II" (Tippfehler) fuer ein Datum, das
# schon einen korrekten BDI-Eintrag hatte - das Duplikat wurde nicht erkannt und
# landete unter "andere Testverfahren".
_BDI_TYPO_RE = re.compile(r"\bBD[IT]\b", re.IGNORECASE)


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
        is_bdi = _BDI_TYPO_RE.search(seg) or (
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


def append_hints(text, hints):
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


def full_year(y):
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
            entry = (int(m.group(1)), int(m.group(2)), full_year(m.group(3)))
        matches = list(_BDI_RE.finditer(line))
        for i, bdi in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            segment = line[bdi.end():min(end, bdi.end() + 200)]
            # Ein Satz kann mehrere Datum/Punkte-Paare hinter einem einzigen "BDI"
            # nennen ("vom 23.2. mit 6 Punkten ..., vom 15.2. mit 24 Punkten ...") -
            # jeden Score-Treffer einzeln mit dem davor/danach stehenden Text auswerten,
            # nicht nur den ersten im Segment.
            scores = list(_SCORE_RE.finditer(segment))
            for j, score in enumerate(scores):
                before_start = scores[j - 1].end() if j > 0 else 0
                before = segment[before_start:score.start()]
                day = month = year = None
                if (d := _NUM_DATE_RE.search(before)):
                    day, month = int(d.group(1)), int(d.group(2))
                    year = full_year(d.group(3)) if d.group(3) else None
                elif (d := _DAY_MONTH_RE.search(before)):
                    day, month = int(d.group(1)), _MONTHS[d.group(2).lower()]
                    year = int(d.group(3)) if d.group(3) else None
                elif (d := _MONTH_RE.search(before)):
                    month = _MONTHS[d.group(1).lower()]
                    year = int(d.group(2)) if d.group(2) else None
                elif entry and j == 0:
                    day, month, year = entry
                if month is None or not 1 <= month <= 12:
                    continue
                if year is None:
                    year = _infer_year(month, day, entry)

                after_end = scores[j + 1].start() if j + 1 < len(scores) else len(segment)
                # Die Akte klammert die Einordnung teils schon selbst ein
                # ("32 Punkte (schwere depressive Episode)") - Klammern mit ausstreifen,
                # sonst verdoppelt _bdi_line() sie oder haengt eine verwaiste ")" an.
                interpretation = re.split(r"[,;]", segment[score.end():after_end],
                                           maxsplit=1)[0].strip(" .()")
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


# --- Medikamentendosis deterministisch aus der Akte lesen --------------------
# Lauf 11-13: Abschnitt 3.2 nannte wiederholt eine veraltete Dosis ohne Datumsbezug
# (z.B. "Venlafaxin 150 mg" statt der zuletzt dokumentierten 225 mg vom 23.10.2025).
# Anders als bei den BDI-Werten wird hier NICHT ersetzt (3.2 ist Fliesstext mit
# Historie, kein starres Listenformat) - nur geprueft und als Pruefhinweis markiert.

# Kuratierte, keine vollstaendige Liste - wie ICD10_TITLES bei Bedarf erweiterbar.
_MEDICATIONS = {
    "Venlafaxin": ("Venlafaxin",),
    "Escitalopram": ("Escitalopram", "Escit"),
    "Trimipramin": ("Trimipramin", "Trimi"),
}

_DOSE_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(mg|mcg|µg|tropfen|gtt)\b", re.IGNORECASE)
_MED_UNIT_NAMES = {"mg": "mg", "mcg": "mcg", "µg": "µg", "tropfen": "Tropfen", "gtt": "Tropfen"}
_STAND_RE = re.compile(r"\bStand\b", re.IGNORECASE)
_MED_NAME_RES = {drug: re.compile(r"\b(?:" + "|".join(re.escape(a) for a in aliases) + r")\b", re.IGNORECASE)
                 for drug, aliases in _MEDICATIONS.items()}


def _drug_name_matches(text):
    matches = [(m.start(), m.end(), drug) for drug, rx in _MED_NAME_RES.items() for m in rx.finditer(text)]
    matches.sort()
    return matches


def _doses_per_drug(text, max_dist=60):
    """[(Wirkstoff, Wert, Einheit)] je Wirkstoff-Erwaehnung in `text`. Sucht zuerst
    vorwaerts bis zur naechsten Wirkstoff-Erwaehnung (deckt "Venlafaxin in einer
    Dosierung von 150 mg sowie Escitalopram ..." ab, wo reine Zeichen-Distanz die
    Dosis dem falschen, naeheren Nachbarn zuordnen wuerde), sonst rueckwaerts bis zur
    vorherigen Erwaehnung (deckt "225 mg Venlafaxin" ab). Eine Dosis wird nie ueber
    eine andere Wirkstoff-Erwaehnung hinweg zugeordnet."""
    drug_matches = _drug_name_matches(text)
    results = []
    for i, (start, end, drug) in enumerate(drug_matches):
        next_start = drug_matches[i + 1][0] if i + 1 < len(drug_matches) else len(text)
        prev_end = drug_matches[i - 1][1] if i > 0 else 0
        dose_m = _DOSE_RE.search(text[end:min(next_start, end + max_dist)])
        if not dose_m:
            back = list(_DOSE_RE.finditer(text[max(prev_end, start - max_dist):start]))
            dose_m = back[-1] if back else None
        if dose_m:
            value = float(dose_m.group(1).replace(",", "."))
            unit = _MED_UNIT_NAMES[dose_m.group(2).lower()]
            results.append((drug, value, unit))
    return results


def extract_medication_doses(text):
    """Liest je bekanntem Wirkstoff die zuletzt dokumentierte Dosis aus Freitext-
    Notizen, analog zu extract_bdi_values()."""
    latest = {}
    entry = None
    for line in text.splitlines():
        m = _ENTRY_DATE_RE.match(line)
        if m:
            entry = (int(m.group(1)), int(m.group(2)), full_year(m.group(3)))
        if not entry:
            continue
        for drug, value, unit in _doses_per_drug(line):
            day, month, year = entry
            key = (year, month, day)
            if drug not in latest or key >= latest[drug]["_key"]:
                latest[drug] = {"drug": drug, "day": day, "month": month, "year": year,
                                 "value": value, "unit": unit, "_key": key}
    return {drug: {k: v for k, v in info.items() if k != "_key"} for drug, info in latest.items()}


def check_medication_currency(text, source_text):
    """Warnt, wenn Abschnitt 3.2 eine Dosis nennt, die von der zuletzt in der Akte
    dokumentierten Dosis desselben Wirkstoffs abweicht, ohne durch ein "Stand:"-Datum
    als historischer Wert gekennzeichnet zu sein."""
    latest = extract_medication_doses(source_text)
    if not latest:
        return []
    span = _subsection_span(text, "3.2", r"3\.3")
    if not span:
        return []
    body = text[span[0]:span[1]]
    hints = []
    for drug, value, unit in _doses_per_drug(body):
        info = latest.get(drug)
        if not info or unit != info["unit"] or value == info["value"]:
            continue
        name_m = _MED_NAME_RES[drug].search(body)
        if name_m and _STAND_RE.search(body[max(0, name_m.start() - 40):name_m.end() + 40]):
            continue
        hints.append(f"{drug}: Bericht nennt {value:g} {unit}, zuletzt dokumentiert in der Akte "
                     f"sind {info['value']:g} {info['unit']} ({_bdi_when(info)}) — Dosis prüfen")
    return hints


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
                entry = date(full_year(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                entry = None
        for kw in _KW_RE.finditer(line):
            week = int(kw.group(1))
            try:
                if kw.group(2):
                    year = full_year(kw.group(2))
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
        norm = norm_label(r["snippet"])
        if not any(u["monday"] == r["monday"] and (norm.endswith(norm_label(u["snippet"]))
                                                   or norm_label(u["snippet"]).endswith(norm))
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
