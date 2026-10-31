from pypdf import PdfReader


def extract_resume_text(file):

    if not file:
        raise ValueError("No resume file was uploaded.")

    if not file.filename.lower().endswith(".pdf"):
        raise ValueError("Only PDF resumes are supported.")

    reader = PdfReader(file)

    text = ""

    for page in reader.pages:

        page_text = page.extract_text()

        if page_text:
            text += page_text + "\n"

    text = text.strip()

    if not text:
        raise ValueError(
            "Could not extract text from this PDF. "
            "The resume may be scanned or image-based."
        )

    return text