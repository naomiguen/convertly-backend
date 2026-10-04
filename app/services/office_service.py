"""Konversi dokumen Office <-> PDF.

PDF -> Word  : pdf2docx (pure Python). Hasil terbaik untuk PDF yang teksnya bisa diseleksi;
               untuk PDF hasil scan gunakan alat 'Scan ke Teks' (OCR).
Word -> PDF  : LibreOffice (headless) bila terpasang; jika tidak ada dan OS = Windows,
               memakai Microsoft Word (docx2pdf).
PPTX -> PDF  : LibreOffice bila terpasang; jika tidak ada dan OS = Windows,
               memakai Microsoft PowerPoint (COM).
"""
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import threading

from app.core.security import upload_path
from app.services.output import Output, new_result

CONVERT_TIMEOUT = 180  # detik
_office_lock = threading.Lock()   # Word/PowerPoint hanya boleh dikendalikan satu proses sekaligus

_WINDOWS_SOFFICE = [
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
]

_KIND_LABEL = {".docx": "Word", ".pptx": "PowerPoint"}


def _find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for path in _WINDOWS_SOFFICE:
        if os.path.exists(path):
            return path
    return None


def _has_msoffice_app(exe: str) -> bool:
    """Cek Microsoft Office terpasang (mis. exe='WINWORD.EXE' / 'POWERPNT.EXE'). Hanya Windows."""
    if sys.platform != "win32":
        return False
    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        if not base:
            continue
        for pattern in (("Microsoft Office", "root", "Office*", exe), ("Microsoft Office", "Office*", exe)):
            if glob.glob(os.path.join(base, *pattern)):
                return True
    return False


def available_engines() -> dict:
    return {
        "libreoffice": _find_soffice() is not None,
        "word": _has_msoffice_app("WINWORD.EXE"),
        "powerpoint": _has_msoffice_app("POWERPNT.EXE"),
    }


def available_kinds() -> dict:
    """Jenis dokumen yang bisa dikonversi ke PDF di server ini."""
    e = available_engines()
    return {".docx": e["libreoffice"] or e["word"], ".pptx": e["libreoffice"] or e["powerpoint"]}


def _convert_failed(kind: str) -> ValueError:
    return ValueError(f"Dokumen {_KIND_LABEL[kind]} gagal dikonversi. Pastikan file tidak rusak atau berpassword.")


def _to_pdf_libreoffice(soffice: str, src: str, dest: str, kind: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        profile = os.path.join(tmp, "profile")   # profil terpisah -> boleh jalan paralel
        outdir = os.path.join(tmp, "out")
        os.makedirs(outdir)
        profile_uri = "file:///" + profile.replace("\\", "/").lstrip("/")
        cmd = [soffice, "--headless", "--norestore", f"-env:UserInstallation={profile_uri}",
               "--convert-to", "pdf", "--outdir", outdir, src]
        try:
            subprocess.run(cmd, capture_output=True, timeout=CONVERT_TIMEOUT, check=True)
        except subprocess.TimeoutExpired:
            raise ValueError("Konversi terlalu lama (timeout). Coba dokumen yang lebih kecil.")
        except subprocess.CalledProcessError:
            raise _convert_failed(kind)
        produced = glob.glob(os.path.join(outdir, "*.pdf"))
        if not produced:
            raise _convert_failed(kind)
        shutil.move(produced[0], dest)


def _docx_to_pdf_word(src: str, dest: str) -> None:
    import pythoncom
    from docx2pdf import convert

    with _office_lock:
        pythoncom.CoInitialize()
        try:
            convert(os.path.abspath(src), os.path.abspath(dest))
        except Exception:
            raise _convert_failed(".docx")
        finally:
            pythoncom.CoUninitialize()
    if not os.path.exists(dest):
        raise _convert_failed(".docx")


def _pptx_to_pdf_powerpoint(src: str, dest: str) -> None:
    import pythoncom
    import win32com.client

    PP_SAVE_AS_PDF = 32
    with _office_lock:
        pythoncom.CoInitialize()
        app = None
        try:
            app = win32com.client.DispatchEx("PowerPoint.Application")
            # Open(FileName, ReadOnly, Untitled, WithWindow)
            presentation = app.Presentations.Open(os.path.abspath(src), True, False, False)
            try:
                presentation.SaveAs(os.path.abspath(dest), PP_SAVE_AS_PDF)
            finally:
                presentation.Close()
        except Exception:
            raise _convert_failed(".pptx")
        finally:
            if app is not None:
                try:
                    app.Quit()
                except Exception:
                    pass
            pythoncom.CoUninitialize()
    if not os.path.exists(dest):
        raise _convert_failed(".pptx")


def _office_to_pdf(filename: str, kind: str) -> Output:
    src = upload_path(filename)
    if not os.path.exists(src):
        raise FileNotFoundError(filename)

    engines = available_engines()
    out = new_result(".pdf")
    try:
        if engines["libreoffice"]:
            _to_pdf_libreoffice(_find_soffice(), src, out.path, kind)
        elif kind == ".docx" and engines["word"]:
            _docx_to_pdf_word(src, out.path)
        elif kind == ".pptx" and engines["powerpoint"]:
            _pptx_to_pdf_powerpoint(src, out.path)
        else:
            raise ValueError(
                f"Konversi {_KIND_LABEL[kind]} ke PDF butuh LibreOffice di server "
                f"(atau Microsoft {_KIND_LABEL[kind]} di Windows). Install LibreOffice lalu restart backend."
            )
    except Exception:
        if os.path.exists(out.path):
            os.remove(out.path)
        raise
    return out


def docx_to_pdf(filename: str, options: dict) -> Output:
    return _office_to_pdf(filename, ".docx")


def pptx_to_pdf(filename: str, options: dict) -> Output:
    return _office_to_pdf(filename, ".pptx")


def pdf_to_docx(filename: str, options: dict) -> Output:
    from pdf2docx import Converter
    import pymupdf

    src = upload_path(filename)
    if not os.path.exists(src):
        raise FileNotFoundError(filename)

    try:
        with pymupdf.open(src) as doc:
            if doc.needs_pass:
                raise ValueError("PDF dilindungi password. Buka dulu lewat alat 'Buka Kunci PDF'.")
    except ValueError:
        raise
    except Exception:
        raise ValueError("File PDF rusak atau tidak bisa dibaca.")

    out = new_result(".docx")
    cv = Converter(src)
    try:
        cv.convert(out.path)
    except Exception:
        if os.path.exists(out.path):
            os.remove(out.path)
        raise ValueError("PDF gagal dikonversi ke Word.")
    finally:
        cv.close()
    return out
