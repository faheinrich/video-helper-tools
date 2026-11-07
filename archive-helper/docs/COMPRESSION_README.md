# Compress & Archive

<p align="center">
  <img src="icons/tool-compressor.png" alt="Compress & Archive" width="160">
</p>

Das Tool "Compress & Archive" der Video Helper Tools komprimiert ganze Ordner voller Videos per Batch. Es nutzt **FFmpeg** für die Video- und Audio-Konvertierung (HEVC/H.265) und **ExifTool**, um Metadaten (GPS, Aufnahmedatum, User-Tags) vom Original in die komprimierte Datei zu übertragen.

## Features

- **Batch-Komprimierung** eines Quellordners (inkl. Unterordnern) in einen Zielordner, mit Live-Log und Fortschritt pro Datei sowie Gesamt-Ersparnis.
- **Vergleich & Verwaltung in derselben Liste**: Bereits komprimierte Videos werden beim Scan erkannt und bieten direkt Aktionen an: Original durch die komprimierte Version ersetzen, beide tauschen oder eine der beiden löschen.
- **Synchroner Side-by-Side-Player**: Original und komprimierte Version laufen nebeneinander, mit gekoppeltem **Mausrad-Zoom**, **Verschieben** und **Rotation**, um Bilddetails zu prüfen.
- **Hardware-Beschleunigung**: Apple VideoToolbox (Mac-GPU) oder hochwertiges CPU-Encoding mit libx265 (CRF + Preset).
- **Auflösungs- und Framerate-Limits**, z.B. 4K/60fps auf 1080p/30fps.
- **Apple-Fotos-kompatibel**: Ausgaben werden mit dem Codec-Tag `hvc1` geschrieben und lassen sich so in Apple Fotos importieren.
- **Sicherheitsnetz**: Wird die komprimierte Datei größer als das Original, wird zusätzlich das Original als `*_source` daneben kopiert, außer das Ergebnis liegt ohnehin im selben Ordner wie das Original. Dateien mit `_source` im Namen werden beim Scan standardmäßig übersprungen. Bereits vorhandene Ziele (gleiche Dauer) werden übersprungen, außer "Überschreiben" ist aktiv.
- **Dry-Run**: komprimiert nur die erste Sekunde, um Einstellungen schnell zu testen.
- Standard-Einstellungen speichern, Log als CSV exportieren, Drag & Drop von Ordnern.

## Voraussetzungen

```bash
brew install ffmpeg exiftool          # macOS
sudo apt install ffmpeg libimage-exiftool-perl   # Ubuntu/Debian
```

Python-Abhängigkeiten werden mit [uv](https://docs.astral.sh/uv/) installiert (siehe Haupt-README):

```bash
uv sync
```

## Nutzung

```bash
uv run --package archive-helper python archive-helper/main.py   # im Repository-Hauptordner
```

Auf der Startseite **Compress & Archive** wählen, Quell- und Zielordner setzen, **Ordner scannen**, dann **Archivierung starten**.

## Projektstruktur

`archive-helper/archive_helper/compressor/`:
- `gui.py`: Haupt-UI des Tools (Einstellungen, Liste, Aktionen).
- `widgets.py`: Listen-Einträge und der Vergleichs-Player.
- `workers.py`: Hintergrund-Threads für Scannen, Komprimieren und Thumbnails.
- `utils.py`: Hilfsfunktionen (Formatierung, ffprobe-Auswertung, Thumbnails).
- `main.py`: Einstiegspunkt für den Einzelstart.

## Mögliche Erweiterungen

- [ ] Hardware-Beschleunigung für Windows/Linux (NVENC, QSV)
- [ ] Speicherplatz-Prüfung vor dem Start und Restzeit-Schätzung

## License
This project is licensed under [`CC BY-NC-SA 4.0`](https://creativecommons.org/licenses/by-nc-sa/4.0/?ref=chooser-v1).
