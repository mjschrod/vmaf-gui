#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VMAF Tool – GUI (optional) + CLI fallback

Änderung (Native-Modus):
- Bei "Native" werden **keine zusätzlichen Filter** erzwungen.
  * Mit Crop: Referenz und Distorted erhalten denselben Crop, damit die Abmessungen identisch bleiben.
  * Ohne Crop: Beide Streams gehen ohne Filter in libvmaf.
  * Keine impliziten format/setsar/scale/pad-Schritte.
  * Voraussetzung: Der Crop passt innerhalb des Distorted-Frames – andernfalls bricht die Analyse mit einer klaren Fehlermeldung ab.
- 1080p/WxH bleiben unverändert (aspektkorrektes scale/pad + setsar + yuv420p).

Weitere Features:
- Start/Endzeit (GUI & CLI)
- Auto-Crop (Default limit=128)
- GUI zeigt "Ref. Abmessungen" / "Dis. Abmessungen"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

GUI_AVAILABLE = False
try:
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtCore import Qt, Signal, Slot
    from PySide6.QtWidgets import (
        QApplication, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
        QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QSpinBox, QComboBox,
        QWidget, QVBoxLayout, QMessageBox, QStatusBar,
    )
    GUI_AVAILABLE = True
except Exception:
    QtCore = QtWidgets = None  # type: ignore
    Qt = Signal = Slot = object  # type: ignore

@dataclass
class VMAFResult:
    mean: Optional[float]
    frames: List[Tuple[int, float]]
    raw_json: dict


@dataclass
class VideoInfo:
    width: int
    height: int
    pix_fmt: str
    sar: Optional[str]

def parse_time_to_seconds(s: str) -> float:
    s = (s or "").strip()
    if not s:
        raise ValueError("leerer Zeitstring")
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    parts = s.split(":")
    if len(parts) == 2:
        m, sec = parts
        return int(m) * 60 + float(sec)
    if len(parts) == 3:
        h, m, sec = parts
        return int(h) * 3600 + int(m) * 60 + float(sec)
    raise ValueError(f"Ungültiges Zeitformat: {s}")

def parse_crop_str(crop: str) -> Tuple[int, int, int, int]:
    if ":" in crop and crop.count(":") == 3:
        w, h, x, y = crop.split(":")
        return int(w), int(h), int(x), int(y)
    if ("x" in crop or "X" in crop) and ":" in crop:
        wh, x, y = crop.split(":")
        w, h = wh.lower().split("x")
        return int(w), int(h), int(x), int(y)
    raise ValueError("Ungültiges Crop-Format. Erwartet W:H:X:Y oder WxH:X:Y")

def parse_vmaf_data(data: dict) -> VMAFResult:
    mean = (
        data.get("pooled_metrics", {}).get("vmaf", {}).get("mean")
        or data.get("aggregate", {}).get("VMAF_score")
    )
    frames: List[Tuple[int, float]] = []
    for fr in data.get("frames", []):
        try:
            frames.append((int(fr.get("frameNum", 0)), float(fr["metrics"]["vmaf"])))
        except Exception:
            pass
    return VMAFResult(mean=float(mean) if mean is not None else None, frames=frames, raw_json=data)

def _ensure_ffmpeg_available() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg nicht gefunden. Bitte ffmpeg installieren und im PATH verfügbar machen.")

def _check_libvmaf_available() -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-v", "0", "-filters"], capture_output=True, text=True)
        return "libvmaf" in (out.stdout + out.stderr)
    except Exception:
        return False

def probe_video_info(path: str) -> Optional[VideoInfo]:
    p = (path or "").strip()
    if not p or not Path(p).exists():
        return None
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        proc = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type,width,height,pix_fmt,sample_aspect_ratio,disposition",
                "-of",
                "json",
                p,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        data = json.loads(proc.stdout)
        for st in data.get("streams", []):
            if st.get("codec_type") != "video":
                continue
            disp = st.get("disposition") or {}
            if disp.get("attached_pic", 0) != 0:
                continue
            w = st.get("width")
            h = st.get("height")
            pix = st.get("pix_fmt") or "unknown"
            sar = st.get("sample_aspect_ratio")
            if w and h:
                return VideoInfo(int(w), int(h), pix, sar if sar not in (None, "0:1", "1:1") else None)
        return None
    except Exception:
        return None


def probe_video_dimensions(path: str) -> Optional[Tuple[int, int]]:
    info = probe_video_info(path)
    if info:
        return info.width, info.height
    return None

def _qt_preflight() -> Tuple[bool, List[str], str]:
    import ctypes
    prefer_wayland = bool(os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("QT_QPA_PLATFORM")
    platform = "wayland" if prefer_wayland else "xcb"
    required: List[str] = []
    if platform == "xcb":
        required = ["libxcb-cursor.so.0", "libxkbcommon-x11.so.0"]
    missing: List[str] = []
    for lib in required:
        try:
            ctypes.CDLL(lib)
        except OSError:
            missing.append(lib)
    return (len(missing) == 0, missing, platform)

def suggest_crop_via_ffmpeg(
    ref_path: str,
    frames: int = 300,
    *, limit: int = 128, round_val: int = 16, reset: int = 0, stream: bool = False,
    progress_cb: Optional[Callable[[Tuple[int, int, int, int]], None]] = None,
) -> Tuple[int, int, int, int]:
    _ensure_ffmpeg_available()
    vf = f"cropdetect={limit}:{round_val}:{reset}"
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "info",
        "-i", ref_path,
        "-vf", vf,
        "-frames:v", str(frames),
        "-f", "null", "-",
    ]
    if not stream:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"cropdetect fehlgeschlagen – rc={proc.returncode}\n{proc.stderr}")
        text_out = (proc.stdout or "") + (proc.stderr or "")
        last_crop = None
        for m in re.finditer(r"crop=(\d+):(\d+):(\d+):(\d+)", text_out):
            last_crop = tuple(map(int, m.groups()))
            if progress_cb and last_crop:
                try: progress_cb(last_crop)
                except Exception: pass
        if not last_crop:
            raise RuntimeError("Kein cropdetect-Ergebnis gefunden – erhöhe ggf. 'Limit'.")
        return last_crop
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    last_crop = None
    assert proc.stderr is not None
    for line in proc.stderr:
        m = re.search(r"crop=(\d+):(\d+):(\d+):(\d+)", line)
        if m:
            last_crop = tuple(map(int, m.groups()))
            if progress_cb and last_crop:
                try: progress_cb(last_crop)
                except Exception: pass
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError("cropdetect fehlgeschlagen – stimmt der Pfad/Codec?")
    if last_crop is None:
        raise RuntimeError("Kein cropdetect-Ergebnis gefunden")
    return last_crop

def _fmt_sec(x: float) -> str:
    return f"{x:.3f}"

def run_vmaf_ffmpeg(
    distorted: str,
    reference: str,
    *, crop_ref: Optional[Tuple[int, int, int, int]] = None,
    scale: str = "1080p",
    threads: int = max(1, os.cpu_count() or 8),
    model_path: Optional[str] = None,
    subsample: int = 1,
    json_out: Optional[Path] = None,
    start_sec: Optional[float] = None,
    end_sec: Optional[float] = None,
    duration_sec: Optional[float] = None,
) -> VMAFResult:
    _ensure_ffmpeg_available()
    if not _check_libvmaf_available():
        raise RuntimeError("ffmpeg wurde ohne libvmaf gebaut. Bitte eine ffmpeg-Version mit libvmaf verwenden.")

    dur: Optional[float] = None
    if start_sec is not None and end_sec is not None:
        dur = max(0.0, float(end_sec) - float(start_sec))
    elif duration_sec is not None:
        dur = max(0.0, float(duration_sec))

    tmp_json = Path(tempfile.gettempdir()) / "vmaf_cli_result.json"
    if tmp_json.exists():
        try: tmp_json.unlink()
        except Exception: pass

    filters_ref: List[str] = []
    filters_dist: List[str] = []
    scale_key = (scale or "").strip().lower()

    ref_info = probe_video_info(reference)
    dist_info = probe_video_info(distorted)

    if crop_ref:
        cw, ch, cx, cy = crop_ref
        filters_ref.append(f"crop={cw}:{ch}:{cx}:{cy}")
        apply_dist_crop = True

        if ref_info and (cw > ref_info.width or ch > ref_info.height or cx + cw > ref_info.width or cy + ch > ref_info.height):
            raise RuntimeError(
                f"Crop überschreitet die Referenz-Abmessungen ({ref_info.width}x{ref_info.height}) – bitte Werte anpassen."
            )

        if dist_info:
            if cw > dist_info.width or ch > dist_info.height:
                raise RuntimeError(
                    f"Crop überschreitet die Distorted-Abmessungen ({dist_info.width}x{dist_info.height}) – bitte Werte anpassen oder eine Skalierung wählen."
                )
            if cx + cw > dist_info.width or cy + ch > dist_info.height:
                if dist_info.width == cw and dist_info.height == ch:
                    apply_dist_crop = False
                else:
                    raise RuntimeError(
                        f"Crop überschreitet die Distorted-Abmessungen ({dist_info.width}x{dist_info.height}) – bitte Werte anpassen oder eine Skalierung wählen."
                    )

        if apply_dist_crop:
            filters_dist.append(f"crop={cw}:{ch}:{cx}:{cy}")

    if scale_key == "native" and ref_info and dist_info:
        if ref_info.width != dist_info.width or ref_info.height != dist_info.height:
            if not crop_ref:
                raise RuntimeError(
                    "Native-Modus erfordert identische Abmessungen. Bitte Crop setzen oder eine Skalierung wählen."
                )
        if ref_info.pix_fmt != dist_info.pix_fmt:
            raise RuntimeError(
                "Native-Modus erfordert identisches Pixelformat. "
                f"Referenz: {ref_info.pix_fmt}, Distorted: {dist_info.pix_fmt}. Bitte 1080p-Skalierung wählen oder Videos angleichen."
            )

    if scale_key == "1080p":
        filters_ref += ["scale=1920:-2:flags=bicubic", "pad=1920:1080:(ow-iw)/2:(oh-ih)/2", "setsar=1", "format=yuv420p"]
        filters_dist += ["scale=1920:-2:flags=bicubic", "pad=1920:1080:(ow-iw)/2:(oh-ih)/2", "setsar=1", "format=yuv420p"]
    elif re.fullmatch(r"\d+x\d+", scale_key or ""):
        w, h = map(int, scale_key.split("x"))
        filters_ref += [f"scale={w}:-2:flags=bicubic", f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "setsar=1", "format=yuv420p"]
        filters_dist += [f"scale={w}:-2:flags=bicubic", f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "setsar=1", "format=yuv420p"]
    elif scale_key == "native":
        # NATIVE: keine weiteren Filter!
        pass
    else:
        filters_ref += ["setsar=1", "format=yuv420p"]
        filters_dist += ["setsar=1", "format=yuv420p"]

    ref_f = ",".join(filters_ref) if filters_ref else "null"
    dist_f = ",".join(filters_dist) if filters_dist else "null"

    vmaf_opts = ["log_fmt=json", f"log_path={tmp_json.as_posix()}", f"n_threads={threads}"]
    if model_path:
        vmaf_opts.append(f"model=path={model_path}")
    if subsample and subsample > 1:
        vmaf_opts.append(f"subsample={subsample}")

    # NATIVE: keine zusätzlichen Filter – optionaler Crop wirkt auf beide Streams
    if scale_key == "native":
        lavfi = f"[1:v]{ref_f}[ref];[0:v]{dist_f}[dist];[dist][ref]libvmaf={':'.join(vmaf_opts)}"
    else:
        lavfi = f"[1:v]{ref_f}[ref];[0:v]{dist_f}[dist];[dist][ref]libvmaf={':'.join(vmaf_opts)}"

    cmd: List[str] = ["ffmpeg", "-hide_banner", "-nostats", "-y"]

    if start_sec is not None:
        cmd += ["-ss", _fmt_sec(float(start_sec))]
    if dur is not None and dur > 0:
        cmd += ["-t", _fmt_sec(dur)]
    cmd += ["-i", distorted]

    if start_sec is not None:
        cmd += ["-ss", _fmt_sec(float(start_sec))]
    if dur is not None and dur > 0:
        cmd += ["-t", _fmt_sec(dur)]
    cmd += ["-i", reference]

    cmd += ["-lavfi", lavfi, "-f", "null", "-"]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg/libvmaf Fehler (rc={proc.returncode})\n{proc.stderr}")

    if not tmp_json.exists():
        raise RuntimeError("VMAF-JSON nicht gefunden – hat libvmaf geloggt?")

    with tmp_json.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if json_out:
        try:
            json_out.parent.mkdir(parents=True, exist_ok=True)
            json_out.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    return parse_vmaf_data(data)

def save_vmaf_plot(frames: List[Tuple[int, float]], out_png: Path) -> None:
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    plt.figure()
    xs = [i for i, _ in frames]
    ys = [v for _, v in frames]
    if xs and ys:
        plt.plot(xs, ys)
        plt.xlabel("Frame"); plt.ylabel("VMAF"); plt.title("VMAF über die Zeit")
        plt.grid(True, which="both", linestyle="--", alpha=0.4)
    else:
        plt.text(0.5, 0.5, "Keine Daten", ha="center", va="center")
    plt.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(out_png); plt.close()

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="VMAF Tool – GUI optional, CLI fallback")
    p.add_argument("--gui", action="store_true", help="GUI starten (erfordert PySide6)")
    p.add_argument("--selftest", action="store_true", help="Selbsttests ausführen (keine externen Abhängigkeiten)")

    p.add_argument("--dist", help="Distorted/Test-Video (Pfad)")
    p.add_argument("--ref", help="Referenz/Original-Video (Pfad)")
    p.add_argument("--crop", help="Crop (W:H:X:Y oder WxH:X:Y); im Native-Modus werden beide Streams entsprechend beschnitten")
    p.add_argument("--scale", default="1080p", help="'1080p', 'Native' oder WxH (z.B. 1280x720)")
    p.add_argument("--threads", type=int, default=max(1, os.cpu_count() or 8), help="Threads für libvmaf")
    p.add_argument("--model", help="VMAF Model JSON Pfad (optional)")
    p.add_argument("--subsample", type=int, default=1, help="Nur jeden n-ten Frame auswerten (>=1)")
    p.add_argument("--json-out", type=Path, help="Pfad für VMAF JSON Log (optional)")
    p.add_argument("--plot", type=Path, help="PNG-Datei für VMAF-Plot (optional)")

    p.add_argument("--start", help="Startzeit (Sekunden oder HH:MM:SS(.ms))")
    p.add_argument("--end", help="Endzeit (Sekunden oder HH:MM:SS(.ms))")
    p.add_argument("--duration", help="Dauer (Sekunden oder HH:MM:SS(.ms))")

    p.add_argument("--suggest-crop", type=int, metavar="N", help="Nur Crop vorschlagen – analysiere erste N Frames der Referenz")
    return p

def run_cli(args: argparse.Namespace) -> int:
    if args.suggest_crop:
        if not args.ref:
            print("--suggest-crop benötigt --ref <Pfad>"); return 2
        try:
            w, h, x, y = suggest_crop_via_ffmpeg(args.ref, frames=args.suggest_crop)
            print(f"Vorgeschlagener Crop: {w}:{h}:{x}:{y}")
            return 0
        except Exception as e:
            print(f"Fehler: {e}"); return 1

    if not args.dist or not args.ref:
        print("Bitte --dist und --ref angeben oder --suggest-crop nutzen. --help für Hilfe."); return 2

    crop_tuple = None
    if args.crop:
        try: crop_tuple = parse_crop_str(args.crop)
        except Exception as e:
            print(f"Ungültiges --crop: {e}"); return 2

    start_sec = end_sec = duration_sec = None
    try:
        if args.start: start_sec = parse_time_to_seconds(args.start)
        if args.end: end_sec = parse_time_to_seconds(args.end)
        if args.duration: duration_sec = parse_time_to_seconds(args.duration)
    except Exception as e:
        print(f"Zeitargument ungültig: {e}"); return 2

    try:
        res = run_vmaf_ffmpeg(
            distorted=args.dist, reference=args.ref, crop_ref=crop_tuple, scale=args.scale,
            threads=args.threads, model_path=args.model, subsample=max(1, int(args.subsample or 1)),
            json_out=args.json_out, start_sec=start_sec, end_sec=end_sec, duration_sec=duration_sec,
        )
    except Exception as e:
        print(f"Fehler: {e}"); return 1

    print(f"Gesamt-VMAF: {res.mean:.2f}" if res.mean is not None else "Gesamt-VMAF: (nicht gefunden)")
    if args.plot:
        try:
            save_vmaf_plot(res.frames, args.plot); print(f"Plot gespeichert: {args.plot}")
        except Exception as e:
            print(f"Plot konnte nicht geschrieben werden: {e}")
    return 0

if GUI_AVAILABLE:
    class MplCanvas(QtWidgets.QWidget):
        def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
            super().__init__(parent)
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
            from matplotlib.figure import Figure
            self._figure = Figure()
            self._canvas = FigureCanvas(self._figure)
            self._ax = self._figure.add_subplot(111)
            layout = QtWidgets.QVBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.addWidget(self._canvas)
            self._figure.tight_layout()
        def plot_vmaf(self, frames: List[Tuple[int, float]]):
            ax = self._ax; ax.clear()
            if frames:
                xs = [i for i,_ in frames]; ys = [v for _,v in frames]
                ax.plot(xs, ys); ax.set_xlabel("Frame"); ax.set_ylabel("VMAF"); ax.set_title("VMAF über die Zeit"); ax.grid(True, which="both", linestyle="--", alpha=0.4)
            else:
                ax.set_title("Keine Daten")
            self._figure.tight_layout(); self._canvas.draw()

    class FfmpegWorker(QtCore.QObject):
        finished = Signal(object); progress = Signal(str)
        def __init__(self, dist_path: str, ref_path: str, crop: Optional[Tuple[int,int,int,int]], scale_opt: str, threads: int, model_path: Optional[str], subsample: Optional[int] = None, start_sec: Optional[float] = None, end_sec: Optional[float] = None, duration_sec: Optional[float] = None, parent: Optional[QtCore.QObject] = None):
            super().__init__(parent)
            self.dist_path = dist_path; self.ref_path = ref_path; self.crop = crop; self.scale_opt = scale_opt
            self.threads = threads; self.model_path = model_path; self.subsample = subsample or 1
            self.start_sec = start_sec; self.end_sec = end_sec; self.duration_sec = duration_sec
            self._proc: Optional[subprocess.Popen[str]] = None
            self._abort_requested = False

        def _build_lavfi(self, tmp_json: Path) -> str:
            filters_ref: List[str] = []; filters_dist: List[str] = []
            scale_key = (self.scale_opt or "").strip().lower()

            ref_info = probe_video_info(self.ref_path)
            dist_info = probe_video_info(self.dist_path)

            if self.crop:
                cw, ch, cx, cy = self.crop
                filters_ref.append(f"crop={cw}:{ch}:{cx}:{cy}")

                apply_dist_crop = True
                if ref_info and (cw > ref_info.width or ch > ref_info.height or cx + cw > ref_info.width or cy + ch > ref_info.height):
                    raise RuntimeError(f"Crop überschreitet die Referenz-Abmessungen ({ref_info.width}x{ref_info.height}) – bitte Werte anpassen.")

                if dist_info:
                    if cw > dist_info.width or ch > dist_info.height:
                        raise RuntimeError(f"Crop überschreitet die Distorted-Abmessungen ({dist_info.width}x{dist_info.height}) – bitte Werte anpassen oder eine Skalierung wählen.")
                    if cx + cw > dist_info.width or cy + ch > dist_info.height:
                        if dist_info.width == cw and dist_info.height == ch:
                            apply_dist_crop = False
                        else:
                            raise RuntimeError(f"Crop überschreitet die Distorted-Abmessungen ({dist_info.width}x{dist_info.height}) – bitte Werte anpassen oder eine Skalierung wählen.")

                if apply_dist_crop:
                    filters_dist.append(f"crop={cw}:{ch}:{cx}:{cy}")

            if scale_key == "native" and ref_info and dist_info:
                if (ref_info.width != dist_info.width or ref_info.height != dist_info.height) and not self.crop:
                    raise RuntimeError("Native-Modus erfordert identische Abmessungen. Bitte Crop setzen oder eine Skalierung wählen.")
                if ref_info.pix_fmt != dist_info.pix_fmt:
                    raise RuntimeError(
                        "Native-Modus erfordert identisches Pixelformat. "
                        f"Referenz: {ref_info.pix_fmt}, Distorted: {dist_info.pix_fmt}. Bitte 1080p-Skalierung wählen oder Videos angleichen."
                    )

            if scale_key == "1080p":
                filters_ref += ["scale=1920:-2:flags=bicubic", "pad=1920:1080:(ow-iw)/2:(oh-ih)/2", "setsar=1", "format=yuv420p"]
                filters_dist += ["scale=1920:-2:flags=bicubic", "pad=1920:1080:(ow-iw)/2:(oh-ih)/2", "setsar=1", "format=yuv420p"]
            elif re.fullmatch(r"\d+x\d+", scale_key or ""):
                w, h = map(int, scale_key.split("x"))
                filters_ref += [f"scale={w}:-2:flags=bicubic", f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "setsar=1", "format=yuv420p"]
                filters_dist += [f"scale={w}:-2:flags=bicubic", f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "setsar=1", "format=yuv420p"]
            elif scale_key == "native":
                pass  # keine Filter
            else:
                filters_ref += ["setsar=1", "format=yuv420p"]; filters_dist += ["setsar=1", "format=yuv420p"]

            ref_f = ",".join(filters_ref) if filters_ref else "null"
            dist_f = ",".join(filters_dist) if filters_dist else "null"

            vmaf_opts = ["log_fmt=json", f"log_path={tmp_json.as_posix()}", f"n_threads={self.threads}"]
            if self.model_path: vmaf_opts.append(f"model=path={self.model_path}")
            if self.subsample and self.subsample > 1: vmaf_opts.append(f"subsample={self.subsample}")

            return f"[1:v]{ref_f}[ref];[0:v]{dist_f}[dist];[dist][ref]libvmaf={':'.join(vmaf_opts)}"

        @Slot()
        def run(self):
            try:
                self._abort_requested = False
                tmp_json = Path(tempfile.gettempdir()) / "vmaf_gui_result.json"
                if tmp_json.exists():
                    try: tmp_json.unlink()
                    except Exception: pass
                if not _check_libvmaf_available():
                    raise RuntimeError("ffmpeg ohne libvmaf – bitte passende ffmpeg-Version installieren.")
                lavfi = self._build_lavfi(tmp_json)

                dur: Optional[float] = None
                if self.start_sec is not None and self.end_sec is not None:
                    dur = max(0.0, float(self.end_sec) - float(self.start_sec))
                elif self.duration_sec is not None:
                    dur = max(0.0, float(self.duration_sec))

                cmd: List[str] = ["ffmpeg", "-hide_banner", "-nostats", "-y"]
                if self.start_sec is not None: cmd += ["-ss", f"{self.start_sec:.3f}"]
                if dur is not None and dur > 0: cmd += ["-t", f"{dur:.3f}"]
                cmd += ["-i", self.dist_path]

                if self.start_sec is not None: cmd += ["-ss", f"{self.start_sec:.3f}"]
                if dur is not None and dur > 0: cmd += ["-t", f"{dur:.3f}"]
                cmd += ["-i", self.ref_path]

                cmd += ["-lavfi", lavfi, "-f", "null", "-"]

                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                self._proc = proc
                assert proc.stderr is not None
                log_lines: List[str] = []
                interesting_tokens = ("frame=", "fps=", "libvmaf", "crop", "Error", "error", "WARNING", "warning")
                for raw_line in proc.stderr:
                    if not raw_line:
                        continue
                    line = raw_line.rstrip()
                    log_lines.append(line)
                    if any(tok in line for tok in interesting_tokens):
                        self.progress.emit(line)
                    if self._abort_requested:
                        break
                try:
                    rc = proc.wait(timeout=5 if self._abort_requested else None)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    rc = proc.wait()
                if self._abort_requested:
                    raise RuntimeError("Abbruch durch Benutzer")
                if rc != 0:
                    tail = "\n".join(log_lines[-20:]) if log_lines else "(keine ffmpeg-Ausgabe)"
                    raise RuntimeError(f"ffmpeg/libvmaf Fehler (rc={rc})\n{tail}")
                if not (Path(tmp_json).exists()):
                    raise RuntimeError("VMAF-JSON nicht gefunden – hat libvmaf geloggt?")
                data = json.loads(Path(tmp_json).read_text(encoding="utf-8"))
                self.finished.emit(parse_vmaf_data(data))
            except Exception as e:
                self.finished.emit(e)
            finally:
                self._proc = None

        def request_cancel(self) -> None:
            self._abort_requested = True
            proc = self._proc
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

                def _force_kill():
                    p = self._proc
                    if p and p.poll() is None:
                        try:
                            p.kill()
                        except Exception:
                            pass

                threading.Timer(5.0, _force_kill).start()

    class CropDetectWorker(QtCore.QObject):
        finished = Signal(object); progress = Signal(str)
        def __init__(self, ref_path: str, frames: int = 300, limit: int = 128, round_val: int = 16, reset: int = 0, stream: bool = False, parent: Optional[QtCore.QObject] = None):
            super().__init__(parent); self.ref_path = ref_path; self.frames = frames; self.limit = limit; self.round_val = round_val; self.reset = reset; self.stream = stream
        @Slot()
        def run(self):
            try:
                self.progress.emit(f"Starte cropdetect (limit={self.limit}, round={self.round_val}, reset={self.reset}) über {self.frames} Frames…")
                def _cb(t: Tuple[int,int,int,int]): self.progress.emit(f"Vorschlag: crop={t[0]}:{t[1]}:{t[2]}:{t[3]}")
                res = suggest_crop_via_ffmpeg(self.ref_path, frames=self.frames, limit=self.limit, round_val=self.round_val, reset=self.reset, stream=self.stream, progress_cb=_cb)
                self.finished.emit(res)
            except Exception as e:
                self.finished.emit(e)

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__(); self.setWindowTitle("VMAF GUI – Starter"); self.resize(1160, 800); self._build_ui()
            self.thread: Optional[QtCore.QThread] = None; self.worker: Optional[FfmpegWorker] = None
            self.crop_thread: Optional[QtCore.QThread] = None; self.crop_worker: Optional[CropDetectWorker] = None
            self._last_status_logged: str = ""
            self._set_status("Bereit.", clear_log=True)

        def _build_ui(self):
            central = QWidget(self); self.setCentralWidget(central); main_layout = QVBoxLayout(central)
            file_group = QGroupBox("Eingaben"); fg = QGridLayout(file_group)
            self.le_ref = QLineEdit(); self.le_dist = QLineEdit()
            btn_ref = QPushButton("Referenz wählen…"); btn_dist = QPushButton("Test/Distorted wählen…")
            btn_ref.clicked.connect(lambda: self._pick_file(self.le_ref)); btn_dist.clicked.connect(lambda: self._pick_file(self.le_dist))
            fg.addWidget(QLabel("Referenz (Original):"), 0, 0); fg.addWidget(self.le_ref, 0, 1); fg.addWidget(btn_ref, 0, 2)
            fg.addWidget(QLabel("Distorted (komprimiert):"), 1, 0); fg.addWidget(self.le_dist, 1, 1); fg.addWidget(btn_dist, 1, 2)
            self.lbl_ref_dims = QLabel("—"); self.lbl_ref_dims.setStyleSheet("color: gray")
            self.lbl_dist_dims = QLabel("—"); self.lbl_dist_dims.setStyleSheet("color: gray")
            fg.addWidget(QLabel("Ref. Abmessungen:"), 2, 0); fg.addWidget(self.lbl_ref_dims, 2, 1)
            fg.addWidget(QLabel("Dis. Abmessungen:"), 3, 0); fg.addWidget(self.lbl_dist_dims, 3, 1)

            params = QGroupBox("Parameter"); pg = QGridLayout(params)
            self.sb_crop_w = QSpinBox(); self.sb_crop_h = QSpinBox(); self.sb_crop_x = QSpinBox(); self.sb_crop_y = QSpinBox()
            for sb in (self.sb_crop_w, self.sb_crop_h, self.sb_crop_x, self.sb_crop_y):
                sb.setMaximum(16384); sb.setSpecialValueText("auto/leer"); sb.setMinimum(0)
            self.btn_suggest_crop = QPushButton("Auto-Crop vorschlagen"); self.btn_suggest_crop.clicked.connect(self.on_suggest_crop)
            self.le_start = QLineEdit(); self.le_end = QLineEdit()
            self.le_start.setPlaceholderText("Start (Sek. oder HH:MM:SS.mmm)"); self.le_end.setPlaceholderText("Ende (Sek. oder HH:MM:SS.mmm)")
            self.cb_scale = QComboBox(); self.cb_scale.addItems(["1080p", "Native", "1280x720", "3840x2160"])
            self.sb_threads = QSpinBox(); self.sb_threads.setRange(1, 128); self.sb_threads.setValue(max(1, os.cpu_count() or 8))
            self.le_model = QLineEdit(); btn_model = QPushButton("Modellpfad…"); btn_model.clicked.connect(self._pick_model)
            self.sb_subsample = QSpinBox(); self.sb_subsample.setRange(1, 20); self.sb_subsample.setValue(1)
            self.sb_crop_frames = QSpinBox(); self.sb_crop_frames.setRange(10, 5000); self.sb_crop_frames.setValue(300)
            self.sb_cd_limit = QSpinBox(); self.sb_cd_limit.setRange(0, 255); self.sb_cd_limit.setValue(128)
            self.sb_cd_round = QSpinBox(); self.sb_cd_round.setRange(1, 128); self.sb_cd_round.setValue(16)
            self.sb_cd_reset = QSpinBox(); self.sb_cd_reset.setRange(0, 10000); self.sb_cd_reset.setValue(0)
            self.cb_cd_live = QtWidgets.QCheckBox("Live-Log (experimentell)"); self.cb_cd_live.setChecked(False)

            pg.addWidget(QLabel("Crop (W,H,X,Y) – Native beschneidet beide:"), 0, 0)
            crop_layout = QHBoxLayout()
            for w in (self.sb_crop_w, self.sb_crop_h, self.sb_crop_x, self.sb_crop_y, self.btn_suggest_crop): crop_layout.addWidget(w)
            pg.addLayout(crop_layout, 0, 1, 1, 3)
            pg.addWidget(QLabel("Zeitfenster:"), 1, 0); time_layout = QHBoxLayout(); time_layout.addWidget(self.le_start); time_layout.addWidget(self.le_end)
            pg.addLayout(time_layout, 1, 1, 1, 3)
            pg.addWidget(QLabel("Skalierung:"), 2, 0); pg.addWidget(self.cb_scale, 2, 1)
            pg.addWidget(QLabel("Threads:"), 2, 2); pg.addWidget(self.sb_threads, 2, 3)
            pg.addWidget(QLabel("VMAF Modell (optional):"), 3, 0); pg.addWidget(self.le_model, 3, 1); pg.addWidget(btn_model, 3, 2)
            pg.addWidget(QLabel("Subsample:"), 4, 0); pg.addWidget(self.sb_subsample, 4, 1)
            pg.addWidget(QLabel("Auto-Crop über N Frames:"), 5, 0); pg.addWidget(self.sb_crop_frames, 5, 1)
            pg.addWidget(QLabel("Limit:"), 6, 0); pg.addWidget(self.sb_cd_limit, 6, 1)
            pg.addWidget(QLabel("Round:"), 6, 2); pg.addWidget(self.sb_cd_round, 6, 3)
            pg.addWidget(QLabel("Reset:"), 7, 2); pg.addWidget(self.sb_cd_reset, 7, 3)
            pg.addWidget(self.cb_cd_live, 7, 0, 1, 2)

            actions = QHBoxLayout(); self.btn_run = QPushButton("VMAF berechnen"); self.btn_run.clicked.connect(self.on_run)
            self.btn_cancel = QPushButton("Abbrechen"); self.btn_cancel.setEnabled(False); self.btn_cancel.clicked.connect(self.on_cancel)
            self.lbl_status = QLabel("Bereit."); self.lbl_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
            actions.addWidget(self.btn_run); actions.addWidget(self.btn_cancel); actions.addWidget(self.lbl_status, 1)

            out_group = QGroupBox("Ergebnis & Plot"); og = QVBoxLayout(out_group)
            self.lbl_mean = QLabel("Gesamt-VMAF: —"); f = self.lbl_mean.font(); f.setPointSize(f.pointSize() + 4); f.setBold(True); self.lbl_mean.setFont(f)
            self.canvas = MplCanvas(); og.addWidget(self.lbl_mean); og.addWidget(self.canvas, 2)
            og.addWidget(QLabel("Status / ffmpeg Ausgaben:"))
            self.log_view = QPlainTextEdit(); self.log_view.setReadOnly(True); self.log_view.setMaximumBlockCount(500)
            og.addWidget(self.log_view, 1)

            main_layout.addWidget(file_group); main_layout.addWidget(params); main_layout.addLayout(actions); main_layout.addWidget(out_group, 1)

            self.status_bar = QStatusBar(self); self.setStatusBar(self.status_bar); self.status_bar.showMessage("Bereit.")

            self.le_ref.textChanged.connect(lambda _: self._update_ref_dims()); self.le_ref.editingFinished.connect(self._update_ref_dims)
            self.le_dist.textChanged.connect(lambda _: self._update_dist_dims()); self.le_dist.editingFinished.connect(self._update_dist_dims)
            self._update_ref_dims(); self._update_dist_dims()

        def _pick_file(self, target: QLineEdit):
            path, _ = QFileDialog.getOpenFileName(self, "Videodatei wählen", str(Path.home()))
            if path:
                target.setText(path)
                if target is self.le_ref: self._update_ref_dims()
                elif target is self.le_dist: self._update_dist_dims()

        def _pick_model(self):
            path, _ = QFileDialog.getOpenFileName(self, "VMAF Modell (JSON)", str(Path.home()), "JSON (*.json);;Alle Dateien (*.*)")
            if path: self.le_model.setText(path)

        def _current_crop(self) -> Optional[Tuple[int, int, int, int]]:
            w = self.sb_crop_w.value()
            h = self.sb_crop_h.value()
            x = self.sb_crop_x.value()
            y = self.sb_crop_y.value()
            if w <= 0 or h <= 0:
                return None
            return (w, h, x, y)

        def _update_ref_dims(self):
            dims = probe_video_dimensions(self.le_ref.text().strip()); self.lbl_ref_dims.setText(f"{dims[0]}x{dims[1]}" if dims else "—")
        def _update_dist_dims(self):
            dims = probe_video_dimensions(self.le_dist.text().strip()); self.lbl_dist_dims.setText(f"{dims[0]}x{dims[1]}" if dims else "—")

        def _set_status(self, message: str, *, clear_log: bool = False, log: bool = True):
            if clear_log:
                self.log_view.clear(); self._last_status_logged = ""
            self.lbl_status.setText(message)
            if hasattr(self, "status_bar"):
                self.status_bar.showMessage(message)
            if log and message:
                if message != self._last_status_logged:
                    self.log_view.appendPlainText(message)
                    self._last_status_logged = message

        @Slot()
        def on_run(self):
            ref = self.le_ref.text().strip(); dist = self.le_dist.text().strip()
            if not ref or not dist:
                QMessageBox.warning(self, "Eingabe fehlt", "Bitte Referenz- und Distorted-Video wählen."); return
            if not Path(ref).exists() or not Path(dist).exists():
                QMessageBox.warning(self, "Datei nicht gefunden", "Eine der angegebenen Dateien existiert nicht."); return
            crop = self._current_crop(); scale_opt = self.cb_scale.currentText()
            threads = int(self.sb_threads.value()); model_path = self.le_model.text().strip() or None
            subsample = int(self.sb_subsample.value())

            start_sec = end_sec = duration_sec = None
            try:
                if self.le_start.text().strip(): start_sec = parse_time_to_seconds(self.le_start.text().strip())
                if self.le_end.text().strip(): end_sec = parse_time_to_seconds(self.le_end.text().strip())
            except Exception as e:
                QMessageBox.warning(self, "Zeitformat", f"Ungültige Zeitangabe: {e}"); return

            self.btn_run.setEnabled(False); self.btn_cancel.setEnabled(True)
            self._set_status("Berechne…", clear_log=True); self.lbl_mean.setText("Gesamt-VMAF: —"); self.canvas.plot_vmaf([])

            self.thread = QtCore.QThread(self)
            self.worker = FfmpegWorker(dist, ref, crop, scale_opt, threads, model_path, subsample, start_sec, end_sec, duration_sec)
            self.worker.moveToThread(self.thread)
            self.thread.started.connect(self.worker.run)
            self.worker.progress.connect(self.on_progress)
            self.worker.finished.connect(self.on_finished)
            self.worker.finished.connect(self.thread.quit)
            self.worker.finished.connect(self.worker.deleteLater)
            self.thread.finished.connect(self.thread.deleteLater)
            self.thread.finished.connect(self._cleanup_worker)
            self.thread.start()

        @Slot()
        def on_cancel(self):
            if not self.worker:
                return
            self.btn_cancel.setEnabled(False)
            self._set_status("Abbruch läuft…")
            try:
                self.worker.request_cancel()
            except Exception as exc:
                self.log_view.appendPlainText(f"Abbruch fehlgeschlagen: {exc}")

        @Slot()
        def on_suggest_crop(self):
            ref = self.le_ref.text().strip()
            if not ref:
                QMessageBox.information(self, "Referenz fehlt", "Bitte zuerst die Referenzdatei wählen."); return
            self.btn_suggest_crop.setEnabled(False); self._set_status("Auto-Crop läuft…")
            frames = int(self.sb_crop_frames.value()) if hasattr(self, 'sb_crop_frames') else 300
            if self.crop_thread is not None and self.crop_thread.isRunning():
                self.crop_thread.quit(); self.crop_thread.wait()
            self.crop_thread = QtCore.QThread(self)
            self.crop_worker = CropDetectWorker(ref, frames=frames, limit=int(self.sb_cd_limit.value()), round_val=int(self.sb_cd_round.value()), reset=int(self.sb_cd_reset.value()), stream=bool(self.cb_cd_live.isChecked()))
            self.crop_worker.moveToThread(self.crop_thread); self.crop_worker.progress.connect(self.on_progress)
            self.crop_thread.started.connect(self.crop_worker.run)
            self.crop_worker.finished.connect(self._on_crop_finished)
            self.crop_worker.finished.connect(self.crop_thread.quit)
            self.crop_thread.finished.connect(self.crop_worker.deleteLater)
            self.crop_thread.finished.connect(self._on_crop_thread_gone)
            self.crop_thread.start()

        @Slot(str)
        def on_progress(self, msg: str): self._set_status(msg)

        @Slot(object)
        def on_finished(self, result):
            self.btn_run.setEnabled(True)
            self.btn_cancel.setEnabled(False)
            if isinstance(result, Exception):
                msg = str(result)
                if "Abbruch durch Benutzer" in msg:
                    self._set_status("Abgebrochen")
                else:
                    QMessageBox.critical(self, "Fehler", msg)
                    self._set_status("Fehlgeschlagen", log=False)
                if msg:
                    self.log_view.appendPlainText(msg)
                return
            self.lbl_mean.setText(f"Gesamt-VMAF: {result.mean:.2f}" if result.mean is not None else "Gesamt-VMAF: (nicht gefunden)")
            self.canvas.plot_vmaf(result.frames); self._set_status("Fertig.")

        def _cleanup_worker(self):
            self.worker = None
            self.thread = None

        @QtCore.Slot(object)
        def _on_crop_finished(self, res):
            if isinstance(res, Exception):
                QMessageBox.warning(self, "cropdetect", str(res))
            else:
                w, h, x, y = res
                self.sb_crop_w.setValue(w); self.sb_crop_h.setValue(h); self.sb_crop_x.setValue(x); self.sb_crop_y.setValue(y)
                self.on_progress(f"Gewählter Crop: {w}:{h}:{x}:{y}")

        @QtCore.Slot()
        def _on_crop_thread_gone(self):
            self.btn_suggest_crop.setEnabled(True); self.crop_worker = None; self.crop_thread = None

    def run_gui() -> int:
        ok, missing, platform = _qt_preflight()
        if not ok:
            print("Qt kann nicht starten: Fehlende Systembibliotheken für Plattform '", platform, "': ", ", ".join(missing), sep="")
            if platform == "xcb":
                print("Ubuntu 22.04 Fix: sudo apt install -y libxcb-cursor0 libxkbcommon-x11-0")
            print("(Du kannst alternativ 'QT_QPA_PLATFORM=wayland' setzen, falls du auf Wayland bist.)")
            return 1
        if not os.environ.get("QT_QPA_PLATFORM"):
            os.environ["QT_QPA_PLATFORM"] = platform
        app = QApplication(sys.argv); w = MainWindow(); w.show(); return app.exec()
else:
    def run_gui() -> int:
        print("PySide6 ist nicht installiert. Starte CLI-Modus. (Tipp: 'pip install PySide6' für GUI)")
        return 0

def self_tests() -> int:
    print("[SelfTest] parse_vmaf_data (pooled)…", end=" ")
    fake = {"pooled_metrics": {"vmaf": {"mean": 92.34}}, "frames": [{"frameNum": 0, "metrics": {"vmaf": 95.0}}, {"frameNum": 1, "metrics": {"vmaf": 90.0}}]}
    res = parse_vmaf_data(fake); assert abs(res.mean - 92.34) < 1e-6 and len(res.frames) == 2; print("ok")
    print("[SelfTest] parse_vmaf_data (legacy aggregate)…", end=" ")
    fake_legacy = {"aggregate": {"VMAF_score": 88.5}, "frames": [{"frameNum": 0, "metrics": {"vmaf": 88.0}}, {"frameNum": 1, "metrics": {"vmaf": 89.0}}]}
    res2 = parse_vmaf_data(fake_legacy); assert abs(res2.mean - 88.5) < 1e-6 and len(res2.frames) == 2; print("ok")
    print("[SelfTest] parse_crop_str (valid)…", end=" ")
    assert parse_crop_str("1920:800:0:140") == (1920, 800, 0, 140); assert parse_crop_str("1920x800:0:140") == (1920, 800, 0, 140); print("ok")
    print("[SelfTest] parse_time_to_seconds …", end=" ")
    assert abs(parse_time_to_seconds("12.5") - 12.5) < 1e-6; assert abs(parse_time_to_seconds("01:02:03.5") - (1*3600+2*60+3.5)) < 1e-6; assert abs(parse_time_to_seconds("02:03") - (2*60+3)) < 1e-6; print("ok")
    print("SELFTEST OK"); return 0

def build_zip_path() -> Path:
    return Path("/mnt/data/vmaf_native_nofilters.zip")

def main() -> int:
    parser = build_arg_parser(); args = parser.parse_args()
    if args.selftest: return self_tests()
    if args.gui:
        if not GUI_AVAILABLE:
            print("PySide6 nicht verfügbar – bitte installieren oder CLI nutzen.")
            if not (args.dist and args.ref): return 0
        else:
            return run_gui()
    return run_cli(args)

if __name__ == "__main__":
    sys.exit(main())
