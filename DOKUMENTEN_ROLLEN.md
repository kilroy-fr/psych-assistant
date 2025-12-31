# Rollenbasierte Dokumententrennung im RAG-System

## Übersicht

Das RAG-System verwendet jetzt eine rollenbasierte Dokumententrennung, um verschiedene Arten von Wissensdokumenten klar zu unterscheiden.

## Dokumentrollen

### 1. Struktur-Leitfaden (`role=struktur_leitfaden`)

**Datei:** `report_schema_vt_umwandlung.json`
**Quelle:** `PTV3`
**Zweck:** Definiert die **verbindliche Gliederung und Struktur** für VT-Umwandlungsberichte

**Verwendung im Prompt:**
```
Die Gliederung folgt ausschließlich dem Struktur-Leitfaden (PTV-3, VT Erwachsene).
Nutze Dokumente mit role=struktur_leitfaden für Aufbau/Gliederung.
```

**Priorität:** **HÖCHSTE** - Die Struktur ist nicht verhandelbar

### 2. Qualitäts-Checkliste (`role=qualitaets_checkliste`)

**Datei:** `checkliste_vt_beihilfe.json`
**Quelle:** `Beihilfe`
**Zweck:** Konkretisiert **inhaltliche Anforderungen** innerhalb der vorgegebenen Struktur

**Verwendung im Prompt:**
```
Nutze Dokumente mit role=qualitaets_checkliste, um die Inhalte
innerhalb der vorgegebenen Gliederung zu konkretisieren.
```

**Priorität:** Nachrangig zur Struktur-Vorgabe

### 3. Stilvorlage (`doc_type=stilvorlage`)

**Datei:** `beispiel.pdf`
**Zweck:** Musterdokument für **Schreibstil und Tonalität**

## Metadaten-Struktur

Jedes Dokument erhält beim Indexieren folgende Metadaten:

```python
{
    "role": "struktur_leitfaden" | "qualitaets_checkliste",
    "source": "PTV3" | "Beihilfe",
    "doc_type": "struktur" | "checkliste" | "stilvorlage",
    "description": "Beschreibung des Dokuments"
}
```

## Konfiguration in `build_index.py`

```python
DOCUMENT_METADATA = {
    "report_schema_vt_umwandlung.json": {
        "role": "struktur_leitfaden",
        "source": "PTV3",
        "doc_type": "struktur",
        "description": "PTV-3 Strukturvorgaben für VT-Umwandlungsberichte"
    },
    "checkliste_vt_beihilfe.json": {
        "role": "qualitaets_checkliste",
        "source": "Beihilfe",
        "doc_type": "checkliste",
        "description": "Qualitätscheckliste für Beihilfe-konforme Inhalte"
    }
}
```

## Verwendung in Prompts

### Pass 1 (Fakten-Extraktion)

```
DOKUMENTEN-ROLLEN:
1. STRUKTUR-LEITFADEN (role=struktur_leitfaden, source=PTV3):
   → Definiert AUSSCHLIESSLICH Aufbau und Gliederung
   → Höchste Priorität

2. QUALITÄTS-CHECKLISTE (role=qualitaets_checkliste, source=Beihilfe):
   → Konkretisiert inhaltliche Anforderungen
   → Nachrangig zur PTV3-Struktur

DIE GLIEDERUNG FOLGT AUSSCHLIESSLICH DEM STRUKTUR-LEITFADEN.
```

### Pass 2 (Berichtsformulierung)

```
STRUKTURVORGABE:
Nutze die Dokumente mit role=struktur_leitfaden für die Gliederung.

QUALITÄTSSICHERUNG:
Nutze die Dokumente mit role=qualitaets_checkliste für Vollständigkeit.

WICHTIG: Die Struktur aus PTV3 ist nicht verhandelbar.
```

## Index neu erstellen

Nach Änderungen an den Metadaten muss der Index neu erstellt werden:

```bash
# Storage löschen
docker exec psych-assistant rm -rf /app/storage/*

# Container neu starten (baut Index neu)
docker restart psych-assistant
```

## Vorteile dieser Trennung

1. **Klare Verantwortlichkeiten**: Struktur vs. Inhalt
2. **Prioritätssteuerung**: PTV3-Struktur ist verbindlich
3. **Erweiterbarkeit**: Neue Rollen können hinzugefügt werden
4. **Transparenz**: Dokumentherkunft ist nachvollziehbar
5. **Qualitätssicherung**: Checkliste ergänzt ohne zu überschreiben
