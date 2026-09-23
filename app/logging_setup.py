# app/logging_setup.py
"""Debug-Logging fuer Zwischenergebnisse.

Eigenes Modul, damit report_checks.py, sorc.py und anonymization.py denselben
Logger verwenden koennen, ohne app.py zu importieren (Zirkelimport).
"""
import logging
import os

# Werkzeug Request-Logging reduzieren (verhindert doppelte Logs)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

# Debug-Logging für Zwischenergebnisse
debug_logger = logging.getLogger("psych_debug")
debug_logger.setLevel(logging.DEBUG)

# Log-Datei im gemounteten data-Verzeichnis (./data auf Host = /app/data/data im Container)
log_dir = "/app/data/data"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "debug_results.log")

file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
debug_logger.addHandler(file_handler)

# Inhalte (Pass-1-Vorschau, SORC-Block, gefundene Namen) nur auf ausdruecklichen
# Wunsch ins Log: sie enthalten Echtdaten, das Log liegt dauerhaft in ./data.
LOG_PATIENT_CONTENT = os.environ.get("LOG_PATIENT_CONTENT") == "1"
