# Modellempfehlungen für 2-Pass-System

## Hardware-Voraussetzungen
- **VRAM verfügbar:** 16GB
- **System:** Ollama auf lokalem Server/GPU

## Empfohlene Modellkombinationen

### ✅ Option 1: Ausgewogen (EMPFOHLEN)
```
Pass 1: gemma2:9b
Pass 2: gemma2:9b
```

**Vorteile:**
- Sehr gute Instruktionsbefolgung für Faktenextraktion
- Exzellentes Deutsch für Berichtsformulierung
- Kein VRAM-Wechsel zwischen Pässen (schneller)
- ~6GB VRAM pro Modell
- Temperatur 0.1 in Pass 2 für maximale Kontrolle

**Installation:**
```bash
ollama pull gemma2:9b
```

---

### Option 2: Schnelligkeit fokussiert
```
Pass 1: llama3.1:8b
Pass 2: gemma2:9b
```

**Vorteile:**
- llama3.1:8b sehr schnell bei Faktenextraktion (~5GB)
- gemma2:9b besseres Deutsch für Bericht (~6GB)

**Nachteile:**
- Modellwechsel zwischen Pässen

**Installation:**
```bash
ollama pull llama3.1:8b
ollama pull gemma2:9b
```

---

### Option 3: Maximal (falls genug VRAM frei)
```
Pass 1: gemma2:9b
Pass 2: gemma2:27b (quantisiert Q4)
```

**Vorteile:**
- gemma2:27b: Exzellentes Deutsch, sehr präzise
- ~14-15GB VRAM für Pass 2

**Voraussetzung:**
- Nur wenn sonst nichts auf GPU läuft
- Evtl. Ollama Container während Pass 2 als einziger Prozess

**Installation:**
```bash
ollama pull gemma2:9b
ollama pull gemma2:27b
```

---

## Automatische Optimierungen

Das System passt sich automatisch an:

### Pass 1 (Faktenextraktion)
- **Modus:** RAG aktiviert (nutzt Wissensbasis)
- **Temperatur:** Standard (0.7)
- **Kontext:** Voller Zugriff auf Patientendaten + Dokumente
- **Ziel:** Strikte Zuordnung zu Struktur-Leitfaden

### Pass 2 (Berichtsformulierung)
- **Modus:** RAG deaktiviert (nur Pass-1-Output)
- **Temperatur:** 0.1 (maximale Kontrolle)
- **Kontext:** Pass 1 Output + System Prompt
- **Kontextfenster:** 4096 Token für kleine Modelle
- **Timeout:** 3 Minuten für kleine Modelle

---

## Nicht empfohlen (bekannte Probleme)

❌ **llama3.2:3b**
- Zu schwach für komplexe Berichte
- Crashes bei großem Pass-1-Output

❌ **deepseek-r1:8b** (aktuelle Probleme)
- Crashes in Pass 2 trotz Optimierungen
- VRAM-Probleme

❌ **Sehr große Modelle (>30B) ohne Quantisierung**
- Überschreiten 16GB VRAM-Limit

---

## Installation empfohlener Modelle

```bash
# Hauptempfehlung
ollama pull gemma2:9b

# Optional: Für maximale Qualität
ollama pull gemma2:27b

# Fallback: Falls gemma2 nicht verfügbar
ollama pull llama3.1:8b
```

---

## Testen der Modelle

1. **Webapp öffnen:** http://localhost:5000
2. **Pass 1 Modell wählen:** gemma2:9b
3. **Pass 2 Modell wählen:** gemma2:9b
4. **Patientendaten hochladen** (PDF/TXT/DOCX)
5. **Bericht erstellen** klicken

Die Logs zeigen die aktiven Optimierungen:
```bash
docker logs -f psych-assistant
```

Erwartete Log-Meldungen:
- `Setze Temperatur auf 0.1 für Pass 2`
- `RAG deaktiviert - direkter LLM-Call`
- `Begrenze Kontextgröße auf 4096`

---

## Performance-Erwartungen

| Modell | Pass 1 | Pass 2 | Gesamt | Qualität |
|--------|--------|--------|---------|----------|
| gemma2:9b + gemma2:9b | ~30s | ~45s | ~75s | ⭐⭐⭐⭐⭐ |
| llama3.1:8b + gemma2:9b | ~20s | ~45s | ~65s | ⭐⭐⭐⭐ |
| gemma2:9b + gemma2:27b | ~30s | ~90s | ~120s | ⭐⭐⭐⭐⭐ |

*Zeiten sind Richtwerte für mittlere Berichte (~2000 Wörter)*

---

## Troubleshooting

### "llama runner process has terminated"
→ Modell zu groß oder Pass-1-Output zu lang
→ Lösung: Wechsel zu gemma2:9b

### Timeout nach 3 Minuten
→ Pass 2 dauert zu lange
→ Lösung: Kürzerer System-Prompt oder größeres Modell

### Schlechte Berichtsqualität
→ Temperatur zu hoch oder Modell zu schwach
→ Lösung: Temperatur auf 0.1 gesetzt (automatisch), gemma2:9b verwenden

---

**Letzte Aktualisierung:** 2025-11-19
