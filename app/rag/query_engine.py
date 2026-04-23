import os
import tempfile
import logging
import requests

from llama_index.core import VectorStoreIndex, Settings, SimpleDirectoryReader
from llama_index.llms.ollama import Ollama
from llama_index.embeddings.ollama import OllamaEmbedding
from typing import Optional
from werkzeug.utils import secure_filename

from .build_index import build_index

logger = logging.getLogger(__name__)
diag_logger = logging.getLogger("psych_debug")  # schreibt in debug_results.log

_index = None  # globaler Index-Cache

DEFAULT_MODEL = "gemma3:12b"
DEFAULT_EMBED_MODEL = "nomic-embed-text"  # muss in Ollama gepullt sein

# Ollama-Host aus Umgebungsvariable, Default: Service-Name im Docker-Netz
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")

# Globale Konfiguration für LlamaIndex mit Ollama
Settings.llm = Ollama(
    model=DEFAULT_MODEL,
    base_url=OLLAMA_HOST,
    request_timeout=600.0,  # 10 Minuten für komplexe RAG-Anfragen
)

Settings.embed_model = OllamaEmbedding(
    model_name=DEFAULT_EMBED_MODEL,
    base_url=OLLAMA_HOST,
)

def get_index():
    global _index
    if _index is None:
        # Hier deine bestehende build_index()-Logik aufrufen
        _index = build_index()
    return _index

ALLOWED_EXTENSIONS = {".pdf", ".txt", ".docx"}


def build_temp_index_from_uploaded_files(uploaded_files):
    """
    Baut einen temporären Index aus hochgeladenen Dateien.
    Unterstützt: PDF, TXT, DOCX
    """
    if not uploaded_files:
        return None

    tmpdir = tempfile.mkdtemp(prefix="uploaded_docs_")
    file_paths = []

    for f in uploaded_files:
        filename = f.filename or ""
        ext = os.path.splitext(filename)[1].lower()

        if ext not in ALLOWED_EXTENSIONS:
            # andere Dateitypen ignorieren
            continue

        safe_name = secure_filename(filename)
        path = os.path.join(tmpdir, safe_name)
        f.save(path)
        file_paths.append(path)

    if not file_paths:
        return None

    # SimpleDirectoryReader kann pdf/txt/docx verarbeiten,
    # sofern die passenden Parser-Pakete installiert sind.
    docs = SimpleDirectoryReader(input_files=file_paths).load_data()
    temp_index = VectorStoreIndex.from_documents(
        docs,
        embed_model=Settings.embed_model
    )
    return temp_index


def answer_question(
    question: str,
    system_prompt: Optional[str] = None,
    uploaded_files=None,
    model_name: Optional[str] = None,
    disable_rag: bool = False,
    temperature: Optional[float] = None,
    num_ctx_override: Optional[int] = None,
) -> str:
    """
    Beantwortet eine Frage anhand des globalen Psychotherapie-Index
    und optional zusätzlicher hochgeladener Dateien (pdf/txt/docx).

    system_prompt:
        Prompt, den du im Frontend bearbeiten kannst
    uploaded_files:
        List[FileStorage] aus Flask (request.files.getlist("files"))
    disable_rag:
        Wenn True, wird kein RAG verwendet (nur LLM + Prompt)
    """

    question = (question or "").strip()
    system_prompt = (system_prompt or "").strip()

    if not question:
        return "Bitte gib eine Frage ein."

    # Wenn ein Modell ausgewählt wurde, verwende es statt des Standard-LLM
    if model_name:
        # Kontextgröße für kleinere Modelle begrenzen
        request_timeout = 600.0  # 10 Minuten default
        llm_kwargs = {
            "model": model_name,
            "base_url": OLLAMA_HOST,
            "request_timeout": request_timeout,
        }

        # Temperatur für Pass 2 (Berichtsformulierung) sehr niedrig setzen
        # für maximale Kontrolle und Vermeidung von Halluzinationen
        if disable_rag:  # Pass 2 ohne RAG
            llm_kwargs["temperature"] = 0.1
            llm_kwargs["request_timeout"] = 1200.0  # 20 Minuten für Pass 2 mit langem Context

        llm = Ollama(**llm_kwargs)
    else:
        llm = Settings.llm

    # DIREKTER MODUS OHNE RAG (für Pass 2)
    if disable_rag:
        try:
            # Direkte Anfrage ohne RAG-Kontext
            if system_prompt:
                prompt = f"{system_prompt}\n\nFrage: {question}"
            else:
                prompt = question

            # Verwende Ollama API direkt mit num_ctx Parameter für längeren Context
            ollama_url = f"{OLLAMA_HOST}/api/generate"

            # Context-Größe abhängig vom Modell (oder Override verwenden)
            if num_ctx_override:
                num_ctx = num_ctx_override
            else:
                # Kleine Modelle (8B und kleiner) brauchen deutlich weniger Context
                # um Speicher-Crashes zu vermeiden (siehe llama3.1:8b SIGSEGV bei num_ctx=131072)
                num_ctx = 4096  # Sicherer Default für kleine Modelle (3B-8B)

                if model_name:
                    model_lower = model_name.lower()
                    # Für größere Modelle (12B+) mehr Context erlauben
                    if any(size in model_lower for size in ['12b', '13b','14b', '27b', '34b', '70b']):
                        num_ctx = 12288
                    # Explizit kleine Modelle mit reduziertem Context
                    elif any(size in model_lower for size in ['3b', '4b', '7b', '8b', '9b']):
                        num_ctx = 8192

            # Temperatur (Override oder Default 0.1)
            temp = temperature if temperature is not None else 0.1

            payload = {
                "model": model_name or DEFAULT_MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temp,
                    "num_ctx": num_ctx,
                    "num_predict": 4096  # Erlaube längere Antworten
                }
            }

            logger.info(f"Pass 2: {model_name}, num_ctx={num_ctx}, temp={temp}")
            resp = requests.post(ollama_url, json=payload, timeout=1200.0)
            resp.raise_for_status()
            result = resp.json()
            return result.get("response", "").strip()
        except requests.exceptions.Timeout:
            return ("⏱️ Die Anfrage hat zu lange gedauert (Timeout nach 20 Minuten).\n\n"
                    "Empfehlung: Verwenden Sie ein größeres/schnelleres Modell für Pass 2.")
        except Exception as e:
            logger.error(f"Error in direct LLM call: {e}")
            return f"❌ Fehler bei der Anfrage: {str(e)}"

    # NORMALER RAG-MODUS
    # 1) Basis-Index laden
    base_index = get_index()

    # 2) Optional: hochgeladene Dateien als Zusatzkontext laden
    upload_context = ""
    temp_index = build_temp_index_from_uploaded_files(uploaded_files)

    def build_upload_context(chunks):
        ctx = "\n\n=== HOCHGELADENE PATIENTENDATEN (vollständig) ===\n"
        for i, text in enumerate(chunks, 1):
            ctx += f"\n[Patientendaten Teil {i}]:\n{text}\n"
        ctx += "\n=== ENDE PATIENTENDATEN ===\n"
        return ctx

    doc_chunks = []  # einzelne Chunks für spätere Kürzung zugänglich halten
    if temp_index is None:
        diag_logger.warning(f"RAG: temp_index ist None - keine Patientendaten geladen! "
                            f"uploaded_files={len(uploaded_files or [])} Dateien")
    else:
        # Für Pass 1: ALLE Patientendaten komplett laden, nicht nur ähnliche Chunks
        # Die Frage ist generisch ("Analysiere Patientendaten"), daher ist Similarity-Search suboptimal
        # Besser: Alle Chunks einbeziehen
        all_doc_ids = list(temp_index.docstore.docs.keys())
        total_chunks = len(all_doc_ids)

        # Verwende ALLE Chunks für maximale Vollständigkeit (bis zu einem sinnvollen Limit)
        max_chunks = min(total_chunks, 100)  # Limit bei 100 Chunks um nicht zu überladen

        # Hole alle Dokumente direkt aus dem docstore statt via Retriever
        for doc_id in all_doc_ids[:max_chunks]:
            doc = temp_index.docstore.get_document(doc_id)
            doc_chunks.append(doc.text)

        upload_context = build_upload_context(doc_chunks)
        diag_logger.info(f"RAG: {len(doc_chunks)}/{total_chunks} Chunks geladen, "
                         f"upload_context={len(upload_context)} Zeichen")

    # 3) Context-Größe für RAG-Modus einstellen
    # Pass 1 braucht VIEL Context, weil alle Patientendaten + Guidelines + System-Prompt geladen werden
    # Typischer Pass 1 Prompt: ~30.000 Token (mit 25 Chunks Patientendaten + Guidelines)
    # ABER: Sehr große Modelle (70B) haben extreme Speicheranforderungen!
    num_ctx_rag = 32768  # 32K Default - genug für die meisten Patientendaten

    if model_name:
        model_lower = model_name.lower()
        # RIESIGE Modelle (70B): Brauchen massiv RAM/VRAM, Context stark begrenzen
        if '70b' in model_lower:
            num_ctx_rag = 8192  # Begrenzt aber nutzbar für 70B
            logger.info(f"Pass 1: SEHR GROSSES Modell ({model_name}), num_ctx={num_ctx_rag}")
        # Mittlere Modelle (14-34B): Brauchen mehr Context für vollständige Patientendaten
        # WICHTIG: Diese müssen VOR den kleineren Modellen geprüft werden (14b enthält auch "4b"!)
        # e4b = gemma4 MoE (27B Gewichte, 4B aktiv) → wie mittleres Modell behandeln
        elif any(f':{size}' in model_lower or f'-{size}' in model_lower for size in ['14b', '20b', '27b', '34b']) or ':e4b' in model_lower:
            num_ctx_rag = 49152  # 48K für mittlere Modelle - verhindert Prompt-Kürzung
            logger.info(f"Pass 1: Mittleres Modell ({model_name}), num_ctx={num_ctx_rag}")
        # Kleine-mittlere Modelle (12-13B): Guter Kompromiss
        elif any(f':{size}' in model_lower or f'-{size}' in model_lower for size in ['12b', '13b']):
            num_ctx_rag = 36864  # 36K
            logger.info(f"Pass 1: Kompakt-Modell ({model_name}), num_ctx={num_ctx_rag}")
        # Kleine Modelle (3-9B): Können viel Context bei geringem VRAM
        elif any(f':{size}' in model_lower or f'-{size}' in model_lower for size in ['3b', '4b', '7b', '8b', '9b']):
            num_ctx_rag = 36864  # 36 - kleine Modelle vertragen das gut
            logger.info(f"Pass 1: Kleines Modell ({model_name}), num_ctx={num_ctx_rag}")

    # 4) NEUER ANSATZ: Direkte Ollama API statt LlamaIndex Query Engine
    # Problem: LlamaIndex Query Engine behandelt Patientendaten als "Frage", nicht als Kontext
    # Lösung: Direkter API-Call mit explizitem Prompt-Aufbau
    try:
        # Hole relevante Guidelines aus dem base_index
        retriever = base_index.as_retriever(similarity_top_k=10)
        guideline_nodes = retriever.retrieve(question)

        guideline_context = ""
        if guideline_nodes:
            guideline_context = "\n\n=== STRUKTUR-VORGABEN (Guidelines) ===\n"
            for i, node in enumerate(guideline_nodes, 1):
                guideline_context += f"\n[Guideline {i}]:\n{node.text}\n"
            guideline_context += "\n=== ENDE GUIDELINES ===\n"
            logger.info(f"Guidelines: {len(guideline_nodes)} Chunks")

        # Kombiniere ALLES in einem klaren Prompt
        full_prompt = f"""{system_prompt or ''}

{guideline_context}

{upload_context}

Frage: {question}"""

        # WICHTIG: Prompt-Länge prüfen und ggf. kürzen
        # Grobe Schätzung: 1 Token ≈ 3 Zeichen (für Deutsch konservativ)
        estimated_tokens = len(full_prompt) // 3
        max_prompt_tokens = num_ctx_rag - 4096  # 4K Reserve für Output (Pass 1 braucht weniger Output als Pass 2)

        if estimated_tokens > max_prompt_tokens:
            logger.warning(f"Prompt zu lang ({estimated_tokens} Token geschätzt), max erlaubt: {max_prompt_tokens}")
            # Stufe 1: Guidelines auf 3 Chunks kürzen
            guideline_context_short = ""
            if guideline_nodes:
                guideline_context_short = "\n\n=== STRUKTUR-VORGABEN (Guidelines - gekürzt) ===\n"
                for i, node in enumerate(guideline_nodes[:3], 1):
                    guideline_context_short += f"\n[Guideline {i}]:\n{node.text}\n"
                guideline_context_short += "\n=== ENDE GUIDELINES ===\n"

            full_prompt = f"""{system_prompt or ''}

{guideline_context_short}

{upload_context}

Frage: {question}"""

            # Stufe 2: Wenn immer noch zu lang, Patientendaten-Chunks schrittweise entfernen
            estimated_tokens = len(full_prompt) // 3
            if estimated_tokens > max_prompt_tokens and doc_chunks:
                original_chunk_count = len(doc_chunks)
                active_chunks = list(doc_chunks)
                while len(active_chunks) > 1 and len(full_prompt) // 3 > max_prompt_tokens:
                    active_chunks.pop()
                    trimmed_upload = build_upload_context(active_chunks)
                    full_prompt = f"""{system_prompt or ''}

{guideline_context_short}

{trimmed_upload}

Frage: {question}"""
                logger.warning(
                    f"Patientendaten auf {len(active_chunks)}/{original_chunk_count} Chunks "
                    f"gekürzt (Prompt passt jetzt in {num_ctx_rag} Token-Fenster)"
                )

        # Direkter Ollama API Call (wie in Pass 2)
        ollama_url = f"{OLLAMA_HOST}/api/generate"

        # num_predict basierend auf verfügbarem Context anpassen
        num_predict = min(4096, num_ctx_rag // 2)  # Max 50% des Context für Output

        payload = {
            "model": model_name or DEFAULT_MODEL,
            "prompt": full_prompt,
            "stream": False,
            "options": {
                "temperature": 0.3,  # Etwas höher für bessere Extraktion
                "num_ctx": num_ctx_rag,
                "num_predict": num_predict
            }
        }

        resp = requests.post(ollama_url, json=payload, timeout=1200.0)
        resp.raise_for_status()
        result = resp.json()
        return result.get("response", "").strip()

    except requests.exceptions.Timeout:
        return ("⏱️ Die Anfrage hat zu lange gedauert (Timeout nach 15 Minuten).\n\n"
                "Mögliche Lösungen:\n"
                "- Verwenden Sie ein kleineres/schnelleres Modell (z.B. llama3.2:3b)\n"
                "- Kürzen Sie den System-Prompt\n"
                "- Stellen Sie eine einfachere Frage")
    except requests.exceptions.RequestException as e:
        logger.error(f"Error in Pass 1 Ollama request: {e}")
        return f"❌ Fehler bei der Pass 1 Anfrage: {str(e)}"
    except Exception as e:
        logger.error(f"Unexpected error in Pass 1: {e}")
        return f"❌ Unerwarteter Fehler in Pass 1: {str(e)}"


