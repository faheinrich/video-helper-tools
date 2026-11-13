# Compress & Archive

<p align="center">
  <img src="../resources/tool-compressor.png" alt="Compress & Archive" width="160">
</p>

Das Tool „Compress & Archive“ des Archive Helper komprimiert ganze Ordner voller Videos per Batch. Es nutzt **FFmpeg** für die Video- und Audio-Konvertierung (HEVC/H.265) und **ExifTool** sowie eigenes Box-Kopieren, um die Metadaten (GPS, Aufnahmedatum, Kameradaten, Datenspuren, Dateizeiten) vom Original in die komprimierte Datei zu übertragen.

Die vollständige Beschreibung aller Funktionen steht in [docs/features.md](../../docs/features.md#compress--archive), der Aufbau in [docs/architecture.md](../../docs/architecture.md#archive-helper-archive-helperarchive_helper).

## Features

- **Batch-Komprimierung** eines Quellordners (inkl. Unterordnern) in einen Zielordner, mit Fortschritt pro Datei, Gesamt-Ersparnis, Zeitschätzung und freiem Platz am Ziel. Ohne Zielordner landet jedes Ergebnis neben seinem Original.
- **Vergleich & Verwaltung in derselben Liste**: Bereits komprimierte Videos werden beim Scan erkannt und bieten direkt Aktionen an: Original durch die komprimierte Version ersetzen, beide tauschen oder eine der beiden in den Papierkorb verschieben. Gelöscht wird nie endgültig.
- **Synchroner Vergleichsplayer**: Original und komprimierte Version nebeneinander, mit Schieberegler übereinander oder im Wechsel alle zwei Sekunden; gekoppelter **Mausrad-Zoom**, **Verschieben** und **Rotation**.
- **Kompressions-Labor** („Verschiedene Kompressionen prüfen“): kodiert einen Ausschnitt mit bis zu acht Einstellungen nebeneinander, mit Größe, Hochrechnung und Einzelbildvergleich.
- **Metadaten-Prüfung und -Viewer**: Original und Ergebnis werden Feld für Feld verglichen; fehlt etwas, steht „⚠ Metadaten?“ in der Liste, und der Viewer zeigt beide nebeneinander.
- **Block-Prüfung**: warnt nach jedem Video vor sichtbaren Kompressionsblöcken.
- **Encoder**: hochwertiges CPU-Encoding mit libx265 (CRF + Preset) oder Apple VideoToolbox (Mac-GPU).
- **Auflösungs- und Framerate-Limits**, z.B. 4K/60fps auf 1080p/30fps. HDR bleibt HDR (10 bit).
- **Apple-Fotos-kompatibel**: Ausgaben werden mit dem Codec-Tag `hvc1` geschrieben und lassen sich so in Apple Fotos importieren.
- **Sicherheitsnetz**:
  - Ein Ergebnis wird als `NAME.partial.mp4` geschrieben und bekommt erst fertig seinen Namen; eine vorhandene Datei geht dabei in den Papierkorb.
  - Wird die komprimierte Datei größer als das Original, bleibt sie erhalten, und das Original wird zusätzlich als `NAME_source.ext` daneben kopiert, wenn Apple Fotos es importieren kann und das Ergebnis nicht ohnehin im selben Ordner wie das Original liegt.
  - Soll das Ergebnis das Original ersetzen (Quelle = Ziel), geschieht das nur nach einer Bestätigung und nur, wenn das Ergebnis kleiner ist.
  - Quellvideos, deren Name auf eine der ausgeschlossenen Endungen endet (Standard: `_archived, _compressed, _source`), werden beim Scan übersprungen, ebenso das Ergebnis-Postfix.
  - Bereits vorhandene Ergebnisse werden übersprungen, außer „Vorhandene Ergebnisse überschreiben“ ist aktiv.
- **Testlauf**: komprimiert nur die erste Sekunde, um Einstellungen schnell zu testen.
- Standard-Einstellungen speichern, Liste als CSV exportieren, Drag & Drop von Ordnern und einzelnen Dateien.

## Voraussetzungen

```bash
brew install ffmpeg exiftool          # macOS
sudo apt install ffmpeg libimage-exiftool-perl   # Ubuntu/Debian
```

Python-Abhängigkeiten werden mit [uv](https://docs.astral.sh/uv/) installiert (siehe Haupt-README):

```bash
uv sync --package archive-helper      # im Repository-Hauptordner
```

## Nutzung

```bash
uv run --package archive-helper python archive-helper/main.py   # im Repository-Hauptordner
```

Auf der Startseite **Compress & Archive** wählen und Quell- und Zielordner setzen (oder Ordner bzw. Dateien auf das Fenster ziehen). Der Ordner wird automatisch gescannt; dann **Start archiving** drücken (deutsche Oberfläche: „Archivierung starten“).

## Projektstruktur

`archive-helper/archive_helper/compressor/`:
- `gui.py`: die Seite des Tools (Ordner, Einstellungs-Drawer, Liste, Laufsteuerung, Dateiaktionen).
- `model.py`: Zeilen, Tabellenmodell, Filter, Sortierung und Statustexte.
- `workers.py`: Hintergrund-Threads für Scannen, Komprimieren, Vorschaubilder, Dauern, Einstellungen und Block-Prüfung; Übertragen der Metadaten.
- `boxes.py`: Lesen und Schreiben von MP4/MOV-Boxen (Hersteller-Boxen, Datenspuren, Movie-Keys).
- `utils.py`: Hilfsfunktionen (ffprobe/exiftool, Metadatenvergleich, Aufnahmedaten, Sidecars, Einstellungs-Tag).
- `metadata_view.py`: der Metadaten-Viewer.
- `lab.py`: das Kompressions-Labor.
- `widgets.py`: Vergleichsplayer, zoombare Videoansichten, Ablagefeld und Drop-Overlay.
- `options.py`: die Auswahlmöglichkeiten des Drawers (Encoder, Presets, Auflösungen, Bildraten).

Gestartet wird über `archive-helper/main.py` (die ganze App mit beiden Tools).

## Mögliche Erweiterungen

- [ ] Hardware-Beschleunigung für Windows/Linux (NVENC, QSV)
- [ ] Start verhindern, wenn der Platz am Ziel nicht reicht (bisher nur eine Warnung in der Zusammenfassung)

## License
This project is licensed under [`CC BY-NC-SA 4.0`](https://creativecommons.org/licenses/by-nc-sa/4.0/?ref=chooser-v1).
