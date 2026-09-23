# app/anonymization.py
"""Anonymisierung: Patientin/Patient deterministisch, Dritte per Namenpruefung + Modell.

Die Pass-2-Prompts verlangen Rollen statt Namen, das Modell haelt sich aber
nicht zuverlaessig daran. Namen AUFZULISTEN klappt dagegen gut (find_person_names).
"""
import re
from difflib import SequenceMatcher

from app.logging_setup import debug_logger, LOG_PATIENT_CONTENT
from app.rag.query_engine import answer_question
from app.report_checks import append_hints, full_year

# --- Namenpruefung -----------------------------------------------------------

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
    import json
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
    return {"last": m.group(1), "first": m.group(2), "dob": (int(d), int(mo), full_year(y))}


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
        result.append(append_hints(section, notes))
    return result
