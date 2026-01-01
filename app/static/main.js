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

  // Markiere aktuelle Kombi als aktiv
  if (data.status === 'starting') {
    // Entferne 'active' von allen Kombis
    document.querySelectorAll('.progress-combo').forEach(c => {
      if (c !== comboEl) c.classList.remove('active');
    });
    comboEl.classList.add('active');
    comboEl.classList.remove('completed');
  }

  if (data.status === 'completed') {
    comboEl.classList.remove('active');
    comboEl.classList.add('completed');
    // Markiere alle Sections dieser Kombi als completed
    comboEl.querySelectorAll('.progress-section').forEach(s => {
      s.classList.add('completed');
      s.classList.remove('active');
    });
    return;
  }

  // Finde die richtige Section
  let sectionKey = '';
  if (data.section === '1-3, 5') {
    sectionKey = '135';
  } else if (data.section === '4') {
    sectionKey = '4';
  } else if (data.section === '6') {
    sectionKey = '6';
  }

  if (!sectionKey) return;

  const sectionEl = comboEl.querySelector(`.progress-section[data-section="${sectionKey}"]`);
  if (!sectionEl) return;

  // Entferne 'active' von allen Sections in dieser Kombi
  comboEl.querySelectorAll('.progress-section').forEach(s => {
    if (s !== sectionEl) s.classList.remove('active');
  });

  if (data.status === 'running') {
    sectionEl.classList.add('active');
    sectionEl.classList.remove('completed');

    // Zeige Pass-Nummer
    const passLabel = data.pass === 1 ? 'Pass 1' : 'Pass 2';
    sectionEl.querySelector('.section-pass').textContent = passLabel;
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

    updateProgress(data);
  };

  eventSource.onerror = (error) => {
    console.error('SSE Error:', error);
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  };
}

// ============================================
// Vergleichstabelle
// ============================================

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

    for (let colIdx = 0; colIdx < numModels; colIdx++) {
      const td = document.createElement("td");
      td.className = "comparison-cell";
      td.dataset.row = rowIdx;
      td.dataset.col = colIdx;
      td.dataset.tooltip = sectionHeader;

      const cellText = data.results[colIdx][rowIdx] || "";
      td.textContent = cellText;

      if (colIdx === 0) {
        td.classList.add("selected");
      }

      td.addEventListener("click", () => selectCell(rowIdx, colIdx));
      tr.appendChild(td);
    }

    tbody.appendChild(tr);
  });

  document.getElementById("comparison-table-container").style.display = "block";
}

function selectCell(rowIdx, colIdx) {
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
  showSpinner();

  // Starte Progress-Stream
  connectProgressStream(currentSessionId);

  try {
    const formData = new FormData(form);
    formData.append('session_id', currentSessionId);

    const response = await fetch("/ask-compare", {
      method: "POST",
      body: formData,
      signal: currentAbortController.signal
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const data = await response.json();

    const docxBytes = Uint8Array.from(atob(data.docx_base64), c => c.charCodeAt(0));
    const blob = new Blob([docxBytes], {type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'});
    const url = window.URL.createObjectURL(blob);
    const downloadLink = document.getElementById("download-link");
    downloadLink.href = url;

    renderComparisonTable(data);

    document.getElementById("answer-container").style.display = "block";

  } catch (error) {
    if (error.name === 'AbortError') {
      console.log("Anfrage wurde abgebrochen.");
    } else {
      console.error("Fehler:", error);
      alert("Fehler bei der Anfrage: " + error.message);
    }
  } finally {
    hideSpinner();
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
});
