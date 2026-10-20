"""Synthetic technical-handoff review reference engine.

This package does not perform AI inference, embeddings, PDF extraction, or
engineering safety certification.  It compares already structured demo inputs.
"""

from .review import review_case

__all__ = ["review_case"]
