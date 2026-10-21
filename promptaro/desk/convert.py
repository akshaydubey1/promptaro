# Copyright (c) 2026 Akshay Dubey. All rights reserved. Proprietary and confidential; see LICENSE.
"""Local file tools: PDFs, image/audio/video/document conversion. Author: Akshay Dubey

Everything runs on this machine — no file leaves it. Heavy lifting comes from
tools that are already here or bundled with the app: Pillow (images), pypdf
(merging), reportlab (text -> PDF), imageio-ffmpeg's bundled ffmpeg (audio and
video), macOS textutil or pandoc (documents). If a conversion isn't possible
with what's installed, the error says exactly what's missing.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
import wave
from io import BytesIO
from pathlib import Path

IMG_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "tif", "ico"}
AUDIO_EXTS = {"mp3", "wav", "m4a", "aac", "ogg", "flac", "opus", "wma", "aiff"}
VIDEO_EXTS = {"mp4", "mov", "mkv", "webm", "avi", "m4v", "mpg", "mpeg", "ts", "3gp"}
DOC_EXTS = {"txt", "md", "rtf", "doc", "docx", "html", "htm", "odt"}

_JPEG_SAVABLE = {"jpg", "jpeg", "webp", "bmp", "tiff", "tif"}
_PIL_FMT = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP", "gif": "GIF",
            "bmp": "BMP", "tiff": "TIFF", "tif": "TIFF", "ico": "ICO"}
_TEXTUTIL_FMT = {"txt": "txt", "rtf": "rtf", "doc": "doc", "docx": "docx",
                 "html": "html", "odt": "odt", "md": "txt"}


def _have(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


def ffmpeg() -> str | None:
    """The bundled ffmpeg first (no install needed), then a system one."""
    if _have("imageio_ffmpeg"):
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    return shutil.which("ffmpeg")


def capabilities() -> dict:
    return {
        "pillow": _have("PIL"),
        "pypdf": _have("pypdf"),
        "reportlab": _have("reportlab"),
        "ffmpeg": bool(ffmpeg()),
        "pandoc": bool(shutil.which("pandoc")),
        "textutil": bool(shutil.which("textutil")),
        "image_formats": sorted(IMG_EXTS),
        "audio_formats": sorted(AUDIO_EXTS),
        "video_formats": sorted(VIDEO_EXTS),
        "doc_formats": sorted(DOC_EXTS | {"pdf"}),
    }


def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        raise RuntimeError(tail[-1][:240] if tail else f"{Path(cmd[0]).name} failed")


# ------------------------------------------------------------------ PDF
_UNI_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",   # macOS
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arialuni.ttf",                           # Windows (Office)
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",         # Linux
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
)
def _uni_font(needed: set[int]) -> str | None:
    """Register a system TTF that actually covers every codepoint in `needed`
    (a font that merely exists would still render missing glyphs as boxes)."""
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        return None
    for cand in _UNI_FONT_CANDIDATES:
        if Path(cand).is_file():
            try:
                f = TTFont("UniSans", cand)
                if needed <= set(f.face.charToGlyph):
                    pdfmetrics.registerFont(f)
                    return "UniSans"
            except Exception:
                continue
    return None


def _enc(line: str, font: str | None) -> str:
    """Standard PDF fonts only cover latin-1; a unicode font renders the rest."""
    return line if font else line.encode("latin-1", "replace").decode("latin-1")


def _text_pdf(text: str, title: str = "") -> BytesIO:
    """Plain text/markdown -> PDF pages. Uses a wide-coverage system font when the
    text isn't latin-1; if none is installed, unmappable chars are replaced rather
    than failing the whole export."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    needed = {ord(ch) for ch in (title + text) if ord(ch) > 255}
    font = _uni_font(needed) if needed else None
    body_font = font or "Helvetica"
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    pw, ph = A4
    x, y, line_h = 56, ph - 60, 15
    c.setFont(body_font, 11)
    if title:
        c.setFont(body_font, 14)
        c.drawString(x, y, _enc(title, font))
        y -= line_h * 2
        c.setFont(body_font, 11)
    for raw in text.splitlines() or [""]:
        line = _enc(raw.lstrip("#*>-` "), font)
        while len(line) > 95:
            c.drawString(x, y, line[:95])
            y -= line_h
            line = line[95:]
            if y < 60:
                c.showPage(); c.setFont(body_font, 11); y = ph - 60
        c.drawString(x, y, line)
        y -= line_h
        if y < 60:
            c.showPage(); c.setFont(body_font, 11); y = ph - 60
    c.save()
    buf.seek(0)
    return buf


def _image_pdf(path: Path) -> BytesIO:
    from PIL import Image
    img = Image.open(path)
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, "PDF")
    buf.seek(0)
    return buf


def make_pdf(inputs: list[Path], out: Path) -> Path:
    """Images, text/markdown and existing PDFs -> one PDF, in the order given."""
    if not _have("pypdf") or not _have("reportlab"):
        raise RuntimeError("Making PDFs needs the pypdf and reportlab packages (bundled with the app).")
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for p in inputs:
        ext = p.suffix.lower().lstrip(".")
        if ext == "pdf":
            src = PdfReader(str(p))
            for pg in src.pages:
                w.add_page(pg)
        elif ext in IMG_EXTS:
            src = PdfReader(_image_pdf(p))
            for pg in src.pages:
                w.add_page(pg)
        elif ext in ("txt", "md", "rtf"):
            text = p.read_text(encoding="utf-8", errors="replace") if ext != "rtf" else _rtf_text(p)
            src = PdfReader(_text_pdf(text, title=p.stem))
            for pg in src.pages:
                w.add_page(pg)
        else:
            raise RuntimeError(f"{p.name}: can't put a .{ext} file into a PDF "
                               "(images, text/markdown and PDFs work).")
    if not w.pages:
        raise RuntimeError("Nothing made it into the PDF.")
    with open(out, "wb") as f:
        w.write(f)
    return out


def merge_pdfs(inputs: list[Path], out: Path) -> Path:
    if not _have("pypdf"):
        raise RuntimeError("Merging PDFs needs the pypdf package (bundled with the app).")
    from pypdf import PdfWriter
    w = PdfWriter()
    for p in inputs:
        if p.suffix.lower() != ".pdf":
            raise RuntimeError(f"{p.name} isn't a PDF. Use Make PDF for mixed files.")
        w.append(str(p))
    with open(out, "wb") as f:
        w.write(f)
    return out


def _rtf_text(path: Path) -> str:
    if shutil.which("textutil"):
        tmp = path.with_suffix(".from-rtf.txt")
        try:
            _run(["textutil", "-convert", "txt", str(path), "-output", str(tmp)])
            return tmp.read_text(encoding="utf-8", errors="replace")
        finally:
            tmp.unlink(missing_ok=True)
    return path.read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------------ convert
def convert(inp: Path, out_dir: Path, fmt: str) -> Path:
    """One file -> another format. Returns the new file's path."""
    fmt = fmt.lower().lstrip(".")
    ext = inp.suffix.lower().lstrip(".")
    out = unique_path(out_dir / f"{inp.stem}.{fmt}")
    if ext == fmt:
        raise RuntimeError(f"{inp.name} is already a .{fmt} file.")
    if ext in IMG_EXTS:
        return _convert_image(inp, out, fmt)
    if ext in AUDIO_EXTS or ext in VIDEO_EXTS:
        return _convert_av(inp, out, fmt)
    if ext in DOC_EXTS:
        return _convert_doc(inp, out, fmt)
    if ext == "pdf":
        return _convert_pdf(inp, out, fmt)
    raise RuntimeError(f".{ext} files aren't supported yet.")


def _convert_image(inp: Path, out: Path, fmt: str) -> Path:
    if fmt == "pdf":
        out.write_bytes(_image_pdf(inp).getvalue())
        return out
    if fmt not in _PIL_FMT:
        raise RuntimeError(f"Images can become: {', '.join(sorted(_PIL_FMT))} or pdf.")
    if not _have("PIL"):
        raise RuntimeError("Image conversion needs the Pillow package (bundled with the app).")
    from PIL import Image
    img = Image.open(inp)
    if fmt in _JPEG_SAVABLE and img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    img.save(out, _PIL_FMT[fmt])
    return out


def _convert_av(inp: Path, out: Path, fmt: str) -> Path:
    if fmt not in AUDIO_EXTS and fmt not in VIDEO_EXTS and fmt != "gif":
        raise RuntimeError(f"Audio/video can become: {', '.join(sorted(AUDIO_EXTS | VIDEO_EXTS))}.")
    exe = ffmpeg()
    if not exe:
        raise RuntimeError("Audio and video conversion needs ffmpeg — reinstall the app "
                           "(the installer bundles it), or install ffmpeg yourself.")
    _run([exe, "-hide_banner", "-loglevel", "error", "-y", "-i", str(inp), str(out)])
    if not out.exists() or not out.stat().st_size:
        raise RuntimeError("ffmpeg produced an empty file — is the source playable?")
    return out


def _convert_doc(inp: Path, out: Path, fmt: str) -> Path:
    ext = inp.suffix.lower().lstrip(".")
    if fmt == "pdf":
        text = inp.read_text(encoding="utf-8", errors="replace") if ext in ("txt", "md") else _doc_text(inp)
        out.write_bytes(_text_pdf(text, title=inp.stem).getvalue())
        return out
    if fmt in _TEXTUTIL_FMT and shutil.which("textutil"):
        _run(["textutil", "-convert", _TEXTUTIL_FMT[fmt], str(inp), "-output", str(out)])
        return out
    if shutil.which("pandoc"):
        _run(["pandoc", str(inp), "-o", str(out)])
        return out
    raise RuntimeError(f"Documents can become pdf, or {', '.join(sorted(_TEXTUTIL_FMT))} "
                       "with the system's converter (pandoc isn't installed).")


def _docx_text(path: Path) -> str | None:
    """.docx is a zip of XML — pull the paragraph text without any external tool."""
    if path.suffix.lower() != ".docx":
        return None
    try:
        import zipfile
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
        xml = re.sub(r"<w:p[ >/]", "\n<w:p ", xml)          # paragraphs -> newlines
        return re.sub(r"<[^>]+>", "", xml).strip()
    except Exception:
        return None


def _doc_text(inp: Path) -> str:
    if shutil.which("textutil"):
        tmp = inp.with_suffix(".to-text.txt")
        try:
            _run(["textutil", "-convert", "txt", str(inp), "-output", str(tmp)])
            return tmp.read_text(encoding="utf-8", errors="replace")
        finally:
            tmp.unlink(missing_ok=True)
    if shutil.which("pandoc"):
        p = subprocess.run(["pandoc", str(inp), "-t", "plain"], capture_output=True, text=True, timeout=300)
        if p.returncode == 0:
            return p.stdout
    text = _docx_text(inp)
    if text is not None:
        return text
    return inp.read_text(encoding="utf-8", errors="replace")


def _convert_pdf(inp: Path, out: Path, fmt: str) -> Path:
    if fmt in ("txt", "md"):
        if not _have("pypdf"):
            raise RuntimeError("Reading PDFs needs the pypdf package (bundled with the app).")
        from pypdf import PdfReader
        text = "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(inp)).pages)
        out.write_text(text.strip() or "(no readable text in this PDF)", encoding="utf-8")
        return out
    raise RuntimeError("A PDF can become txt or markdown. (PDF -> images needs poppler, not bundled.)")


# ------------------------------------------------------------------ helpers
def make_silence_wav(path: Path, seconds: float = 0.2) -> Path:
    """Tiny valid wav for tests and smoke checks."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\x00\x00" * int(8000 * seconds))
    return path


def resolve_inputs(raw: list[str]) -> list[Path]:
    out, bad = [], []
    for r in raw:
        p = Path(r.strip()).expanduser()
        if p.is_file():
            out.append(p.resolve())
        elif r.strip():
            bad.append(r.strip())
    if bad:
        raise RuntimeError("Not found: " + ", ".join(bad[:3]) + ("…" if len(bad) > 3 else ""))
    if not out:
        raise RuntimeError("Add at least one file.")
    return out


def unique_path(out: Path) -> Path:
    """Never overwrite an earlier result: foo.pdf -> foo-2.pdf -> foo-3.pdf …"""
    if not out.exists():
        return out
    for i in range(2, 1000):
        cand = out.with_name(f"{out.stem}-{i}{out.suffix}")
        if not cand.exists():
            return cand
    return out.with_name(f"{out.stem}-{int(time.time())}{out.suffix}")
