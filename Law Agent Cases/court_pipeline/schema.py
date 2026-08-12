"""Pydantic models for the per-image (page) records and per-case records.

These models define the on-disk JSON shapes and are used to validate/normalize
whatever the vision LLM returns before we persist it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

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
    "appearance_for_defendant",
    "lawyer_or_agent_for_plaintiff",
    "lawyer_or_agent_for_defendant",
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
    # Provider alternates are intentionally separate from the Gemini/default
    # baseline. retry_opus_pages writes transcription_alternates["claude"] so a
    # difficult-page retry cannot destroy verbatim_text or its provenance.
    transcription_alternates: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    # -- Per-pass state so each pass is independently resumable (keyed on sha1).
    # Pass A (classify):
    classified_at: str = ""
    classify_model: str = ""
    classify_model_version: str = ""
    classify_prompt_hash: str = ""
    classify_git_commit: str = ""
    classify_git_dirty: Optional[bool] = None
    classify_error: Optional[str] = None
    # Pass B (transcribe):
    transcribed_at: str = ""
    transcribe_model: str = ""
    transcribe_model_version: str = ""
    transcribe_prompt_hash: str = ""
    transcribe_git_commit: str = ""
    transcribe_git_dirty: Optional[bool] = None
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
    model_version: str = ""
    prompt_hash: str = ""
    git_commit: str = ""
    git_dirty: Optional[bool] = None
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
    appearance_for_defendant: Optional[str] = None
    lawyer_or_agent_for_plaintiff: Optional[str] = None
    lawyer_or_agent_for_defendant: Optional[str] = None
    interpreter: Optional[str] = None
    plea_verbatim: Optional[str] = None
    verdict: Optional[str] = None
    full_transcript: Optional[str] = None

    # Quality / review aids.
    language_notes: Optional[str] = None
    field_confidence: Dict[str, str] = Field(default_factory=dict)
    uncertain_fields: List[str] = Field(default_factory=list)
    # Pages that carried no usable transcription when this case was consolidated,
    # and are therefore MISSING from full_transcript. Empty on a clean run; only
    # populated when consolidation was forced with --allow-incomplete.
    incomplete_pages: List[str] = Field(default_factory=list)

    error: Optional[str] = None
