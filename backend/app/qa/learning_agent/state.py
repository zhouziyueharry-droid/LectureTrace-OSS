"""LangGraph state for one bounded tutoring turn."""
from __future__ import annotations

from typing import Any, TypedDict


class TutorState(TypedDict, total=False):
    video_id: str
    question: str
    node_id: str | None
    context: Any
    history: list[dict]
    history_citation_ids: list[str]
    intent: str
    lesson_plan: str
    retrieval_round: int
    retrieval_query: str
    retrieved_ids: list[str]
    image_ids: list[str]
    expansion: Any
    evidence_sufficient: bool
    evidence_reason: str
    answer: str
    review_ready: bool
    review_issues: list[str]
    revision_count: int

