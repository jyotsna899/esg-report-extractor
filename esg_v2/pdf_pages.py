"""PDF helpers: per-page text (for finding and verifying) and page subsets (for the LLM)."""

import gc
import io


def read_page_texts(pdf_path: str, progress_callback=None) -> dict[int, str]:
    """{1-based page number: text}. pypdfium2 first (fast, low memory), pdfplumber fallback."""
    pages: dict[int, str] = {}
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(pdf_path)
        total = len(pdf)
        for i in range(total):
            page = pdf[i]
            textpage = page.get_textpage()
            pages[i + 1] = (textpage.get_text_range() or "").strip()
            textpage.close()
            page.close()
            if progress_callback:
                progress_callback(i + 1, total)
        pdf.close()
        gc.collect()
        return pages
    except Exception:
        pages = {}

    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            pages[i + 1] = (page.extract_text() or "").strip()
            try:
                page.flush_cache()
            except Exception:
                pass
            if progress_callback:
                progress_callback(i + 1, total)
    gc.collect()
    return pages


def page_count(pdf_path: str) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(pdf_path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def subset_pdf_bytes(pdf_path: str, page_numbers: list[int]) -> bytes:
    """A new PDF containing only the given 1-based pages, in the given order."""
    import pypdfium2 as pdfium

    src = pdfium.PdfDocument(pdf_path)
    dst = pdfium.PdfDocument.new()
    try:
        dst.import_pages(src, [p - 1 for p in page_numbers])
        buf = io.BytesIO()
        dst.save(buf)
        return buf.getvalue()
    finally:
        dst.close()
        src.close()


def is_mostly_scanned(page_texts: dict[int, str], min_chars: int = 30, threshold: float = 0.2) -> bool:
    """True when fewer than `threshold` of pages carry real text (image-only PDF)."""
    if not page_texts:
        return True
    with_text = sum(1 for t in page_texts.values() if len(t) >= min_chars)
    return with_text / len(page_texts) < threshold
