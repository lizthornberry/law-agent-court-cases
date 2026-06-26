"""Prompt templates for the per-image pass and the per-case consolidation."""

from __future__ import annotations

from .schema import PAGE_TYPES

PAGE_PROMPT = """You are a meticulous archival transcriber working with photographed pages \
from South African Resident Magistrate's Court civil case files, c. 1880-1920. \
The image is a camera photo and MAY be rotated 90/180/270 degrees and/or show two \
facing pages of an open book. Read it regardless of orientation.

Return ONLY a single JSON object (no prose, no markdown fences) with these keys:

- "page_type": one of {page_types}.
- "margin_case_number": the case number written or printed on this page, if any \
(typical formats: "52", "71/91", "90/1901"). Use null if none is visible.
- "detected_rotation_degrees": integer 0, 90, 180 or 270 describing how much you \
had to mentally rotate the page to read it.
- "languages": array of languages present, e.g. ["English"], ["English","isiZulu"].
- "verbatim_text": a faithful, VERBATIM transcription. Rules:
    * Preserve original spelling, punctuation, capitalisation and line breaks.
    * Transcribe BOTH the pre-printed form labels and the handwritten entries.
    * Use "[illegible]" for unreadable words, and "[illegible: guess?]" when you \
have a plausible guess.
    * If two pages are visible, transcribe the left page, then a line containing \
exactly "--- PAGE BREAK ---", then the right page.
    * Do NOT summarise, paraphrase, translate, or add commentary.
- "notes": a brief note on condition/legibility/uncertainty, or null.

page_type guidance:
- "cover_regular": a pre-printed cover such as "CIVIL RECORD / CASE NO." that opens a case.
- "cover_appeal": a cover page for an APPEAL (different wording from the regular cover).
- "box_photo": a photograph of an archival box/folder/binding, not a document page.
- "blank": an essentially blank or near-blank page.
- "warrant": a warrant of execution / judgment warrant form.
- "bill_of_costs": a taxed bill of costs (tabular fees form).
- "plea": a page that is primarily the defendant's plea.
- "hearing": a page of the actual hearing record / evidence / proceedings.
- "other": anything else.

Be literal and complete. Transcribe everything you can read.""".format(
    page_types=PAGE_TYPES
)


# --- Two-pass prompts -------------------------------------------------------
# Pass A: classify only (cheap/fast). Deliberately produces NO transcription so
# the output stays small. The literal "TASK: CLASSIFY" marker lets the offline
# mock provider tell this prompt apart from the transcribe/consolidation ones.
CLASSIFY_PROMPT = """TASK: CLASSIFY one photographed page from South African Resident \
Magistrate's Court civil case files, c. 1880-1920. The image is a camera photo and MAY \
be rotated 90/180/270 degrees and/or show two facing pages. Judge it regardless of \
orientation. Do NOT transcribe the page; only classify it.

Return ONLY a single JSON object (no prose, no markdown fences) with these keys:

- "page_type": one of {page_types}.
- "margin_case_number": the case number written or printed on this page, if any \
(typical formats: "52", "71/91", "90/1901"). Use null if none is visible.
- "detected_rotation_degrees": integer 0, 90, 180 or 270 describing how much you \
had to mentally rotate the page to read it.
- "languages": array of languages present, e.g. ["English"], ["English","isiZulu"].
- "notes": a brief note on condition/legibility/uncertainty, or null.

page_type guidance:
- "cover_regular": a pre-printed cover such as "CIVIL RECORD / CASE NO." that opens a case.
- "cover_appeal": a cover page for an APPEAL (different wording from the regular cover).
- "box_photo": a photograph of an archival box/folder/binding, not a document page.
- "blank": an essentially blank or near-blank page.
- "warrant": a warrant of execution / judgment warrant form.
- "bill_of_costs": a taxed bill of costs (tabular fees form).
- "plea": a page that is primarily the defendant's plea.
- "hearing": a page of the actual hearing record / evidence / proceedings.
- "other": anything else.

Be decisive. Output ONLY the JSON object.""".format(
    page_types=PAGE_TYPES
)


# Pass B: pure verbatim transcription (model routed by page_type). The literal
# "TASK: TRANSCRIBE" marker lets the mock provider recognise this prompt.
TRANSCRIBE_PROMPT = """TASK: TRANSCRIBE one photographed page from South African Resident \
Magistrate's Court civil case files, c. 1880-1920. The image is a camera photo and MAY \
be rotated 90/180/270 degrees and/or show two facing pages of an open book. Read it \
regardless of orientation.

Return ONLY a single JSON object (no prose, no markdown fences) with these keys:

- "verbatim_text": a faithful, VERBATIM transcription. Rules:
    * Preserve original spelling, punctuation, capitalisation and line breaks.
    * Transcribe BOTH the pre-printed form labels and the handwritten entries.
    * Use "[illegible]" for unreadable words, and "[illegible: guess?]" when you \
have a plausible guess.
    * If two pages are visible, transcribe the left page, then a line containing \
exactly "--- PAGE BREAK ---", then the right page.
    * Do NOT summarise, paraphrase, translate, or add commentary.
- "languages": array of languages present, e.g. ["English"], ["English","isiZulu"].
- "notes": a brief note on condition/legibility/uncertainty, or null.

Be literal and complete. Transcribe everything you can read. Output ONLY the JSON object."""


# NOTE: the model is deliberately NOT asked for the full hearing transcript any
# more. ``consolidate.py`` assembles ``full_transcript`` deterministically in code
# by concatenating the per-page ``verbatim_text`` (verbatim, no model drift), so
# this prompt only extracts the SHORT structured header/outcome fields. The page
# transcriptions are still supplied below purely as CONTEXT for that extraction.
CONSOLIDATION_PROMPT = """You are extracting structured data for ONE South African civil \
court case (Resident Magistrate's Court, c. 1880-1920). You are given (1) the cover-page \
image of the case and (2) verbatim transcriptions of every page of the case, in order.

Return ONLY a single JSON object (no prose, no markdown fences) with EXACTLY these keys:

- "case_number": e.g. "52" or "90/1901". null if unknown.
- "district": the district / magistracy. null if unknown.
- "magistrate": name of the magistrate before whom the case was heard. null if unknown.
- "plaintiff": plaintiff name(s). null if unknown.
- "defendant": defendant name(s). null if unknown.
- "claim": the claim / cause of action, verbatim or closely paraphrased from the record. null if unknown.
- "date_heard": the hearing date AS WRITTEN (verbatim). null if unknown.
- "date_heard_iso": that date in ISO 8601 "YYYY-MM-DD" if determinable, else null.
- "appearance_for_plaintiff": name of the person/agent/attorney who appeared for the \
plaintiff. null if none/unknown.
- "interpreter": name of the interpreter, if any. null otherwise.
- "plea_verbatim": the VERBATIM text of the plea. null if none.
- "verdict": the verdict / judgment / resolution of the case. null if unknown.
- "language_notes": brief note on languages / translation issues, or null.
- "field_confidence": object mapping each of these field names to "high", "medium" or \
"low": case_number, district, magistrate, plaintiff, defendant, claim, date_heard, \
appearance_for_plaintiff, interpreter, plea_verbatim, verdict.
- "uncertain_fields": array listing the field names you are least confident about.
- "is_appeal": true if this is an appeal case, else false.

Do NOT return the full hearing transcript; it is assembled separately from the page \
transcriptions and is not requested here.

Rules:
- Use null for fields genuinely absent from the record. NEVER fabricate names, dates or outcomes.
- Prefer information from the cover page for the header fields (case number, district, \
magistrate, parties, date, appearances, interpreter, plea).
- Keep verbatim fields verbatim.

--- BEGIN PAGE TRANSCRIPTIONS ---
{transcripts}
--- END PAGE TRANSCRIPTIONS ---
"""
