# vmaf-gui
GUI for vmaf-comparision using a ffmpeg 


# VMAF Tool (main.py)

Dieses Skript kombiniert eine komfortable PySide6-GUI mit einer CLI, um VMAF-Auswertungen auf Basis von `ffmpeg` und `libvmaf` zu fahren. Es unterstützt optionale Auto-Crop-Vorschläge, Teilbereiche (Start/Ende, Dauer), benutzerdefinierte Modelle sowie eine native Pipeline ohne zusätzliche Filter.

## Voraussetzungen

- Linux/macOS/WSL mit Python 3.10 (getestet mit 3.10.12)
- `ffmpeg` **mit** `libvmaf` und `ffprobe` im `PATH`
- (GUI) X11/Wayland-Unterstützung inkl. benötigter Qt/XCB-Bibliotheken
- Optional: Eigenes VMAF-Modell (JSON)

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

Die Datei `requirements.txt` enthält exakt die Pakete, die aktuell im Projekt-Venv installiert sind:

- PySide6 6.9.2 + shiboken6
- Matplotlib 3.10.6 + Abhängigkeiten (numpy, pillow, …)

## Nutzung

### CLI-Modus (Standard)

```bash
source .venv/bin/activate
python main.py --dist dist.mp4 --ref ref.mp4 \
    --scale Native \
    --crop 3840:1600:0:280 \
    --json-out results/vmaf.json
```

Wichtige Optionen:

- `--scale {1080p,Native,WxH}`: legt die Skalierung fest. Im `Native`-Modus werden keine zusätzlichen Filter gesetzt.
- `--crop W:H:X:Y`: beschneidet Referenz **und** Distorted. Für Native muss der Crop in beide Quellen passen; wird Distorted exakt auf die Crop-Größe gebracht, entfällt das zweite Crop automatisch.
- `--start/--end/--duration`: Zeitfenster (Sekunden oder HH:MM:SS(.ms)).
- `--model`: Pfad zu einem alternativen VMAF-Modell.
- `--subsample N`: wertet nur jeden N-ten Frame aus.
- `--plot` / `--json-out`: schreiben Plot/JSON auf Platte.

### GUI-Modus

```bash
source .venv/bin/activate
python main.py --gui
```

Die GUI bietet:

- Datei-Dialoge für Referenz/Distorted und optionale Modell-Datei
- Direktanzeige der Video-Abmessungen (Benötigt funktionierendes `ffprobe`)
- Spin-Boxen für Crop (Breite/Höhe > 0, `X/Y = 0` entspricht „auto/leer“)
- Start/Ende-Eingabefelder (gleiches Format wie CLI)
- Auto-Crop über `ffmpeg cropdetect`
- Live-Status, Log-Panel und eingebetteten VMAF-Zeitverlauf (Matplotlib)
- Abbrechen-Button zum sauberen Terminieren des laufenden `ffmpeg`

> **Hinweis (Linux/X11):** Falls Qt wegen fehlender Bibliotheken (`libxcb-cursor.so.0`, `libxkbcommon-x11.so.0`) meldet, installiere sie via Paketmanager oder setze `QT_QPA_PLATFORM=wayland`, wenn du auf Wayland unterwegs bist.

### Beispiele

1. **Native ohne Crop** – nur erlaubt, wenn beide Videos identische Auflösung + Pixelformat besitzen:
   ```bash
   python main.py --dist dist.mp4 --ref ref.mp4 --scale Native
   ```

2. **Native mit Crop** – Referenz 3840×2160, Distorted 3840×1600; wir schneiden oben/unten 280 Pixel ab:
   ```bash
   python main.py --dist dist.mp4 --ref ref.mp4 --scale Native --crop 3840:1600:0:280
   ```
   Passt der Crop nicht vollständig in das Distorted-Video, wird er (sofern exakt größenidentisch) nur auf die Referenz angewendet und die Analyse läuft trotzdem.

3. **Standardisierte 1080p-Auswertung mit JSON & Plot**:
   ```bash
   python main.py --dist dist.mp4 --ref ref.mp4 \
       --scale 1080p --json-out out/vmaf.json --plot out/vmaf.png
   ```

### Auto-Crop aus der CLI

```bash
python main.py --suggest-crop 300 --ref ref.mp4
```

Das Skript analysiert die ersten 300 Frames und gibt einen Crop-Vorschlag auf STDOUT aus.

## Troubleshooting

- **`ffmpeg/libvmaf Fehler (rc=...)`**: Prüfe die Status/Log-Ausgabe. Häufige Ursachen sind fehlendes `libvmaf` im ffmpeg-Build, nicht passende Abmessungen im Native-Modus oder falsches Pixelformat.
- **`Crop überschreitet...`**: Passe die Werte an oder wähle eine Skalierung. In Native wird Distorted nur beschnitten, wenn der Crop vollständig innerhalb des Frames liegt.
- **Qt startet nicht / Status zeigt keine Abmessungen**: Stelle sicher, dass `ffprobe` im PATH liegt und die notwendigen Qt-Systembibliotheken installiert sind.
- **Abbruch**: Der Button „Abbrechen“ schickt `SIGTERM` an ffmpeg und killt den Prozess nach wenigen Sekunden, falls er nicht freiwillig beendet.

## Entwicklung

- Die Hauptlogik liegt in `main.py`.
- Tests für Parser und Hilfsfunktionen können mit `python main.py --selftest` ausgeführt werden (keine externen Dateien nötig).
- Achte beim Bearbeiten des GUI-Codes darauf, Qt-Objekte nur über `deleteLater()` freizugeben – das aktuelle Thread/Cleanup-Handling ist darauf ausgelegt.

Viel Erfolg bei der Analyse!
