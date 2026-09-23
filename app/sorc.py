# app/sorc.py
"""SORC-Konsequenzen aus zerlegten Angaben.

Kein Modell ordnete C+/C-/C+//C-/ zuverlaessig zu (auch gemma4:26b nicht).
Pass 1 beantwortet deshalb je Konsequenz nur "tritt ein / faellt weg" und
"angenehm / unangenehm" (K-Zeilen, prompt4-1.txt); die Zuordnung macht der Code.
"""
import re
from collections import Counter
from difflib import SequenceMatcher

from app.report_checks import norm_label

_K_LINE_RE = re.compile(r"^\s*[-*]?\s*K\s*:\s*(.+)$")
_C_LINE_RE = re.compile(r"^\s*C\s*[+-]\s*/?\s*:")
# S-O-R-C-Feld mitten in einer Zeile (nach Satzende oder Semikolon): ". O: Genetik", ". C+: ..."
_INLINE_FIELD_RE = re.compile(r"([.;]\s*)((?:O|R|C\s*[+-]\s*/?)\s*:\s)")
_KONSEQUENZEN_HEADER_RE =re.compile(r"^\s*KONSEQUENZEN\s*:?\s*$", re.IGNORECASE)
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
# Unangenehmes, dessen "Reduktion/Vermeidung" das Modell als wegfallendes Angenehmes einträgt
_AVERSIVE_RE = re.compile(
    r"druck|reiz|stress|konflikt|angst|ängst|anspannung|kritik|scham|schuld|überforder|enttäusch|"
    r"ablehnung|belastung|streit|konfrontation|unsicherheit", re.IGNORECASE)
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
        a, b = set(norm_label(intern.group(1)).split()), set(norm_label(rule.group(1)).split())
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
        elif (avoided and gone and pleasant and _AVERSIVE_RE.search(avoided.group(1))
              and not short.lower().startswith("wegfall")):
            # "Reduktion von sozialem Druck | fällt weg | angenehm" (Laeufe 15, 19, 20) meint:
            # der Druck faellt weg -> C-/. "Wegfall von Kontakt" bleibt C+/.
            reinterpreted += 1
            short, gone, pleasant = avoided.group(1).strip(), True, False
        label = next(lb for lb, g, p in _C_TYPES if g == gone and p == pleasant)
        # Angenehmer Zustand unter C-/C-/ ("Funktionieren -> Wegfall von Unangenehmem
        # (Stabilitaet)", Laeufe 8-10): gemeint ist, dass er eintritt bzw. erhalten bleibt -> C+
        if (label in ("C-", "C-/") and _PLEASANT_RE.search(short)
                and not _NEGATED_RE.search(short) and not _RELIEF_RE.search(short)):
            label = "C+"
            moved_to_plus += 1

        key = tuple(norm_label(s) for s in (behavior, short))
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
    # Gleiche Hinweise zusammenfassen (Lauf 15: sechsmal derselbe "Entlastung"-Hinweis)
    hints = [f"{h} ({n}×)" if n > 1 else h for h, n in Counter(hints).items()]

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
        # Pass 2 schreibt S-O-R-C manchmal als einen Absatz ("S: ... O: ... C+: ...", Lauf 16):
        # Feldbezeichnungen erst auf eigene Zeilen holen, sonst bleiben die alten C-Zeilen stehen
        # Verirrtes "K:" vor "Intern:" in der S-Zeile (Lauf 18: "... durch den Bruder; K: Intern: ...")
        section_text = re.sub(r"[;,]?\s*\bK\s*:\s*(?=Intern\s*:)", "; ", section_text)
        section_text = _INLINE_FIELD_RE.sub(lambda m: m.group(1).rstrip(" ;,") + "\n" + m.group(2), section_text)
        lines = [l for l in section_text.splitlines() if not _C_LINE_RE.match(l)]
        r_idx = [i for i, l in enumerate(lines) if re.match(r"^\s*R\s*:", l)]
        pos = r_idx[-1] + 1 if r_idx else len(lines)
        lines.insert(pos, c_block)
        section_text = "\n".join(lines)
    if hints:
        section_text = section_text.rstrip() + "\n\n" + "\n".join(f"[Prüfhinweis: {h}]" for h in hints)
    return section_text
