import logging

from pypdf import PdfReader
from pypdf.errors import PdfReadError
from sqlalchemy.orm import Session

from models import Document, User

logger = logging.getLogger(__name__)

MAX_DOC_CHARS = 12000          # extracted text kept per document (context window safety)
MAX_RECENT_DOCS = 3            # documents fed into the LLM context at once


def extract_pdf_text(pdf_path: str) -> str:
    """Extract all text from a PDF file. Returns '' for image-only/corrupt PDFs."""
    try:
        reader = PdfReader(pdf_path)
    except PdfReadError as exc:
        logger.warning("Unreadable PDF %s: %s", pdf_path, exc)
        return ""
    pages = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            logger.warning("Page extraction failed in %s: %s", pdf_path, exc)
            text = ""
        pages.append(text)
    return "\n".join(pages).strip()


def save_document(session: Session, user: User, filename: str, content: str) -> Document:
    """Store an uploaded document's extracted text for future Q&A."""
    doc = Document(
        user_id=user.id,
        filename=filename[:255],
        content=content[:MAX_DOC_CHARS],
    )
    session.add(doc)
    session.commit()
    logger.info("Saved document '%s' for user %s (%d chars)", filename, user.id, len(content))
    return doc


def recent_documents(session: Session, user: User, limit: int = MAX_RECENT_DOCS) -> list[Document]:
    """The user's most recently uploaded documents, newest first."""
    return (
        session.query(Document)
        .filter(Document.user_id == user.id)
        .order_by(Document.created_at.desc())
        .limit(limit)
        .all()
    )


def document_context_block(session: Session, user: User) -> str:
    """Compile recent uploaded documents into a compact LLM context block."""
    docs = recent_documents(session, user)
    if not docs:
        return ""
    blocks = [
        f"[Document: {doc.filename}]\n{doc.content}" for doc in reversed(docs)
    ]
    return (
        "The user uploaded the following documents. Use them to answer questions "
        "about their contents (summaries, financials, risks, etc.). "
        "If the question is unrelated to these documents, ignore them.\n\n"
        + "\n\n---\n\n".join(blocks)
    )
