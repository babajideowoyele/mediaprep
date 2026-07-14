#!/usr/bin/env python3
"""
MEDIAPREP — a local "collect & prepare media" workbench.

Three tools behind one Carbon-styled UI, all running on your own machine:

  1. Transcribe      audio/video -> SRT / VTT / ELAN .eaf / TXT  (faster-whisper)
  2. Archive Check   inspect codecs/containers, flag against DANS preferred
                     formats, and convert to a preservation format  (ffmpeg)
  3. Scrub Metadata  strip EXIF/GPS from images and tags from A/V  (Pillow + ffmpeg)

Run:  python app.py   (then open http://127.0.0.1:7655)
Deps: ffmpeg/ffprobe, faster-whisper (transcribe), Pillow (image scrub).
Nothing is uploaded anywhere — you point the tools at local files or folders.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
import xml.sax.saxutils as sax
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent

# Some Anaconda/Windows setups export a broken SSL_CERT_FILE pointing at a file
# that doesn't exist, which breaks the Hugging Face model download that
# faster-whisper uses. Repair it (or drop it) so Transcribe works out of the box.
_cert = os.environ.get("SSL_CERT_FILE")
if _cert and not os.path.exists(_cert):
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        os.environ.pop("SSL_CERT_FILE", None)

PORT = 7655
DEFAULT_DIR = str(Path.home() / "Videos")
WORKERS = 2

MEDIA_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mts", ".mpg", ".mpeg",
              ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aif", ".aiff"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".jp2", ".gif", ".bmp", ".webp", ".heic", ".heif"}

# ── DANS preferred formats (dans.knaw.nl/en/file-formats) ─────────────
DANS_PREFERRED = {
    "video_containers": {"matroska", "mkv", "mxf"},          # + FFV1 video / FLAC audio inside
    "audio": {"flac", "opus", "bwf", "wav", "mka", "mxf"},
    "image_ext": {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".jp2", ".dcm"},
}
# common & fine to keep, but not on the "preferred" list
DANS_ACCEPTABLE = {
    "video_containers": {"mp4", "mov,mp4,m4a,3gp,3g2,mj2"},
    "audio": {"mp3", "aac", "m4a"},
    "image_ext": {".gif", ".bmp"},
}

# ── job registry / SSE / worker pool ──────────────────────────────────
JOBS, JOB_QUEUES, JOB_CTL, GLOBAL_SUBS = {}, {}, {}, []
TASK_Q = queue.Queue()
MODEL_CACHE = {}

class Cancelled(Exception):
    pass

def emit(job_id, **fields):
    job = JOBS.setdefault(job_id, {})
    job.update(fields)
    q = JOB_QUEUES.get(job_id)
    if q:
        q.put(dict(job))
    snap = dict(job); snap["job"] = job_id
    for gq in list(GLOBAL_SUBS):
        try: gq.put(snap)
        except Exception: pass

def worker_loop():
    while True:
        fn, job_id, kwargs = TASK_Q.get()
        try:
            ctl = JOB_CTL.get(job_id)
            if ctl and ctl["cancel"].is_set():
                emit(job_id, status="cancelled", message="Cancelled")
            else:
                fn(job_id=job_id, **kwargs)
        except Cancelled:
            emit(job_id, status="cancelled", message="Cancelled")
        except Exception as e:
            emit(job_id, status="error", message=str(e))
        finally:
            JOB_CTL.pop(job_id, None)
            TASK_Q.task_done()

def submit(fn, **kwargs):
    job_id = uuid.uuid4().hex[:12]
    JOB_QUEUES[job_id] = queue.Queue()
    JOB_CTL[job_id] = {"cancel": threading.Event()}
    JOBS[job_id] = {"status": "queued"}
    TASK_Q.put((fn, job_id, kwargs))
    return job_id

# ── ffprobe helpers ───────────────────────────────────────────────────
def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
                        "-of", "json", str(path)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError((r.stderr.strip().splitlines() or ["ffprobe failed"])[-1])
    return json.loads(r.stdout)

def duration_of(path):
    try:
        return float(ffprobe(path)["format"]["duration"])
    except Exception:
        return None

def run_proc(job_id, cancel, cmd, errlabel):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", errors="replace")
    JOB_CTL.setdefault(job_id, {})["proc"] = p
    _, err = p.communicate()
    if cancel.is_set():
        raise Cancelled()
    if p.returncode != 0:
        raise RuntimeError(f"{errlabel}: " + (err.strip().splitlines() or [""])[-1])

# ── env check ─────────────────────────────────────────────────────────
def check_env():
    problems = []
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        problems.append("ffmpeg/ffprobe not on PATH")
    try:
        import faster_whisper  # noqa
    except Exception:
        problems.append("faster-whisper not installed (pip install faster-whisper) — Transcribe disabled")
    try:
        import PIL  # noqa
    except Exception:
        problems.append("Pillow not installed (pip install Pillow) — image scrub disabled")
    return problems

def gpu_available():
    return shutil.which("nvidia-smi") is not None

# ════════════════════════════════════════════════════════════════════
# 1. TRANSCRIBE
# ════════════════════════════════════════════════════════════════════
def fmt_ts(seconds, comma=True):
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    sep = "," if comma else "."
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"

def write_srt(segs, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, s in enumerate(segs, 1):
            f.write(f"{i}\n{fmt_ts(s['start'])} --> {fmt_ts(s['end'])}\n{s['text'].strip()}\n\n")

def write_vtt(segs, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for s in segs:
            f.write(f"{fmt_ts(s['start'], False)} --> {fmt_ts(s['end'], False)}\n{s['text'].strip()}\n\n")

def write_txt(segs, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(s["text"].strip() for s in segs) + "\n")

def write_eaf(segs, path, media_path):
    """Minimal valid ELAN .eaf with a single transcription tier."""
    slots, entries = [], []
    def slot(ms):
        slots.append(ms); return f"ts{len(slots)}"
    ann = []
    for i, s in enumerate(segs, 1):
        a, b = slot(int(s["start"] * 1000)), slot(int(s["end"] * 1000))
        ann.append((i, a, b, s["text"].strip()))
    media_url = Path(media_path).as_uri()
    mime = "audio/x-wav"
    ext = Path(media_path).suffix.lower()
    if ext in {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm"}:
        mime = "video/mp4" if ext in {".mp4", ".m4v"} else "video/x-matroska"
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<ANNOTATION_DOCUMENT AUTHOR="MediaPrep" DATE="2026" FORMAT="3.0" VERSION="3.0" '
             'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
             'xsi:noNamespaceSchemaLocation="http://www.mpi.nl/tools/elan/EAFv3.0.xsd">',
             f'  <HEADER MEDIA_FILE="" TIME_UNITS="milliseconds">',
             f'    <MEDIA_DESCRIPTOR MEDIA_URL="{sax.quoteattr(media_url)[1:-1]}" MIME_TYPE="{mime}"/>',
             '  </HEADER>',
             '  <TIME_ORDER>']
    for i, ms in enumerate(slots, 1):
        lines.append(f'    <TIME_SLOT TIME_SLOT_ID="ts{i}" TIME_VALUE="{ms}"/>')
    lines.append('  </TIME_ORDER>')
    lines.append('  <TIER LINGUISTIC_TYPE_REF="default-lt" TIER_ID="transcription">')
    for i, a, b, text in ann:
        lines.append(f'    <ANNOTATION><ALIGNABLE_ANNOTATION ANNOTATION_ID="a{i}" '
                     f'TIME_SLOT_REF1="{a}" TIME_SLOT_REF2="{b}">'
                     f'<ANNOTATION_VALUE>{sax.escape(text)}</ANNOTATION_VALUE>'
                     f'</ALIGNABLE_ANNOTATION></ANNOTATION>')
    lines.append('  </TIER>')
    lines.append('  <LINGUISTIC_TYPE GRAPHIC_REFERENCES="false" LINGUISTIC_TYPE_ID="default-lt" TIME_ALIGNABLE="true"/>')
    lines.append('</ANNOTATION_DOCUMENT>')
    Path(path).write_text("\n".join(lines), encoding="utf-8")

def transcribe(job_id, path, model_size, language, formats, device, compute_type):
    from faster_whisper import WhisperModel
    ctl = JOB_CTL.setdefault(job_id, {}); cancel = ctl.setdefault("cancel", threading.Event())
    src = Path(path)
    if not src.exists():
        raise RuntimeError(f"file not found: {src}")
    emit(job_id, status="running", stage="load", percent=None,
         message=f"Loading {model_size} model ({device})…", file=src.name)

    key = (model_size, device, compute_type)
    model = MODEL_CACHE.get(key)
    if model is None:
        model = WhisperModel(model_size, device=device, compute_type=compute_type)
        MODEL_CACHE[key] = model

    dur = duration_of(src) or 0
    emit(job_id, status="running", stage="transcribe", percent=0, message="Transcribing…")
    seg_iter, info = model.transcribe(str(src), language=(language or None), vad_filter=True)
    segs = []
    for s in seg_iter:
        if cancel.is_set():
            raise Cancelled()
        segs.append({"start": s.start, "end": s.end, "text": s.text})
        pct = min(99.0, (s.end / dur * 100) if dur else 0)
        emit(job_id, stage="transcribe", percent=round(pct, 1),
             message="Transcribing…", detail=f"{len(segs)} segments · {fmt_ts(s.end, False)}")

    out = []
    stem = src.with_suffix("")
    if "srt" in formats: write_srt(segs, str(stem) + ".srt"); out.append(str(stem) + ".srt")
    if "vtt" in formats: write_vtt(segs, str(stem) + ".vtt"); out.append(str(stem) + ".vtt")
    if "txt" in formats: write_txt(segs, str(stem) + ".txt"); out.append(str(stem) + ".txt")
    if "eaf" in formats: write_eaf(segs, str(stem) + ".eaf", str(src)); out.append(str(stem) + ".eaf")

    emit(job_id, status="done", stage="done", percent=100,
         message=f"Transcribed — {len(segs)} segments, language {info.language}",
         files=out, outdir=str(src.parent))

# ════════════════════════════════════════════════════════════════════
# 2. ARCHIVE CHECK
# ════════════════════════════════════════════════════════════════════
def classify(path):
    p = Path(path); ext = p.suffix.lower()
    if ext in IMAGE_EXTS:
        status = "preferred" if ext in DANS_PREFERRED["image_ext"] else (
            "acceptable" if ext in DANS_ACCEPTABLE["image_ext"] else "review")
        return {"kind": "image", "container": ext.lstrip("."), "vcodec": None, "acodec": None,
                "status": status, "note": _img_note(status)}
    info = ffprobe(path)
    fmt = info.get("format", {}).get("format_name", "")
    vstreams = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
    astreams = [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]
    vcodec = vstreams[0].get("codec_name") if vstreams else None
    acodec = astreams[0].get("codec_name") if astreams else None
    is_video = bool(vstreams) and vcodec not in ("mjpeg", "png", "bmp")  # cover art -> audio
    fmt_set = set(fmt.split(","))
    if is_video:
        if fmt_set & DANS_PREFERRED["video_containers"]:
            status = "preferred"
        elif fmt_set & DANS_ACCEPTABLE["video_containers"] or "mp4" in fmt:
            status = "acceptable"
        else:
            status = "review"
        note = {"preferred": "DANS-preferred container.",
                "acceptable": "Widely accepted; not on the DANS preferred list — remux to Matroska for preservation.",
                "review": "Not a common preservation container — convert recommended."}[status]
        return {"kind": "video", "container": fmt, "vcodec": vcodec, "acodec": acodec,
                "status": status, "note": note}
    # audio
    if (fmt_set & DANS_PREFERRED["audio"]) or (acodec in DANS_PREFERRED["audio"]):
        status = "preferred"
    elif (fmt_set & DANS_ACCEPTABLE["audio"]) or (acodec in DANS_ACCEPTABLE["audio"]):
        status = "acceptable"
    else:
        status = "review"
    note = {"preferred": "DANS-preferred audio format.",
            "acceptable": "Accepted, but FLAC is the preferred archival choice.",
            "review": "Uncommon audio format — convert to FLAC recommended."}[status]
    return {"kind": "audio", "container": fmt, "vcodec": None, "acodec": acodec,
            "status": status, "note": note}

def _img_note(status):
    return {"preferred": "DANS-preferred image format.",
            "acceptable": "Accepted; TIFF/PNG/JP2 are preferred for preservation.",
            "review": "Uncommon image format — convert to TIFF or PNG recommended."}[status]

def list_media(target):
    p = Path(target)
    if p.is_file():
        return [p]
    if p.is_dir():
        return sorted([f for f in p.rglob("*")
                       if f.is_file() and f.suffix.lower() in (MEDIA_EXTS | IMAGE_EXTS)])
    return []

def inspect(target):
    files = list_media(target)
    rows = []
    for f in files[:500]:
        try:
            c = classify(f)
        except Exception as e:
            c = {"kind": "?", "container": "?", "vcodec": None, "acodec": None,
                 "status": "review", "note": f"could not read: {e}"}
        c.update({"path": str(f), "name": f.name, "size": f.stat().st_size})
        rows.append(c)
    return rows

def convert(job_id, path, target):
    ctl = JOB_CTL.setdefault(job_id, {}); cancel = ctl.setdefault("cancel", threading.Event())
    src = Path(path)
    if not src.exists():
        raise RuntimeError(f"file not found: {src}")
    emit(job_id, status="running", stage="convert", percent=None,
         message=f"Converting to {target}…", file=src.name)
    if target == "mkv-ffv1":   # lossless preservation master
        out = src.with_name(src.stem + "_preservation.mkv")
        cmd = ["ffmpeg", "-y", "-i", str(src), "-map", "0",
               "-c:v", "ffv1", "-level", "3", "-c:a", "flac", str(out)]
    elif target == "mkv-remux":  # preferred container, keep codecs (fast)
        out = src.with_name(src.stem + "_remux.mkv")
        cmd = ["ffmpeg", "-y", "-i", str(src), "-map", "0", "-c", "copy", str(out)]
    elif target == "flac":
        out = src.with_name(src.stem + ".flac")
        cmd = ["ffmpeg", "-y", "-i", str(src), "-c:a", "flac", str(out)]
    elif target == "tiff":
        out = src.with_name(src.stem + ".tiff")
        cmd = ["ffmpeg", "-y", "-i", str(src), str(out)]
    elif target == "png":
        out = src.with_name(src.stem + "_conv.png")
        cmd = ["ffmpeg", "-y", "-i", str(src), str(out)]
    else:
        raise RuntimeError(f"unknown target: {target}")
    run_proc(job_id, cancel, cmd, "ffmpeg convert failed")
    emit(job_id, status="done", stage="done", percent=100,
         message="Converted", files=[str(out)], outdir=str(src.parent))

# ════════════════════════════════════════════════════════════════════
# 3. SCRUB METADATA
# ════════════════════════════════════════════════════════════════════
GPS_TAGS = {"GPSInfo", "GPSLatitude", "GPSLongitude", "GPSAltitude"}

def scan_meta(target):
    files = list_media(target)
    rows = []
    for f in files[:500]:
        ext = f.suffix.lower()
        found, has_gps = [], False
        try:
            if ext in IMAGE_EXTS:
                from PIL import Image, ExifTags
                img = Image.open(f)
                exif = img.getexif()
                for tag_id, val in exif.items():
                    name = ExifTags.TAGS.get(tag_id, str(tag_id))
                    if name == "GPSInfo" or "GPS" in name:
                        has_gps = True
                    found.append(name)
                # dedicated GPS IFD
                try:
                    if exif.get_ifd(ExifTags.IFD.GPSInfo):
                        has_gps = True; found.append("GPSInfo")
                except Exception:
                    pass
                kind = "image"
            else:
                info = ffprobe(f)
                tags = {}
                tags.update(info.get("format", {}).get("tags", {}) or {})
                for s in info.get("streams", []):
                    tags.update(s.get("tags", {}) or {})
                for k in tags:
                    found.append(k)
                    if "location" in k.lower() or "gps" in k.lower():
                        has_gps = True
                kind = "video" if any(s.get("codec_type") == "video" for s in info.get("streams", [])) else "audio"
        except Exception as e:
            found = [f"error: {e}"]; kind = "?"
        rows.append({"path": str(f), "name": f.name, "kind": kind,
                     "count": len([x for x in found if not str(x).startswith("error")]),
                     "has_gps": has_gps, "tags": sorted(set(found))[:40]})
    return rows

def scrub(job_id, target, overwrite=False):
    ctl = JOB_CTL.setdefault(job_id, {}); cancel = ctl.setdefault("cancel", threading.Event())
    files = list_media(target)
    if not files:
        raise RuntimeError("no media/image files found at that path")
    emit(job_id, status="running", stage="scrub", percent=0,
         message=f"Scrubbing {len(files)} file(s)…")
    out = []
    for i, f in enumerate(files):
        if cancel.is_set():
            raise Cancelled()
        ext = f.suffix.lower()
        dst = f if overwrite else f.with_name(f.stem + "_clean" + f.suffix)
        try:
            if ext in IMAGE_EXTS:
                from PIL import Image
                img = Image.open(f)
                clean = Image.new(img.mode, img.size)
                clean.putdata(list(img.getdata()))   # pixels only — no EXIF/GPS/thumbnail
                if overwrite:
                    tmp = f.with_name(f.stem + "_tmp" + f.suffix)
                    clean.save(tmp); tmp.replace(f)
                else:
                    clean.save(dst)
            else:
                tmp = f.with_name(f.stem + "_tmp" + f.suffix)
                run_proc(job_id, cancel,
                         ["ffmpeg", "-y", "-i", str(f), "-map_metadata", "-1",
                          "-map_chapters", "-1", "-c", "copy", str(tmp)],
                         "ffmpeg scrub failed")
                if overwrite:
                    tmp.replace(f)
                else:
                    tmp.replace(dst)
            out.append(str(dst))
        except Exception as e:
            emit(job_id, stage="scrub", message=f"skip {f.name}: {e}")
        emit(job_id, stage="scrub", percent=round((i + 1) / len(files) * 100, 1),
             detail=f"{i+1}/{len(files)}")
    emit(job_id, status="done", stage="done", percent=100,
         message=f"Scrubbed {len(out)} file(s)", files=out,
         outdir=str(Path(files[0]).parent))

# ════════════════════════════════════════════════════════════════════
# HTTP layer
# ════════════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            html = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        elif parsed.path == "/api/env":
            self._json({"problems": check_env(), "gpu": gpu_available(),
                        "default_dir": DEFAULT_DIR})
        elif parsed.path == "/api/events":
            self._sse(parse_qs(parsed.query).get("job", [""])[0])
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/inspect":
                self._json({"rows": inspect(self._read()["path"].strip())})
            elif parsed.path == "/api/scan-meta":
                self._json({"rows": scan_meta(self._read()["path"].strip())})
            elif parsed.path == "/api/transcribe":
                d = self._read()
                job = submit(transcribe, path=d["path"].strip(),
                             model_size=d.get("model") or "small",
                             language=d.get("language") or None,
                             formats=d.get("formats") or ["srt"],
                             device=d.get("device") or "cpu",
                             compute_type=d.get("compute_type") or ("float16" if d.get("device") == "cuda" else "int8"))
                self._json({"job": job})
            elif parsed.path == "/api/convert":
                d = self._read()
                self._json({"job": submit(convert, path=d["path"].strip(), target=d["target"])})
            elif parsed.path == "/api/scrub":
                d = self._read()
                self._json({"job": submit(scrub, target=d["path"].strip(),
                                          overwrite=bool(d.get("overwrite")))})
            elif parsed.path == "/api/cancel":
                ctl = JOB_CTL.get(self._read().get("job"))
                if ctl:
                    ctl.get("cancel") and ctl["cancel"].set()
                    p = ctl.get("proc")
                    if p and p.poll() is None:
                        try: p.terminate()
                        except Exception: pass
                self._json({"ok": True})
            elif parsed.path == "/api/open":
                target = self._read().get("path") or DEFAULT_DIR
                p = Path(target); folder = str(p if p.is_dir() else p.parent)
                if sys.platform == "win32": os.startfile(folder)
                elif sys.platform == "darwin": subprocess.Popen(["open", folder])
                else: subprocess.Popen(["xdg-open", folder])
                self._json({"ok": True})
            else:
                self.send_error(404)
        except Exception as e:
            self._json({"error": str(e)}, code=400)

    def _sse(self, job_id):
        q = JOB_QUEUES.get(job_id)
        if q is None:
            self.send_error(404); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self._w(JOBS.get(job_id, {}))
            while True:
                try:
                    st = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n"); self.wfile.flush(); continue
                self._w(st)
                if st.get("status") in ("done", "error", "cancelled"):
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _w(self, st):
        self.wfile.write(f"data: {json.dumps(st)}\n\n".encode("utf-8")); self.wfile.flush()


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    probs = check_env()
    print("-" * 52)
    print("  MEDIAPREP  ->  http://127.0.0.1:%d" % PORT)
    print("  GPU: %s" % ("yes" if gpu_available() else "no (CPU transcription)"))
    if probs:
        print("  [!] notes:")
        for p in probs:
            print("     -", p)
    print("-" * 52)
    for _ in range(WORKERS):
        threading.Thread(target=worker_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye.")


if __name__ == "__main__":
    main()
