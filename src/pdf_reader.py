
from pathlib import Path
from pypdf import PdfReader


def extract_pdf_text(file_path: Path) -> str:
    """Extract text from a PDF file."""

    if not file_path.exists():
        return ""

    reader = PdfReader(file_path)
    extracted_pages = []

    for page in reader.pages:
        text = page.extract_text()

        if text:
            extracted_pages.append(text)

    return "\n".join(extracted_pages).strip()
