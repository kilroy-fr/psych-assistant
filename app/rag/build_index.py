# app/rag/build_index.py

import os
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

# Ollama-Host aus Umgebungsvariable, Default: Service-Name im Docker-Netz
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")

logger.info(f"Using OLLAMA_HOST={OLLAMA_HOST}")

# Lokales Embedding-Modell über Ollama
embed_model = OllamaEmbedding(
    model_name="nomic-embed-text",
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


def build_index():
    """Lädt vorhandenen Index aus dem Storage oder baut ihn neu auf."""

    # 1) Wenn Persist-Verzeichnis existiert und nicht leer ist → Index laden
    if os.path.isdir(PERSIST_DIR) and os.listdir(PERSIST_DIR):
        logger.info(f"Lade bestehenden Index aus {PERSIST_DIR} ...")
        storage_context = StorageContext.from_defaults(persist_dir=PERSIST_DIR)
        index = load_index_from_storage(
            storage_context,
            embed_model=embed_model,  # explizit unser Ollama-Embedding
        )
        logger.info("Index erfolgreich geladen.")
        return index

    # 2) Sonst: Index neu aufbauen
    logger.info(f"Kein bestehender Index gefunden, baue neuen Index aus {DATA_DIR} ...")

    if not os.path.isdir(DATA_DIR):
        raise RuntimeError(
            f"DATA_DIR {DATA_DIR} existiert nicht. "
            "Bitte stelle sicher, dass deine Dokumente im Container vorhanden sind."
        )

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
    logger.info(f"Neuer Index erstellt und in {PERSIST_DIR} gespeichert.")
    logger.info(f"Verarbeitete Dokumente: {len(enriched_docs)}")

    return index
