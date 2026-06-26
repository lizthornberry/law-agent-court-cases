"""court_viewer: a single-user web viewer/editor for the court-transcription archive.

This package is intentionally self-contained and treats ``court_pipeline/`` as a
READ-ONLY upstream source. It builds a portable, canonical ``results.json`` from
the pipeline outputs, mirrors it into a local SQLite (+FTS5) working store for
fast browsing/search/editing, and exports edits back to ``results.json``.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
