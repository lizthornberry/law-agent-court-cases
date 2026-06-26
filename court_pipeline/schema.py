"""Pydantic models for the per-image (page) records and per-case records.

These models define the on-disk JSON shapes and are used to validate/normalize
whatever the vision LLM returns before we persist it.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

PAGE_TYPES = [
    "box_photo",
    "blank",
    "cover_regular",
    "cover_appeal",
    "warrant",
    "bill_of_costs",
    "plea",
    "hearing",
    "other",
]

CASE_FIELDS = [
    "case_number",
    "district",
    "magistrate",
    "plaintiff",
    "defendant",
    "claim",
    "date_heard",
    "appearance_for_plaintiff",
    "interpreter",
    "plea_verbatim",
    "verdict",
    "full_transcript",
]


class PageRecord(BaseModel):
    """One photographed page (the output of the per-image VLM pass)."""

    # Provenance (filled by the pipeline, not the model).
    box: str = ""
    filename: str = ""
    path: str = ""
    order: int = 0
    sha1: str = ""
    provider: str = ""
    # `model` reflects the most recent model that wrote this record (kept for
    # backward compatibility); see classify_model / transcribe_model for the
    # per-pass detail.
    model: str = ""
    processed_at: str = ""

    # Classification output (Pass A).
    page_type: str = "other"
    margin_case_number: Optional[str] = None
    detected_rotation_degrees: int = 0
    languages: List[str] = Field(default_factory=list)
    notes: Optional[str] = None

    # Transcription output (Pass B).
    verbatim_text: str = ""

    # -- Per-pass state so each pass is independently resumable (keyed on sha1).
    # Pass A (classify):
    classified_at: str = ""
    classify_model: str = ""
    classify_error: Optional[str] = None
    # Pass B (transcribe):
    transcribed_at: str = ""
    transcribe_model: str = ""
    transcribe_error: Optional[str] = None
    # "pending" -> not transcribed yet; "done" -> transcribed; "skipped" -> a
    # skip_types page (no API call, verbatim_text left empty).
    transcription_status: str = "pending"

    # Legacy single-pass error field (still honoured for old caches).
    error: Optional[str] = None


class CaseRecord(BaseModel):
    """One civil case (the consolidated output)."""

    # Provenance.
    case_id: str = ""
    box: str = ""
    is_appeal: bool = False
    source_images: List[str] = Field(default_factory=list)
    page_range: List[int] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    processed_at: str = ""

    # Requested fields.
    case_number: Optional[str] = None
    district: Optional[str] = None
    magistrate: Optional[str] = None
    plaintiff: Optional[str] = None
    defendant: Optional[str] = None
    claim: Optional[str] = None
    date_heard: Optional[str] = None
    date_heard_iso: Optional[str] = None
    appearance_for_plaintiff: Optional[str] = None
    interpreter: Optional[str] = None
    plea_verbatim: Optional[str] = None
    verdict: Optional[str] = None
    full_transcript: Optional[str] = None

    # Quality / review aids.
    language_notes: Optional[str] = None
    field_confidence: Dict[str, str] = Field(default_factory=dict)
    uncertain_fields: List[str] = Field(default_factory=list)

    error: Optional[str] = None
