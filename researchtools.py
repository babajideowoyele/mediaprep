"""
Research-workflow prototypes for MediaPrep.

  unchunkle   merge ELAN .eaf files made on Chunkle pieces into one file for the full recording
  frame rate  detect variable frame rate and convert to a constant frame rate
  sync        find the time offset between two recordings from their audio
  redact      replace names, places and other terms in transcripts (SRT/VTT/TXT/EAF)
  agreement   compare two coders' ELAN tiers (time-sliced Cohen's kappa + event matching)

Prototypes: useful and tested on examples, but check results before relying on them.
Only the Python standard library and ffmpeg are required; Sync also uses numpy.
"""

import csv
import re
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

# ── ELAN helpers ───────────────────────────────────────────────────────
def read_eaf(path):
    """Return (tree, root, slots{id: ms or None}, tiers{tier_id: [annotation dict]})."""
    tree = ET.parse(path)
    root = tree.getroot()
    slots = {}
    for ts in root.iter("TIME_SLOT"):
        v = ts.get("TIME_VALUE")
        slots[ts.get("TIME_SLOT_ID")] = int(v) if v is not None else None
    tiers = {}
    for tier in root.iter("TIER"):
        anns = []
        for a in tier.iter("ALIGNABLE_ANNOTATION"):
            val = a.findtext("ANNOTATION_VALUE") or ""
            anns.append({"id": a.get("ANNOTATION_ID"), "start": slots.get(a.get("TIME_SLOT_REF1")),
                         "end": slots.get(a.get("TIME_SLOT_REF2")), "value": val, "ref": None})
        for a in tier.iter("REF_ANNOTATION"):
            anns.append({"id": a.get("ANNOTATION_ID"), "start": None, "end": None,
                         "value": a.findtext("ANNOTATION_VALUE") or "", "ref": a.get("ANNOTATION_REF")})
        tiers[tier.get("TIER_ID")] = anns
    return tree, root, slots, tiers


# ════════════════════════════════════════════════════════════════════
# UNCHUNKLE
# ════════════════════════════════════════════════════════════════════
def unchunkle(chunk_dir, out_name=None):
    """Merge <piece>.eaf files listed in chunks.csv into one .eaf on the full timeline.

    Every alignable annotation is shifted by its piece's start time; tiers with the
    same TIER_ID are combined; linguistic types, constraints and vocabularies are
    taken from the first file that defines them. Returns (out_path, report).
    """
    d = Path(chunk_dir)
    man = d / "chunks.csv"
    if not man.exists():
        raise RuntimeError("no chunks.csv in that folder (cut the recording with Chunkle first)")
    rows = list(csv.DictReader(open(man, encoding="utf-8")))
    source = None
    readme = d / "README.txt"
    if readme.exists():
        m = re.search(r"Chunks of: (.+)", readme.read_text(encoding="utf-8"))
        if m:
            source = d.parent / m.group(1).strip()

    out_root = ET.Element("ANNOTATION_DOCUMENT", {
        "AUTHOR": "MediaPrep Unchunkle", "DATE": "2026-01-01T00:00:00+00:00", "FORMAT": "3.0", "VERSION": "3.0",
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:noNamespaceSchemaLocation": "http://www.mpi.nl/tools/elan/EAFv3.0.xsd"})
    header = ET.SubElement(out_root, "HEADER", {"MEDIA_FILE": "", "TIME_UNITS": "milliseconds"})
    if source is not None:
        ext = source.suffix.lower()
        mime = "video/mp4" if ext in {".mp4", ".m4v", ".mov"} else ("audio/x-wav" if ext == ".wav" else "video/x-matroska")
        ET.SubElement(header, "MEDIA_DESCRIPTOR", {"MEDIA_URL": source.resolve().as_uri(), "MIME_TYPE": mime,
                                                   "RELATIVE_MEDIA_URL": "../" + source.name})
    time_order = ET.SubElement(out_root, "TIME_ORDER")
    out_tiers, extras, seen_extra = {}, [], set()
    slot_n = ann_n = 0
    used, missing = [], []

    def new_slot(ms):
        nonlocal slot_n
        slot_n += 1
        attrs = {"TIME_SLOT_ID": f"ts{slot_n}"}
        if ms is not None:
            attrs["TIME_VALUE"] = str(ms)
        ET.SubElement(time_order, "TIME_SLOT", attrs)
        return f"ts{slot_n}"

    for r in rows:
        eaf = d / (Path(r["file"]).stem + ".eaf")
        if not eaf.exists():
            missing.append(eaf.name); continue
        used.append(eaf.name)
        offset = int(round(float(r["start_seconds"]) * 1000))
        _, root, slots, _ = read_eaf(eaf)
        slot_map = {sid: new_slot(None if ms is None else ms + offset) for sid, ms in slots.items()}
        id_map = {}
        for tier in root.iter("TIER"):
            tid = tier.get("TIER_ID")
            if tid not in out_tiers:
                out_tiers[tid] = ET.Element("TIER", dict(tier.attrib))
            dst = out_tiers[tid]
            for ann in tier.findall("ANNOTATION"):
                child = list(ann)[0]
                ann_n += 1
                id_map[child.get("ANNOTATION_ID")] = f"a{ann_n}"
            for ann in tier.findall("ANNOTATION"):
                child = list(ann)[0]
                new_ann = ET.SubElement(dst, "ANNOTATION")
                attrs = dict(child.attrib)
                attrs["ANNOTATION_ID"] = id_map[child.get("ANNOTATION_ID")]
                if child.tag == "ALIGNABLE_ANNOTATION":
                    attrs["TIME_SLOT_REF1"] = slot_map[child.get("TIME_SLOT_REF1")]
                    attrs["TIME_SLOT_REF2"] = slot_map[child.get("TIME_SLOT_REF2")]
                else:
                    attrs["ANNOTATION_REF"] = id_map.get(child.get("ANNOTATION_REF"), child.get("ANNOTATION_REF"))
                    if child.get("PREVIOUS_ANNOTATION"):
                        attrs["PREVIOUS_ANNOTATION"] = id_map.get(child.get("PREVIOUS_ANNOTATION"))
                nc = ET.SubElement(new_ann, child.tag, attrs)
                ET.SubElement(nc, "ANNOTATION_VALUE").text = child.findtext("ANNOTATION_VALUE") or ""
        for el in root:
            if el.tag in ("LINGUISTIC_TYPE", "CONSTRAINT", "CONTROLLED_VOCABULARY", "LANGUAGE", "LOCALE"):
                key = (el.tag, el.get("LINGUISTIC_TYPE_ID") or el.get("STEREOTYPE") or el.get("CV_ID")
                       or el.get("LANG_ID") or el.get("LANGUAGE_CODE"))
                if key not in seen_extra:
                    seen_extra.add(key); extras.append(el)

    if not used:
        raise RuntimeError("no .eaf files found next to the pieces (expected e.g. "
                           + Path(rows[0]["file"]).stem + ".eaf)")
    for t in out_tiers.values():
        out_root.append(t)
    order = {"LINGUISTIC_TYPE": 0, "LOCALE": 1, "LANGUAGE": 2, "CONSTRAINT": 3, "CONTROLLED_VOCABULARY": 4}
    for el in sorted(extras, key=lambda e: order.get(e.tag, 9)):
        out_root.append(el)
    out = d / (out_name or ((source.stem if source else d.name) + "_merged.eaf"))
    ET.indent(out_root)
    ET.ElementTree(out_root).write(out, encoding="UTF-8", xml_declaration=True)
    return out, {"merged": used, "missing": missing, "tiers": list(out_tiers), "annotations": ann_n}


# ════════════════════════════════════════════════════════════════════
# FRAME RATE
# ════════════════════════════════════════════════════════════════════
def _ratio(s):
    try:
        n, dd = s.split("/")
        return float(n) / float(dd) if float(dd) else None
    except Exception:
        return None

def frame_rate_report(ffprobe, path):
    info = ffprobe(path)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError("no video stream in this file")
    r, avg = _ratio(v.get("r_frame_rate", "")), _ratio(v.get("avg_frame_rate", ""))
    variable = bool(r and avg and abs(r - avg) / max(r, avg) > 0.01)
    target = round(avg or r or 25)
    for std in (24, 25, 30, 50, 60):
        if avg and abs(avg - std) < 1.0:
            target = std
    return {"r_frame_rate": round(r, 3) if r else None, "avg_frame_rate": round(avg, 3) if avg else None,
            "variable": variable, "suggested_fps": target,
            "note": ("Likely variable frame rate: annotations in ELAN may drift. Convert to a constant rate."
                     if variable else "Constant frame rate: no conversion needed.")}

def to_constant_fps_cmd(src, fps):
    src = Path(src)
    out = src.with_name(f"{src.stem}_cfr{int(fps)}.mp4")
    return out, ["ffmpeg", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                 "-vf", f"fps={fps}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]


# ════════════════════════════════════════════════════════════════════
# SYNC
# ════════════════════════════════════════════════════════════════════
SYNC_SR = 8000

def _audio(path, seconds):
    import numpy as np
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-t", str(seconds), "-vn", "-ac", "1",
                        "-ar", str(SYNC_SR), "-f", "s16le", "-"], capture_output=True)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError(f"could not read audio from {Path(path).name}")
    x = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)
    x -= x.mean()
    return x / (np.abs(x).max() + 1e-9)

def find_offset(reference, other, seconds=180):
    """Seconds by which `other` starts later than `reference` (negative: earlier)."""
    try:
        import numpy as np
    except ImportError:
        raise RuntimeError("Sync needs numpy: pip install numpy")
    a, b = _audio(reference, seconds), _audio(other, seconds)
    # onset envelope: robust to different microphones and levels
    def env(x):
        hop = SYNC_SR // 100
        frames = x[: len(x) // hop * hop].reshape(-1, hop)
        e = np.log1p(np.abs(frames).mean(axis=1) * 100)
        d = np.diff(e, prepend=e[0]); d[d < 0] = 0
        return (d - d.mean()) / (d.std() + 1e-9)
    ea, eb = env(a), env(b)
    n = 1 << int(np.ceil(np.log2(len(ea) + len(eb))))
    corr = np.fft.irfft(np.fft.rfft(ea, n) * np.conj(np.fft.rfft(eb, n)), n)
    corr = np.concatenate([corr[-(len(eb) - 1):], corr[: len(ea)]])
    lag = int(np.argmax(corr)) - (len(eb) - 1)          # in 10 ms frames
    peak = float(corr.max())
    conf = (peak - float(np.median(corr))) / (float(np.std(corr)) + 1e-9)
    return {"offset_seconds": round(-lag / 100.0, 3), "confidence": round(conf, 1),
            "reliable": conf > 8,
            "meaning": "positive: the second recording starts later than the reference"}

def aligned_copy_cmd(reference, other, offset):
    """Trim the start of whichever recording began earlier, so both copies start together."""
    early = Path(reference) if offset >= 0 else Path(other)
    out = early.with_name(f"{early.stem}_synced.mp4")
    cmd = ["ffmpeg", "-y", "-ss", f"{abs(offset):.3f}", "-i", str(early), "-map", "0:v:0?", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    return out, cmd


# ════════════════════════════════════════════════════════════════════
# REDACT
# ════════════════════════════════════════════════════════════════════
COMMON = set("""I A The This That These Those It We You He She They My Our Your His Her Their
Yes No Okay OK So And But Or If When Then Well Oh Um Uh Hmm Mm Yeah Right Now Here There What Why How Who
Monday Tuesday Wednesday Thursday Friday Saturday Sunday January February March April May June July August
September October November December Mr Mrs Ms Dr""".split())

def read_transcript_text(path):
    p = Path(path)
    if p.suffix.lower() == ".eaf":
        _, _, _, tiers = read_eaf(p)
        return "\n".join(a["value"] for anns in tiers.values() for a in anns)
    return p.read_text(encoding="utf-8", errors="replace")

def suggest_terms(path, limit=60):
    """Capitalised words that are not sentence-initial and not common words: likely names/places."""
    text = read_transcript_text(path)
    text = re.sub(r"\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}", " ", text)
    counts = Counter()
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
        words = re.findall(r"\b[\w'’-]+\b", sent)
        for w in words[1:]:
            if w[:1].isupper() and w not in COMMON and not w.isupper() and len(w) > 1 and not w[0].isdigit():
                counts[w] += 1
    return [{"term": t, "count": c} for t, c in counts.most_common(limit)]

def redact(path, terms, replacement="[REDACTED]"):
    terms = sorted({t.strip() for t in terms if t.strip()}, key=len, reverse=True)
    if not terms:
        raise RuntimeError("add at least one term to redact")
    rx = re.compile(r"(?<![\w])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w])", re.IGNORECASE)
    p = Path(path)
    counts = Counter()
    def sub(text):
        def f(m):
            counts[m.group(0).lower()] += 1
            return replacement
        return rx.sub(f, text)
    out = p.with_name(f"{p.stem}_redacted{p.suffix}")
    if p.suffix.lower() == ".eaf":
        tree = ET.parse(p)
        for v in tree.getroot().iter("ANNOTATION_VALUE"):
            if v.text:
                v.text = sub(v.text)
        tree.write(out, encoding="UTF-8", xml_declaration=True)
    else:
        out.write_text(sub(p.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
    return out, {"replacements": sum(counts.values()), "terms_found": len(counts),
                 "terms_not_found": len([t for t in terms if t.lower() not in counts])}


# ════════════════════════════════════════════════════════════════════
# AGREEMENT
# ════════════════════════════════════════════════════════════════════
def eaf_tiers(path):
    _, _, _, tiers = read_eaf(path)
    return {t: len([a for a in anns if a["start"] is not None]) for t, anns in tiers.items()}

def agreement(eaf_a, tier_a, eaf_b, tier_b, step_ms=100, min_overlap=0.5):
    def spans(path, tier):
        _, _, _, tiers = read_eaf(path)
        if tier not in tiers:
            raise RuntimeError(f"tier '{tier}' not found in {Path(path).name}")
        return sorted([(a["start"], a["end"], a["value"].strip()) for a in tiers[tier]
                       if a["start"] is not None and a["end"] is not None and a["end"] > a["start"]])
    A, B = spans(eaf_a, tier_a), spans(eaf_b, tier_b)
    if not A and not B:
        raise RuntimeError("both tiers are empty")
    end = max([e for _, e, _ in A + B])
    GAP = "(none)"
    def labels(S):
        lab = [GAP] * (end // step_ms + 1)
        for s, e, v in S:
            for i in range(s // step_ms, min(e // step_ms, len(lab))):
                lab[i] = v or "(empty)"
        return lab
    la, lb = labels(A), labels(B)
    n = len(la)
    po = sum(x == y for x, y in zip(la, lb)) / n
    ca, cb = Counter(la), Counter(lb)
    pe = sum(ca[k] * cb.get(k, 0) for k in ca) / (n * n)
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
    # event matching: greedy one-to-one by largest overlap
    def ov(x, y):
        return max(0, min(x[1], y[1]) - max(x[0], y[0]))
    used, matched, same_label = set(), 0, 0
    for a in A:
        best, bj = 0, None
        for j, b in enumerate(B):
            if j in used:
                continue
            o = ov(a, b)
            if o > best:
                best, bj = o, j
        if bj is not None and best >= min_overlap * min(a[1] - a[0], B[bj][1] - B[bj][0]):
            used.add(bj); matched += 1
            same_label += a[2] == B[bj][2]
    cats = sorted(set(la) | set(lb))
    confusion = {x: {y: 0 for y in cats} for x in cats}
    for x, y in zip(la, lb):
        confusion[x][y] += 1
    return {"time_units": n, "step_ms": step_ms,
            "percent_agreement": round(po * 100, 1), "cohens_kappa": round(kappa, 3),
            "events_a": len(A), "events_b": len(B), "events_matched": matched,
            "matched_same_label": same_label,
            "event_recall_vs_a": round(matched / len(A) * 100, 1) if A else None,
            "categories": cats,
            "confusion_seconds": {x: {y: round(v * step_ms / 1000, 1) for y, v in r.items()} for x, r in confusion.items()}}
