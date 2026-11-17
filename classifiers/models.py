"""Core data models and enums for link and document classification."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum


class DocumentType(Enum):
    """Supported document and file types."""

    FOLDER = "folder"
    PDF = "pdf"
    GOOGLE_DOC = "google_doc"
    MICROSOFT_WORD = "microsoft_word"
    PPTX = "pptx"
    IMAGE = JPG = PNG = JPEG = "image"
    XLSX = "xlsx"
    ZIP_FOLDER = "zip_folder"
    UNSUPPORTED_FORMAT = "unsupported_format"


class WebsiteType(Enum):
    """Supported source websites."""

    GOOGLE_DRIVE = 1
    GOOGLE_DOC = 2
    SCRIBD = 3


@dataclass
class DriveElement:
    """Represents an extracted Google Drive item element from the DOM."""

    data_id: str
    tabindex: str | None = None
    paths: list[str] = field(default_factory=list)
    g_elements: list[str] = field(default_factory=list)
    svg_inners: list[str] = field(default_factory=list)
    svg_outers: list[str] = field(default_factory=list)
    raw_html: str = ""


@dataclass
class ClassifiedLink:
    """Represents a URL with its classified website type and optional document type."""

    url: str
    website_type: WebsiteType
    document_type: DocumentType | None = None

    @property
    def is_document_classified(self) -> bool:
        """Return True if the document type has been determined."""
        return self.document_type is not None

    def set_document_type(self, document_type: DocumentType | None) -> ClassifiedLink:
        """Update document_type in-place and return self."""
        self.document_type = document_type
        return self

    def with_document_type(self, document_type: DocumentType | None) -> ClassifiedLink:
        """Return a new ClassifiedLink instance with the updated document_type."""
        return replace(self, document_type=document_type)

    def to_tag(self) -> str:
        """Serialize the classified link into its tagged string representation.

        - When document_type is None: ``'"{url}".{WEBSITE_TYPE}'``
        - When document_type is set:  ``'"{url}".{WEBSITE_TYPE}.{DOCUMENT_TYPE}'``
        """
        if self.document_type is None:
            return f'"{self.url}".{self.website_type.name}'
        return f'"{self.url}".{self.website_type.name}.{self.document_type.name}'

    def __str__(self) -> str:
        return self.to_tag()
