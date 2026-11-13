# Plan: Drag and Drop, Schutz vor Überschreiben, „Test Compression“

Stand: A und B sind umgesetzt, C als erster Entwurf (siehe dort, was noch offen ist). Die Abschnitte beschreiben den
ursprünglichen Plan; Abweichungen stehen jeweils am Anfang.

## Ausgangslage (im Code geprüft)

- Ziel-Dateinamen entstehen im Scan (`UnifiedScanWorker` in `compressor/workers.py`): `Zielordner / relativer Pfad / Name + Postfix + .mp4`.
- Es gibt **eine** Schutzprüfung: Ist der berechnete Zielpfad *textlich gleich* dem Quellpfad (leeres Postfix im selben Ordner), wird die Datei **stillschweigend übersprungen**. Es gibt keine Warnung.
- Nicht abgedeckt: macOS-Dateisysteme sind meist nicht groß-/kleinschreibungsempfindlich (`clip.MOV` und `clip.mov` sind dieselbe Datei, aber ungleiche Pfade), außerdem Symlinks und Hardlinks.
- ffmpeg läuft mit `-y` und schreibt **direkt auf den endgültigen Namen**. Bei „Überschreiben“ geht das alte Ergebnis schon verloren, sobald der neue Lauf startet; bricht er ab, ist es weg.
- „Vorhandene Ergebnisse überschreiben“ fragt vor dem Start einmal nach (generischer Text, ohne Anzahl).
- Ohne „Überschreiben“ wird ein vorhandenes Ziel übersprungen, wenn die Dauer passt (`start_video_process`).

## A. Nie ungefragt überschreiben — umgesetzt

Entschieden und gebaut (die Beschreibung der Ausgangslage oben ist der Stand davor):

1. `same_file` / `path_key` (`compressor/utils.py`): gleiche Datei, auch bei anderer Groß-/Kleinschreibung oder über Links.
2. **Quelle = Ziel ist sichtbar:** Die Zeile bleibt in der Liste, der Status steht rot als „⚠ Quelle = Ziel!“, und die Zusammenfassung nennt die Anzahl. Das gilt auch, wenn das Ziel ein *anderes* zu verarbeitendes Video wäre.
3. **Warnung vor dem Start** mit 5-Sekunden-Sperre (`DelayedConfirmBox` in `core`): Text sagt, dass die Originale ersetzt werden und danach weg sind; „Abbrechen“ ist die Vorgabe.
4. **Ersetzen ist sicher:** ffmpeg schreibt in `name.partial.mp4`; erst nach Erfolg ersetzt ein atomares Umbenennen die Datei. Ist das Ergebnis nicht kleiner als das Original, bleibt das Original unverändert („ORIGINAL BEHALTEN“). Abbruch oder Fehler lassen vorhandene Ergebnisse unberührt; auch „Überschreiben“ verliert das alte Ergebnis nicht mehr vorzeitig.
5. **Namenskollisionen:** Haben mehrere Quellen denselben Zielnamen (`test.mov` und `test.mp4`), behalten alle ihre Endung im Namen: `test.mov_archived.mp4`, `test.mp4_archived.mp4`. Das gilt auch bei leerem Postfix und unabhängig von Groß-/Kleinschreibung. Nur wirklich gleiche Namen (zwei Ordner, „keine Unterordner“) bleiben als Dublette markiert.

Noch offen aus diesem Abschnitt: Der Start-Dialog für „Überschreiben“ nennt noch keine Anzahl; Teildateien eines abgestürzten Laufs werden erst beim nächsten Start der betroffenen Datei aufgeräumt, nicht beim Programmstart.

## B. Drag and Drop — umgesetzt

Umgesetzt wie unten beschrieben; Abweichungen: Über der Liste ist die ganze Liste ein Quelle-Feld (außerhalb zwei große Hälften). Das Ziel akzeptiert auch eine Datei und nimmt dann deren Ordner. Mit Zielordner landen alle Ergebnisse dort, ohne Zielordner neben den Originalen. Das Postfix darf leer sein: Quelle = Ziel wird dann wie überall rot markiert und vor dem Start bestätigt.

1. **Zwei große Ablagefelder:** Zieht man etwas über das Fenster, erscheint ein halbtransparentes Overlay mit zwei Hälften „Quelle“ und „Ziel“. Ein Ordner auf einer Hälfte setzt das jeweilige Feld.
2. **Auf die Liste ziehen:**
   - Ein **Ordner** setzt die Quelle (wie `Auswählen…`) und scannt neu.
   - Eine **Auswahl von Dateien** (auch gemischt mit Ordnern, die dann eingelesen werden) wird direkt zur Liste. Neuer Modus „Dateiauswahl“: kein Ordner-Scan, die Liste besteht aus genau diesen Videos.
3. **Ziel im Dateimodus:** leeres Ziel = jeweils der Ordner der Datei („Quelle = Ziel“), mit Postfix. Ist ein Ziel gesetzt, gehen alle Ergebnisse dorthin (ohne Unterordner). Das Quellfeld zeigt „3 Dateien ausgewählt“; ein Hinweisband über der Liste bietet „Zurück zum Ordnermodus“.
4. **Technik:** `FileListScanWorker(paths, dst_dir, postfix, excludes)` liefert dieselben Items wie `UnifiedScanWorker` (`path`, `dst_path`, `size`, …); nur die Namensbildung unterscheidet sich. Dubletten-Warnung, Ausschlussliste und A-Schutz gelten unverändert. Spalte „Ordner“ im Dateimodus: Elternordner der Datei.
5. **Während eines Laufs:** Ablegen wird abgelehnt (kurzer Hinweis), damit sich die Warteschlange nicht ändert.
6. **Nicht unterstützt:** Nicht-Videodateien (werden ignoriert und gezählt: „2 Dateien übersprungen“).

Tests: Drop eines Ordners / mehrerer Dateien / gemischt, Overlay erscheint und verschwindet, Postfix-Pflicht bei Quelle = Ziel, Rückkehr in den Ordnermodus, Ablehnung während eines Laufs, Einstellungen werden nicht mit dem Dateimodus verwechselt (`save_defaults` speichert nur Ordner).

## C. „Test Compression“ — erster Entwurf umgesetzt (`compressor/lab.py`)

Stand: Fenster mit Ausschnitt, drei Varianten, Größen und Hochrechnung, Einzelbilder nebeneinander mit Einpassen/100 %/200 % und gemeinsamem Verschieben, „Diese Einstellungen verwenden“. Seitdem ergänzt: Fortschritt und Zeitschätzung, „Alle abbrechen“, bis zu acht Varianten per „+ Variante“ (horizontal scrollbar), freie Wahl von Start und Länge (Standard: die ersten 10 s), HDR-Standbilder mit BT.2020-Matrix, HDR-Testvideo mit Farbverläufen. Offen: Abspielen der Varianten, echtes Tone Mapping für HDR-Standbilder (dieses ffmpeg hat kein `zscale`).

Ein Knopf **„Inspect different compressions“ neben „Process next“** in der Leiste der markierten Zeile öffnet ein eigenes Fenster für dieses Video.

1. **Ausschnitt:** Standard die ersten 10 Sekunden (ursprünglich geplant: aus der Mitte), Start und Länge frei wählbar, damit Varianten in Sekunden entstehen und nicht ganze Videos kodiert werden.
2. **Varianten:** bis zu acht Spalten (ursprünglich vier), jede mit eigenem Encoder, CRF/Qualität, Preset, Auflösung, Bildrate. Vorbelegt mit den aktuellen Einstellungen und den drei Presets Klein/Archiv/Hoch.
3. **Anzeige nebeneinander:** pro Variante ein Bild (ein Standbild, das sich per Regler ändern lässt; abspielbar und synchron ist noch offen), Dateigröße des Ausschnitts, **hochgerechnete Größe des ganzen Videos**, Kodierdauer.
4. **Vergleich:** Original und Varianten als Einzelbilder mit Einpassen und Zoomstufen, gemeinsames Verschieben. Noch offen: synchrones Abspielen für N Spieler (dafür müssten die vorhandene `ZoomableVideoView` und die Synchronisierung des Vergleichsfensters verallgemeinert werden).
5. **Übernehmen:** „Diese Einstellungen verwenden“ schreibt die gewählte Variante in den Einstellungs-Drawer.
6. **Technik:** ffmpeg-Aufrufe wie im `ArchiveWorker`, aber mit `-ss/-t` und in ein temporäres Verzeichnis (wird beim Schließen gelöscht, nie im Zielordner). Läuft über denselben Befehlsbau wie der normale Lauf, damit die Vorschau wirklich dem Ergebnis entspricht (HDR, Audio, Filter).

Tests: Befehlsbau identisch zum normalen Lauf (gleiche Argumente bis auf `-ss/-t`), Hochrechnung der Größe, Aufräumen der temporären Dateien, Übernahme der Einstellungen, Fenster mit 1–8 Varianten.

## Reihenfolge und Aufwand

| Schritt | Inhalt | Aufwand (grob) |
|---|---|---|
| 1 | A: Schutz, rote Warnung, Teildatei, Dialog, Kollisionsnamen | erledigt |
| 2 | B: Overlay + Ordner auf Liste | erledigt |
| 3 | B: Dateimodus (Worker, Liste, Hinweisband) | erledigt |
| 4 | C: Fenster, Varianten, Größen, Vorschaubilder | erledigt |
| 5 | C: synchrones Abspielen für N Varianten (Zoom für Einzelbilder ist erledigt) | offen, mittel bis groß |

## Fragen (entschieden)

- Soll der Dateimodus Einstellungen (Quelle/Ziel) speichern, oder nur beim nächsten Start den Ordnermodus laden? Entschieden: nur der Ordnermodus wird gespeichert (`save_defaults`).
- „Test Compression“: wie viele Varianten höchstens? Entschieden: acht (`MAX_VARIANTS` in `compressor/lab.py`).
- Darf der Dateimodus ein leeres Postfix mit anderem Zielordner erlauben (Name bleibt, Endung `.mp4`)? Entschieden: ja; Quelle = Ziel wird wie überall rot markiert und vor dem Start bestätigt.
