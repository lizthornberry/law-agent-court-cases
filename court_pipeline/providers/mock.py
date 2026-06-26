"""Mock provider: returns deterministic canned output, makes NO API calls.

Useful for testing the full inventory -> classify -> segment -> transcribe ->
consolidate -> catalog plumbing offline (no key, no cost).

It inspects the prompt to decide which canned JSON shape to return:
  * CONSOLIDATION_PROMPT  (contains "BEGIN PAGE TRANSCRIPTIONS")
  * CLASSIFY_PROMPT       (contains "TASK: CLASSIFY")
  * TRANSCRIBE_PROMPT     (contains "TASK: TRANSCRIBE")

For the classify shape it derives a deterministic `page_type` from the image
key (path), so a single box exercises every routing branch: skip types
(blank/box_photo), the flash-routed types (warrant/bill_of_costs), and the
pro-routed types (cover_regular/cover_appeal/plea/hearing/other).
"""

from __future__ import annotations

import hashlib

from .base import LLMRequest, LLMResult, Provider

# A spread of page types so one mock box hits every routing/skip branch.
# Order matters only for variety; selection is by stable hash of the key.
_MOCK_PAGE_TYPE_CYCLE = [
    "cover_regular",
    "hearing",
    "warrant",
    "blank",
    "plea",
    "bill_of_costs",
    "cover_appeal",
    "other",
    "hearing",
]


def _mock_page_type(key: str) -> str:
    h = int(hashlib.sha1((key or "").encode("utf-8")).hexdigest(), 16)
    return _MOCK_PAGE_TYPE_CYCLE[h % len(_MOCK_PAGE_TYPE_CYCLE)]


def _mock_margin_case_number(key: str) -> str:
    h = int(hashlib.sha1((key or "").encode("utf-8")).hexdigest(), 16)
    # Two distinct case numbers so margin-change segmentation is exercised.
    return "11/91" if (h % 2 == 0) else "22/91"


class MockProvider(Provider):
    supports_batch = False

    def generate(self, req: LLMRequest) -> LLMResult:
        prompt = req.prompt or ""
        if "BEGIN PAGE TRANSCRIPTIONS" in prompt:
            parsed = self._consolidation()
        elif "TASK: CLASSIFY" in prompt:
            parsed = self._classify(req.key or "")
        else:  # TASK: TRANSCRIBE (or any legacy single-pass prompt)
            parsed = self._transcribe(req.key or "")
        return LLMResult(text="", parsed=parsed, key=req.key)

    def _classify(self, key: str) -> dict:
        return {
            "page_type": _mock_page_type(key),
            "margin_case_number": _mock_margin_case_number(key),
            "detected_rotation_degrees": 0,
            "languages": ["English"],
            "notes": "mock classify",
        }

    def _transcribe(self, key: str) -> dict:
        return {
            "verbatim_text": "MOCK CIVIL RECORD\n[mock verbatim transcription]\n"
            f"[source: {key}]",
            "languages": ["English"],
            "notes": "mock transcribe",
        }

    def _consolidation(self) -> dict:
        return {
            "case_number": "00/0000",
            "district": "MOCK DISTRICT",
            "magistrate": "Mock Magistrate",
            "plaintiff": "Mock Plaintiff",
            "defendant": "Mock Defendant",
            "claim": "Mock claim text",
            "date_heard": "1st day of January 1900",
            "date_heard_iso": "1900-01-01",
            "appearance_for_plaintiff": "Mock Agent",
            "interpreter": "Mock Interpreter",
            "plea_verbatim": "Plea: not guilty (mock).",
            "verdict": "Judgment for plaintiff (mock).",
            "full_transcript": "Mock transcript assembled from pages.",
            "language_notes": None,
            "field_confidence": {"case_number": "low"},
            "uncertain_fields": ["case_number"],
            "is_appeal": False,
        }
