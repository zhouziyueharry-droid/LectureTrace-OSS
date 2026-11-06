"""Structured model outputs used at the evidence and teaching gates."""
from pydantic import BaseModel, Field


class EvidenceAssessment(BaseModel):
    sufficient: bool = Field(description="当前已读取证据是否足以回答课程相关部分")
    reason: str = Field(description="用一句话说明已有证据覆盖了什么或缺少什么")
    missing_query: str = Field(description="若不足，给一个可直接用于课程检索的短查询；足够时为空")


class TutorReview(BaseModel):
    ready: bool = Field(description="回答是否清楚、连贯、对准问题并遵守证据规则")
    issues: list[str] = Field(default_factory=list, description="必须修正的具体问题；通过时为空")

