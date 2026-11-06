"""Settings API for the independent teaching-agent model connection."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import llm, settings

router = APIRouter(prefix="/api/learning-agent", tags=["learning-agent"])


class AgentSettingsUpdate(BaseModel):
    provider: str
    base_url: str
    model: str
    effort: str
    max_output_tokens: int = Field(ge=1000, le=64000)
    output_language: str
    api_key: str | None = None
    clear_api_key: bool = False


@router.get("/settings")
def get_agent_settings():
    return settings.get()


@router.put("/settings")
def put_agent_settings(body: AgentSettingsUpdate):
    try:
        return settings.update(body.model_dump())
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post("/test")
def test_agent_connection():
    config = settings.get()
    if not config["has_key"]:
        return {"ok": False, "message": "请先填写教学 Agent 专属 API Key"}
    try:
        text = llm.complete(messages=[{"role": "user", "content": "请只回复：连接成功"}],
                            system="你正在进行接口连通性测试，只回复指定短语。", max_tokens=64,
                            stage="connection_test", effort="none")
        return {"ok": True, "message": f"接口可用，模型 {config['model']} 已返回：{text.strip()[:80]}"}
    except llm.LLMError as error:
        return {"ok": False, "message": str(error)}
