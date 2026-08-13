# app/rag/build_index.py

import os
import json
import hashlib
import logging

from llama_index.core import (
    VectorStoreIndex,
    SimpleDirectoryReader,
    StorageContext,
    load_index_from_storage,
    Settings,
)
from llama_index.embeddings.ollama import OllamaEmbedding

logger = logging.getLogger(__name__)

# Pfade ggf. an dein Projekt anpassen
DATA_DIR = "/app/data/data/guidelines"  # hier liegen deine Quell-Dokumente (gemountetes Volume)
PERSIST_DIR = "/app/storage"            # hier wird der Index gespeichert

# Fingerabdruck-Datei im Storage: erkennt geänderte Quelldokumente
FINGERPRINT_FILE = "index_fingerprint.json"
FINGERPRINT_VERSION = 1

EMBED_MODEL_NAME = "nomic-embed-text"

# Ollama-Host aus Umgebungsvariable, Default: Service-Name im Docker-Netz
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")

logger.info(f"Using OLLAMA_HOST={OLLAMA_HOST}")

# Lokales Embedding-Modell über Ollama
embed_model = OllamaEmbedding(
    model_name=EMBED_MODEL_NAME,
    base_url=OLLAMA_HOST,  # GANZ WICHTIG: nicht localhost oder feste IP
)

# Optional, aber praktisch: globaler Default für LlamaIndex
Settings.embed_model = embed_model

# Datei-zu-Metadaten-Mapping
# Definiert, welche Dokumente welche Rolle haben
DOCUMENT_METADATA = {
    "beispiel.pdf": {
        "doc_type": "stilvorlage",
        "description": "Musterdokument für Schreibstil und Tonalität"
    },
    "report_schema_vt_umwandlung.json": {
        "role": "struktur_leitfaden",
        "source": "PTV3",
        "doc_type": "struktur",
        "description": "PTV-3 Strukturvorgaben für VT-Umwandlungsberichte (verbindliche Gliederung)"
    },
    "checkliste_vt_beihilfe.json": {
        "role": "qualitaets_checkliste",
        "source": "Beihilfe",
        "doc_type": "checkliste",
        "description": "Qualitätscheckliste für Beihilfe-konforme Berichtsinhalte"
    },
    "SORC.json": {
        "role": "bedingungsmodell_schema",
        "source": "VT_Methodik",
        "doc_type": "struktur",
        "description": "SORC-Schema für verhaltenstherapeutisches Bedingungsmodell (Stimulus-Organismus-Response-Consequences)"
    },
    "psychopathologischer_befund.json": {
        "role": "befund_schema",
        "source": "AMDP",
        "doc_type": "struktur",
        "description": "Strukturschema für psychopathologischen Befund (Bewusstsein, Orientierung, Gedächtnis, Denken, Wahrnehmung, Affektivität, Antrieb)"
    },
    "biographische_anamnese_leitfaden.json": {
        "role": "anamnese_leitfaden",
        "source": "PTV3",
        "doc_type": "struktur",
        "description": "Leitfaden für präzise und knappe biographische Anamnese (Abschnitt 4.1): Relevanz, Prägnanz, Störungsfokus"
    },
    # Legacy-Unterstützung
    "guidelines.pdf": {
        "doc_type": "fachinhalt",
        "description": "Fachliche Richtlinien und Strukturvorgaben (veraltet)"
    },
}


def _file_sha256(path):
    """Berechnet den SHA-256-Hash einer Datei (blockweise, speicherschonend)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _compute_fingerprint():
    """Erzeugt einen Fingerabdruck des Index-Eingangszustands.

    Beruecksichtigt Inhalt aller Quelldateien, das Metadaten-Mapping und das
    Embedding-Modell. Aendert sich davon etwas, ist der Index veraltet.
    """
    file_hashes = {}
    for name in sorted(os.listdir(DATA_DIR)):
        path = os.path.join(DATA_DIR, name)
        if os.path.isfile(path):
            file_hashes[name] = _file_sha256(path)

    metadata_raw = json.dumps(DOCUMENT_METADATA, sort_keys=True, ensure_ascii=False)

    return {
        "version": FINGERPRINT_VERSION,
        "embed_model": EMBED_MODEL_NAME,
        "metadata_hash": hashlib.sha256(metadata_raw.encode("utf-8")).hexdigest(),
        "files": file_hashes,
    }


def _read_fingerprint():
    """Liest den gespeicherten Fingerabdruck, oder None wenn nicht vorhanden/lesbar."""
    path = os.path.join(PERSIST_DIR, FINGERPRINT_FILE)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logger.warning(f"Fingerabdruck {path} nicht lesbar ({e}) -> Index wird neu gebaut.")
        return None


def _write_fingerprint(fingerprint):
    """Speichert den Fingerabdruck neben dem Index."""
    path = os.path.join(PERSIST_DIR, FINGERPRINT_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(fingerprint, f, indent=2, ensure_ascii=False)


def _describe_fingerprint_diff(stored, current):
    """Beschreibt in Worten, warum der Index als veraltet gilt (fuer das Log)."""
    if stored is None:
        return "kein Fingerabdruck vorhanden (Index aus aelterer Version)"

    reasons = []
    if stored.get("version") != current["version"]:
        reasons.append("Fingerabdruck-Format geaendert")
    if stored.get("embed_model") != current["embed_model"]:
        reasons.append(
            f"Embedding-Modell geaendert: {stored.get('embed_model')} -> {current['embed_model']}"
        )
    if stored.get("metadata_hash") != current["metadata_hash"]:
        reasons.append("DOCUMENT_METADATA geaendert")

    old_files = stored.get("files", {})
    new_files = current["files"]
    added = sorted(set(new_files) - set(old_files))
    removed = sorted(set(old_files) - set(new_files))
    changed = sorted(
        name for name in set(old_files) & set(new_files)
        if old_files[name] != new_files[name]
    )
    if added:
        reasons.append(f"neu: {', '.join(added)}")
    if removed:
        reasons.append(f"entfernt: {', '.join(removed)}")
    if changed:
        reasons.append(f"geaendert: {', '.join(changed)}")

    return "; ".join(reasons) if reasons else "unbekannte Abweichung"


def build_index(force_rebuild=False):
    """Lädt vorhandenen Index aus dem Storage oder baut ihn neu auf.

    Der Index wird neu gebaut, wenn er fehlt, wenn sich die Quelldokumente,
    das Metadaten-Mapping oder das Embedding-Modell geaendert haben, oder
    wenn force_rebuild=True gesetzt ist.
    """

    if not os.path.isdir(DATA_DIR):
        raise RuntimeError(
            f"DATA_DIR {DATA_DIR} existiert nicht. "
            "Bitte stelle sicher, dass deine Dokumente im Container vorhanden sind."
        )

    current_fingerprint = _compute_fingerprint()

    # Index-Dateien vorhanden? (Fingerabdruck-Datei zaehlt nicht als Index)
    index_exists = os.path.isdir(PERSIST_DIR) and any(
        name != FINGERPRINT_FILE for name in os.listdir(PERSIST_DIR)
    )

    # 1) Vorhandenen Index laden, sofern er noch zum Eingangszustand passt
    if index_exists and not force_rebuild:
        stored_fingerprint = _read_fingerprint()
        if stored_fingerprint == current_fingerprint:
            logger.info(f"Lade bestehenden Index aus {PERSIST_DIR} ...")
            storage_context = StorageContext.from_defaults(persist_dir=PERSIST_DIR)
            index = load_index_from_storage(
                storage_context,
                embed_model=embed_model,  # explizit unser Ollama-Embedding
            )
            logger.info("Index erfolgreich geladen.")
            return index

        logger.info(
            "Index ist veraltet, wird neu gebaut. Grund: "
            + _describe_fingerprint_diff(stored_fingerprint, current_fingerprint)
        )
    elif force_rebuild:
        logger.info("force_rebuild=True -> Index wird neu gebaut.")
    else:
        logger.info(f"Kein bestehender Index gefunden, baue neuen Index aus {DATA_DIR} ...")

    # 2) Index neu aufbauen

    # Dokumente laden
    raw_docs = SimpleDirectoryReader(DATA_DIR).load_data()
    if not raw_docs:
        raise RuntimeError(
            f"Keine Dokumente in {DATA_DIR} gefunden. "
            "Bitte lege dort deine Texte/PDFs ab."
        )

    # Metadaten zu den Dokumenten hinzufügen
    enriched_docs = []
    for doc in raw_docs:
        # Dateiname aus den Metadaten extrahieren (falls vorhanden)
        filename = doc.metadata.get("file_name", "")

        # Prüfen, ob wir für diese Datei spezielle Metadaten haben
        if filename in DOCUMENT_METADATA:
            # Metadaten hinzufügen
            doc.metadata.update(DOCUMENT_METADATA[filename])
            logger.info(f"Metadaten für {filename} hinzugefügt: {DOCUMENT_METADATA[filename]}")
        else:
            # Standard-Metadaten für unbekannte Dokumente
            doc.metadata["doc_type"] = "fachinhalt"
            doc.metadata["description"] = "Allgemeines Dokument"
            logger.info(f"Standard-Metadaten für {filename} hinzugefügt")

        enriched_docs.append(doc)

    # Index mit angereicherten Dokumenten erstellen
    index = VectorStoreIndex.from_documents(
        enriched_docs,
        embed_model=embed_model,  # auch hier explizit
    )

    # Persistieren, damit beim nächsten Start nur noch geladen werden muss
    os.makedirs(PERSIST_DIR, exist_ok=True)
    index.storage_context.persist(persist_dir=PERSIST_DIR)

    # Fingerabdruck erst nach erfolgreichem Persistieren schreiben:
    # bricht der Build ab, gilt der Index beim naechsten Start weiterhin als veraltet
    _write_fingerprint(current_fingerprint)

    logger.info(f"Neuer Index erstellt und in {PERSIST_DIR} gespeichert.")
    logger.info(f"Verarbeitete Dokumente: {len(enriched_docs)}")

    return index
