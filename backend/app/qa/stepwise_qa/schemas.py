"""Structured model outputs used at the evidence and teaching gates."""
from typing import Literal

from pydantic import BaseModel, Field


class EvidenceAssessment(BaseModel):
    sufficient: bool = Field(description="当前已读取证据是否足以回答课程相关部分")
    reason: str = Field(description="用一句话说明已有证据覆盖了什么或缺少什么")
    missing_query: str = Field(description="若不足，给一个可直接用于课程检索的短查询；足够时为空")


class TutorReview(BaseModel):
    ready: bool = Field(description="回答是否清楚、连贯、对准问题并遵守证据规则")
    issues: list[str] = Field(default_factory=list, description="必须修正的具体问题；通过时为空")


class LearningSuggestion(BaseModel):
    id: Literal["clarify", "formula", "example", "slides", "course_context", "prerequisite", "analogy",
                "counterexample", "comparison", "application", "practice", "custom"]
    label: str = Field(min_length=2, max_length=36, description="按钮上的简短选项文字")
    prompt: str = Field(min_length=4, max_length=180, description="点击后要回答的具体问题")
    scope: Literal["course", "supplement"] = Field(description="课程资料支持，或明确标为课外补充")
    evidence_ids: list[str] = Field(default_factory=list, description="scope=course 时所依据的课程证据 ID")


class TutorDraft(BaseModel):
    answer: str = Field(min_length=1, max_length=8000, description="回答文本；普通问答简洁，开放任务可按需求完整展开")
    self_review: list[str] = Field(default_factory=list, description="简短检查回答里仍可能不清楚的地方，仅供内部生成后续选项")
    followups: list[LearningSuggestion] = Field(min_length=1, max_length=3,
                                                 description="根据回答和对话动态提出的 1 到 3 个下一步选项")



class TutorRevision(BaseModel):
    answer: str = Field(min_length=1, max_length=8000, description="最终面向学生的答案正文，不含审阅或修订说明")
