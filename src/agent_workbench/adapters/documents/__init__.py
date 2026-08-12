"""Reading document formats that need a library, in one place per format."""

from agent_workbench.adapters.documents.docx import (
    DocxPreview,
    DocxTooLargeError,
    extract_docx_preview,
    preflight_docx,
)

__all__ = [
    "DocxPreview",
    "DocxTooLargeError",
    "extract_docx_preview",
    "preflight_docx",
]
