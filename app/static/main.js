// main.js - Psychotherapie-KI-Assistent Frontend

// DOM-Elemente
const form = document.getElementById("qa-form");
const filesInput = document.getElementById("files");
const submitBtn = document.getElementById("submit-btn");
const pasteText = document.getElementById("paste-text");
const charCount = document.getElementById("char-count");
const inputFilesPanel = document.getElementById("input-files");
const inputPastePanel = document.getElementById("input-paste");
const clearFilesBtn = document.getElementById("clear-files");
const clearPasteBtn = document.getElementById("clear-paste");
const dropZone = document.getElementById("drop-zone");
const fileList = document.getElementById("file-list");

// Globale Variablen
let selectedFiles = new DataTransfer();
let timerInterval = null;
let startTime = 0;
let currentAbortController = null;
let comparisonData = null;
let selectedColumns = [];
let eventSource = null;
let currentSessionId = null;
let resultPollingInterval = null;
let isComputationRunning = false;

// Polling-Konfiguration für robuste Ergebnis-Abfrage
const RESULT_POLL_INTERVAL_MS = 3000;  // Alle 3 Sekunden
const SSE_RECONNECT_DELAY_MS = 2000;   // 2 Sekunden vor SSE-Reconnect

// ============================================
// Panel-Management
// ============================================

function updatePanelStates() {
  const hasFiles = selectedFiles.files.length > 0;
  const hasText = pasteText.value.trim().length > 0;

  if (hasFiles) {
    inputPastePanel.classList.add("disabled");
    inputFilesPanel.classList.remove("disabled");
    inputFilesPanel.classList.add("active");
    clearFilesBtn.style.display = "block";
  } else if (hasText) {
    inputFilesPanel.classList.add("disabled");
    inputPastePanel.classList.remove("disabled");
    inputPastePanel.classList.add("active");
    clearPasteBtn.style.display = "block";
  } else {
    inputFilesPanel.classList.remove("disabled", "active");
    inputPastePanel.classList.remove("disabled", "active");
    clearFilesBtn.style.display = "none";
    clearPasteBtn.style.display = "none";
  }

  updateSubmitButton();
}

function updateSubmitButton() {
  const hasFiles = selectedFiles.files.length > 0;
  const hasText = pasteText.value.trim().length > 0;
  const hasInput = hasFiles || hasText;

  if (hasInput) {
    submitBtn.disabled = false;
    submitBtn.style.opacity = "1";
    submitBtn.style.cursor = "pointer";
  } else {
    submitBtn.disabled = true;
    submitBtn.style.opacity = "0.5";
    submitBtn.style.cursor = "not-allowed";
  }
}

// ============================================
// Text-Eingabe
// ============================================

pasteText.addEventListener("input", () => {
  if (selectedFiles && selectedFiles.files.length > 0) {
    pasteText.value = "";
    return;
  }
  const len = pasteText.value.length;
  charCount.textContent = len.toLocaleString("de-DE") + " Zeichen";
  updatePanelStates();
});

clearPasteBtn.addEventListener("click", () => {
  pasteText.value = "";
  charCount.textContent = "0 Zeichen";
  updatePanelStates();
});

// ============================================
// Datei-Management
// ============================================

clearFilesBtn.addEventListener("click", () => {
  selectedFiles = new DataTransfer();
  filesInput.files = selectedFiles.files;
  updateFileList();
  updatePanelStates();
});

function updateFileList() {
  fileList.innerHTML = "";
  if (selectedFiles.files.length === 0) {
    return;
  }

  Array.from(selectedFiles.files).forEach((file, index) => {
    const fileItem = document.createElement("div");
    fileItem.className = "file-item";

    const fileName = document.createElement("span");
    fileName.className = "file-name";
    fileName.textContent = file.name;

    const fileSize = document.createElement("span");
    fileSize.className = "file-size";
    fileSize.textContent = formatFileSize(file.size);

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "file-remove";
    removeBtn.textContent = "×";
    removeBtn.onclick = () => removeFile(index);

    fileItem.appendChild(fileName);
    fileItem.appendChild(fileSize);
    fileItem.appendChild(removeBtn);
    fileList.appendChild(fileItem);
  });
}

function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function removeFile(index) {
  const newDataTransfer = new DataTransfer();
  Array.from(selectedFiles.files).forEach((file, i) => {
    if (i !== index) newDataTransfer.items.add(file);
  });
  selectedFiles = newDataTransfer;
  filesInput.files = selectedFiles.files;
  updateFileList();
  updatePanelStates();
}

function addFiles(files) {
  if (pasteText.value.trim().length > 0) return;

  const allowedTypes = ['.pdf', '.txt', '.docx'];
  Array.from(files).forEach(file => {
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    if (allowedTypes.includes(ext)) {
      selectedFiles.items.add(file);
    }
  });
  filesInput.files = selectedFiles.files;
  updateFileList();
  updatePanelStates();
}

// ============================================
// Drag & Drop
// ============================================

dropZone.addEventListener("click", (e) => {
  if (e.target !== filesInput) {
    filesInput.click();
  }
});

filesInput.addEventListener("change", () => {
  addFiles(filesInput.files);
});

dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  e.stopPropagation();
  dropZone.classList.add("drag-over");
});

dropZone.addEventListener("dragleave", (e) => {
  e.preventDefault();
  e.stopPropagation();
  dropZone.classList.remove("drag-over");
});

dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  e.stopPropagation();
  dropZone.classList.remove("drag-over");
  addFiles(e.dataTransfer.files);
});

window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

// ============================================
// Spinner & Timer
// ============================================

function showSpinner() {
  const overlay = document.getElementById("spinner-overlay");
  overlay.style.display = "flex";
  startTime = Date.now();
  timerInterval = setInterval(updateTimer, 100);
  resetProgressTracker();
}

function hideSpinner() {
  const overlay = document.getElementById("spinner-overlay");
  overlay.style.display = "none";
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  // Polling wird NICHT hier gestoppt - das muss explizit passieren wenn Ergebnis da ist
  currentAbortController = null;
}

function updateTimer() {
  const elapsed = Date.now() - startTime;
  const seconds = Math.floor(elapsed / 1000);
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  const timerEl = document.getElementById("spinner-timer");
  timerEl.textContent = String(minutes).padStart(2, '0') + ':' + String(remainingSeconds).padStart(2, '0');
}

// ============================================
// Fortschritts-Tracking
// ============================================

function resetProgressTracker() {
  // Alle Kombis und Sections zurücksetzen
  document.querySelectorAll('.progress-combo').forEach(combo => {
    combo.classList.remove('active', 'completed');
  });
  document.querySelectorAll('.progress-section').forEach(section => {
    section.classList.remove('active', 'completed');
    section.querySelector('.section-pass').textContent = '';
  });
}

function updateProgress(data) {
  const comboEl = document.querySelector(`.progress-combo[data-combo="${data.combo}"]`);
  if (!comboEl) return;

  // Gesamte Kombi gestartet
  if (data.status === 'starting') {
    document.querySelectorAll('.progress-combo').forEach(c => {
      if (c !== comboEl) c.classList.remove('active');
    });
    comboEl.classList.add('active');
    comboEl.classList.remove('completed');
    return;
  }

  // Gesamte Kombi abgeschlossen (nur wenn section === 'done')
  if (data.status === 'completed' && data.section === 'done') {
    comboEl.classList.remove('active');
    comboEl.classList.add('completed');
    comboEl.querySelectorAll('.progress-section').forEach(s => {
      s.classList.add('completed');
      s.classList.remove('active');
    });
    return;
  }

  // Section-Lookup
  const sectionMap = {'1-3': '13', '4': '4', '5': '5', '6': '6'};
  const sectionKey = sectionMap[data.section];
  if (!sectionKey) return;

  const sectionEl = comboEl.querySelector(`.progress-section[data-section="${sectionKey}"]`);
  if (!sectionEl) return;

  if (data.status === 'running') {
    // Entferne 'active' von anderen Sections dieser Kombi
    comboEl.querySelectorAll('.progress-section').forEach(s => {
      if (s !== sectionEl) s.classList.remove('active');
    });
    sectionEl.classList.add('active');
    sectionEl.classList.remove('completed');
    if (data.pass) {
      sectionEl.querySelector('.section-pass').textContent = `Pass ${data.pass}`;
    }
  } else if (data.status === 'section_done') {
    // Einzelne Section abgeschlossen, Kombi laeuft weiter
    sectionEl.classList.add('completed');
    sectionEl.classList.remove('active');
    if (data.pass) {
      sectionEl.querySelector('.section-pass').textContent = `Pass ${data.pass}`;
    }
  }
}

function connectProgressStream(sessionId) {
  if (eventSource) {
    eventSource.close();
  }

  eventSource = new EventSource(`/progress/${sessionId}`);

  eventSource.onmessage = (event) => {
    const data = JSON.parse(event.data);

    if (data.status === 'done') {
      eventSource.close();
      eventSource = null;
      return;
    }

    if (data.status === 'error') {
      console.error('Berechnung fehlgeschlagen:', data.error);
      return;
    }

    updateProgress(data);
  };

  eventSource.onerror = (error) => {
    console.error('SSE Error:', error);
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    // Bei SSE-Fehler: Reconnect versuchen wenn Berechnung noch läuft
    if (isComputationRunning && currentSessionId) {
      setTimeout(() => {
        if (isComputationRunning && currentSessionId) {
          connectProgressStream(currentSessionId);
        }
      }, SSE_RECONNECT_DELAY_MS);
    }
  };
}

// ============================================
// Ergebnis-Polling (robust gegen Standby)
// ============================================

function startResultPolling(sessionId) {
  stopResultPolling();

  // Session im localStorage speichern für Recovery nach Seiten-Reload
  localStorage.setItem('pendingSessionId', sessionId);
  localStorage.setItem('pendingSessionStart', Date.now().toString());

  resultPollingInterval = setInterval(() => {
    checkForResult(sessionId);
  }, RESULT_POLL_INTERVAL_MS);

  // Sofort einmal prüfen
  checkForResult(sessionId);
}

function stopResultPolling() {
  if (resultPollingInterval) {
    clearInterval(resultPollingInterval);
    resultPollingInterval = null;
  }
}

async function checkForResult(sessionId) {
  try {
    const response = await fetch(`/result/${sessionId}`);
    const data = await response.json();

    if (data.status === 'completed') {
      // Ergebnis erhalten - Berechnung abgeschlossen
      handleComputationComplete(data.data);
    } else if (data.status === 'error') {
      // Fehler bei der Berechnung
      handleComputationError(data.error);
    } else if (data.status === 'not_found') {
      // Session nicht mehr vorhanden (evtl. Server neugestartet)
      stopResultPolling();
      localStorage.removeItem('pendingSessionId');
      localStorage.removeItem('pendingSessionStart');
    }
    // Bei status === 'running' weiterpollen
  } catch (error) {
    console.error('Fehler beim Abrufen des Ergebnisses:', error);
    // Bei Netzwerkfehlern weiterpollen (könnte Standby sein)
  }
}

function handleComputationComplete(data) {
  isComputationRunning = false;
  stopResultPolling();
  localStorage.removeItem('pendingSessionId');
  localStorage.removeItem('pendingSessionStart');

  // DOCX Download-Link erstellen
  const docxBytes = Uint8Array.from(atob(data.docx_base64), c => c.charCodeAt(0));
  const blob = new Blob([docxBytes], {type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'});
  const url = window.URL.createObjectURL(blob);
  const downloadLink = document.getElementById("download-link");
  downloadLink.href = url;

  // Vergleichstabelle rendern
  renderComparisonTable(data);

  // Timing-Übersicht rendern
  if (data.timing_log && data.timing_log.length > 0) {
    renderTimingTable(data.timing_log, data.models);
  }

  // Ergebnis-Container anzeigen
  document.getElementById("answer-container").style.display = "block";

  hideSpinner();
}

function renderTimingTable(timingLog, modelNames) {
  const container = document.getElementById("timing-container");
  const content = document.getElementById("timing-content");

  // Gruppiere nach Kombi
  const byCombo = {};
  timingLog.forEach(entry => {
    if (!byCombo[entry.combo]) byCombo[entry.combo] = [];
    byCombo[entry.combo].push(entry);
  });

  const sectionLabel = {"1-3": "Abschnitte 1-3", "4": "Abschnitt 4", "5": "Abschnitt 5", "6": "Abschnitt 6"};

  let html = '<table class="timing-table">';
  html += '<thead><tr>';
  html += '<th>Kombi</th><th>Abschnitt</th><th>Pass</th><th>Modell</th><th>Dauer</th>';
  html += '</tr></thead><tbody>';

  Object.keys(byCombo).sort((a, b) => a - b).forEach(combo => {
    const entries = byCombo[combo];
    let comboTotal = 0;

    entries.forEach((entry, idx) => {
      const dur = entry.duration;
      if (!entry.cached) comboTotal += dur;

      const durStr = entry.cached
        ? '<span class="timing-cached">Cache</span>'
        : formatDuration(dur);

      const rowClass = idx % 2 === 0 ? 'timing-row-odd' : 'timing-row-even';
      html += `<tr class="${rowClass}">`;
      html += `<td>${idx === 0 ? 'Kombi ' + combo : ''}</td>`;
      html += `<td>${sectionLabel[entry.section] || entry.section}</td>`;
      html += `<td>Pass ${entry.pass}</td>`;
      html += `<td><span class="timing-model">${entry.model}</span></td>`;
      html += `<td>${durStr}</td>`;
      html += '</tr>';
    });

    // Summenzeile pro Kombi
    html += `<tr class="timing-subtotal">`;
    html += `<td colspan="4">Kombi ${combo} Gesamt</td>`;
    html += `<td>${formatDuration(comboTotal)}</td>`;
    html += '</tr>';
  });

  // Gesamtsumme (nur nicht-gecachte Einträge)
  const grandTotal = timingLog.filter(e => !e.cached).reduce((sum, e) => sum + e.duration, 0);
  html += `<tr class="timing-total">`;
  html += `<td colspan="4">Gesamtzeit (LLM-Aufrufe)</td>`;
  html += `<td>${formatDuration(grandTotal)}</td>`;
  html += '</tr>';

  html += '</tbody></table>';
  content.innerHTML = html;
  container.style.display = "block";
}

function formatDuration(seconds) {
  if (seconds < 60) return seconds.toFixed(1) + ' s';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m + ' min ' + String(s).padStart(2, '0') + ' s';
}

function handleComputationError(errorMessage) {
  isComputationRunning = false;
  stopResultPolling();
  localStorage.removeItem('pendingSessionId');
  localStorage.removeItem('pendingSessionStart');

  hideSpinner();
  alert("Fehler bei der Berechnung: " + errorMessage);
}

// ============================================
// Standby/Visibility Recovery
// ============================================

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    // Prüfe ob eine Berechnung lief
    if (isComputationRunning && currentSessionId) {
      // SSE neu verbinden
      connectProgressStream(currentSessionId);
      // Sofort Ergebnis prüfen (falls während Standby fertig geworden)
      checkForResult(currentSessionId);
    }
  }
});

// Bei Seiten-Load: Prüfe auf ausstehende Session
window.addEventListener('load', () => {
  const pendingSessionId = localStorage.getItem('pendingSessionId');
  const pendingStart = localStorage.getItem('pendingSessionStart');

  if (pendingSessionId && pendingStart) {
    const elapsed = Date.now() - parseInt(pendingStart, 10);
    // Nur wiederherstellen wenn weniger als 1 Stunde vergangen
    if (elapsed < 3600000) {
      currentSessionId = pendingSessionId;
      isComputationRunning = true;
      startTime = parseInt(pendingStart, 10);
      showSpinner();
      connectProgressStream(pendingSessionId);
      startResultPolling(pendingSessionId);
    } else {
      // Session zu alt - aufräumen
      localStorage.removeItem('pendingSessionId');
      localStorage.removeItem('pendingSessionStart');
    }
  }
});

// ============================================
// Vergleichstabelle
// ============================================

// Abschnitte mit 1-Pass-System (identische Ergebnisse in allen Kombis)
// Index 0=Abschnitt 1, 1=Abschnitt 2, 2=Abschnitt 3, 4=Abschnitt 5
const SINGLE_PASS_ROWS = [0, 1, 2, 4];

function renderComparisonTable(data) {
  comparisonData = data;
  selectedColumns = new Array(data.sections.length).fill(0);

  // Setze Header für die Anzahl der tatsächlich vorhandenen Modelle
  const numModels = data.models.length;
  for (let i = 0; i < numModels; i++) {
    document.getElementById(`model-header-${i + 1}`).textContent = data.models[i];
  }

  const tbody = document.getElementById("comparison-tbody");
  tbody.innerHTML = "";

  data.sections.forEach((sectionHeader, rowIdx) => {
    const tr = document.createElement("tr");
    const isSinglePass = SINGLE_PASS_ROWS.includes(rowIdx);

    if (isSinglePass) {
      // Single-Pass-Abschnitte: Eine Zelle über beide Spalten
      const td = document.createElement("td");
      td.className = "comparison-cell single-pass-cell selected";
      td.dataset.row = rowIdx;
      td.dataset.col = 0;
      td.dataset.tooltip = sectionHeader;
      td.colSpan = numModels;

      // Verwende HTML-formatierte Ergebnisse falls vorhanden
      const cellContent = (data.html_results && data.html_results[0] && data.html_results[0][rowIdx])
        ? data.html_results[0][rowIdx]
        : data.results[0][rowIdx] || "";

      td.innerHTML = cellContent;
      tr.appendChild(td);
    } else {
      // 2-Pass-Abschnitte: Separate Zellen pro Modellkombination
      for (let colIdx = 0; colIdx < numModels; colIdx++) {
        const td = document.createElement("td");
        td.className = "comparison-cell";
        td.dataset.row = rowIdx;
        td.dataset.col = colIdx;
        td.dataset.tooltip = sectionHeader;

        const cellContent = (data.html_results && data.html_results[colIdx] && data.html_results[colIdx][rowIdx])
          ? data.html_results[colIdx][rowIdx]
          : data.results[colIdx][rowIdx] || "";

        td.innerHTML = cellContent;

        if (colIdx === 0) {
          td.classList.add("selected");
        }

        td.addEventListener("click", () => selectCell(rowIdx, colIdx));
        tr.appendChild(td);
      }
    }

    tbody.appendChild(tr);
  });

  document.getElementById("comparison-table-container").style.display = "block";
}

function selectCell(rowIdx, colIdx) {
  // Single-Pass-Zeilen haben nur eine Zelle - keine Auswahl nötig
  if (SINGLE_PASS_ROWS.includes(rowIdx)) {
    return;
  }

  const row = document.getElementById("comparison-tbody").rows[rowIdx];
  for (let i = 0; i < row.cells.length; i++) {
    row.cells[i].classList.remove("selected");
  }

  row.cells[colIdx].classList.add("selected");
  selectedColumns[rowIdx] = colIdx;
}

async function createTextDocument() {
  if (!comparisonData) return;

  const selectedTexts = comparisonData.sections.map((_, rowIdx) => {
    const colIdx = selectedColumns[rowIdx];
    return comparisonData.results[colIdx][rowIdx] || "";
  });

  try {
    const response = await fetch("/create-text", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        sections: comparisonData.sections,
        selected_texts: selectedTexts
      })
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "bericht.docx";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  } catch (error) {
    console.error("Fehler beim Erstellen des Textes:", error);
    alert("Fehler beim Erstellen des Dokuments: " + error.message);
  }
}

document.getElementById("create-text-btn").addEventListener("click", createTextDocument);

// ============================================
// Formular-Submit
// ============================================

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  // Generiere neue Session-ID
  currentSessionId = 'session_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);

  currentAbortController = new AbortController();
  isComputationRunning = true;
  showSpinner();

  // Starte Progress-Stream
  connectProgressStream(currentSessionId);

  try {
    const formData = new FormData(form);
    formData.append('session_id', currentSessionId);

    // Starte die Berechnung (gibt sofort zurück)
    const response = await fetch("/ask-compare", {
      method: "POST",
      body: formData,
      signal: currentAbortController.signal
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const data = await response.json();

    if (data.status === 'started') {
      // Berechnung gestartet - starte Polling für Ergebnis
      startResultPolling(data.session_id);
    } else if (data.error) {
      throw new Error(data.error);
    }

    // NICHT hideSpinner() hier aufrufen - das passiert wenn Ergebnis da ist

  } catch (error) {
    if (error.name === 'AbortError') {
      isComputationRunning = false;
      stopResultPolling();
      localStorage.removeItem('pendingSessionId');
      localStorage.removeItem('pendingSessionStart');
      hideSpinner();
    } else {
      console.error("Fehler:", error);
      isComputationRunning = false;
      stopResultPolling();
      localStorage.removeItem('pendingSessionId');
      localStorage.removeItem('pendingSessionStart');
      hideSpinner();
      alert("Fehler bei der Anfrage: " + error.message);
    }
    currentAbortController = null;
  }
});

// ============================================
// Abbrechen-Button
// ============================================

document.getElementById("cancel-btn").addEventListener("click", () => {
  if (currentAbortController) {
    currentAbortController.abort();
  }
  // Polling und SSE stoppen
  isComputationRunning = false;
  stopResultPolling();
  localStorage.removeItem('pendingSessionId');
  localStorage.removeItem('pendingSessionStart');
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
  hideSpinner();
});
