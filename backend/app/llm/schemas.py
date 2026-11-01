"""结构化输出的返回模型（提案 0001 §5.2、§7.2、§8），以及按批次动态生成「ID 枚举约束」模型的工具。

- 这里是**基础模型**：`id` 等字段是普通字符串。读幻灯片时用 `slides_response_model(...)` 生成
  本批专用的子类，把页 ID、出现 ID、T ID 限定为本批输入里的枚举（严格模式下模型只能从中选）。
- 所有模型都是 `extra="forbid"`：多出的键在本地校验时也算错误，与 OpenAI 严格模式
  （`additionalProperties: false`）一致。
- 严格模式不支持任意键的对象，所以 `speech_categories` 一律是 `[{id, category}]` 数组；
  收到后用 `check_speech_categories` 检查、`speech_categories_dict` 转成内部字典（§5.4）。
- 字段的 `description` 会进入 JSON Schema 发给模型，写的是字段含义，不写格式要求。

调用方式见 `client.complete_structured`。
"""
from __future__ import annotations

from typing import Any, Iterable, Literal, Sequence, get_args

from pydantic import BaseModel, ConfigDict, Field, create_model

Category = Literal["lecture", "admin", "interaction", "media", "offtopic", "mixed", "uncertain"]
CATEGORIES: tuple[str, ...] = get_args(Category)


class StrictModel(BaseModel):
    # 所有结构化返回模型的基类：不允许多余字段。（类 docstring 会进入 JSON Schema，所以这里只写注释）

    model_config = ConfigDict(extra="forbid")


# ---------- 读幻灯片（§5.2） ----------

class SpeechCategory(StrictModel):
    id: str = Field(description="Speech segment ID from the input, e.g. T0031")
    category: Category = Field(description=(
        "lecture | admin (course logistics) | interaction | media | offtopic | "
        "mixed (really mixes lecture and logistics) | uncertain (cannot tell, including truncated speech)"))


class OccurrenceSummary(StrictModel):
    id: str = Field(description="Occurrence ID from the input, e.g. S012.1")
    spoken_summary: str = Field(description="Summary of what was said during this occurrence, "
                                            "based only on the speech given in the input")


class SlideRecord(StrictModel):
    id: str = Field(description="Slide ID from the input, e.g. S012")
    title: str
    visual_content: str = Field(description=(
        "Transcription of the slide: text, formulas in LaTeX, figures described; "
        "handwriting marked [handwritten] (or [handwritten, illegible]); "
        "handwriting only visible in a later occurrence marked [handwritten @S012.2]"))
    occurrences: list[OccurrenceSummary]
    speech_categories: list[SpeechCategory] = Field(description="One entry per speech segment of this slide")


class SlidesResponse(StrictModel):
    slides: list[SlideRecord]


# ---------- 大纲（§7.2） ----------

class OutlineItem(StrictModel):
    # 大纲的第二层节点，以及「课程事务」中的条目。

    title: str
    summary: str
    evidence: list[str] = Field(description="IDs cited as evidence (S012 / S012.1 / N001 / T0031)")


class OutlineChapter(StrictModel):
    # 大纲第一层；最多两层，所以 children 不再有 children。

    title: str
    summary: str
    evidence: list[str] = Field(description="IDs cited as evidence (S012 / S012.1 / N001 / T0031)")
    children: list[OutlineItem]


class OutlineAdmin(StrictModel):
    title: str
    items: list[OutlineItem]


class OutlineResponse(StrictModel):
    chapters: list[OutlineChapter]
    admin: OutlineAdmin
    speech_categories: list[SpeechCategory] = Field(
        description="Categories of speech segments that have no slide (only those)")


# ---------- ID 枚举约束 ----------

def id_enum(ids: Iterable[str]) -> Any:
    """把一组允许的 ID 变成 `Literal[...]` 类型（去重，保持顺序），可直接用作字段注解。

    JSON Schema 中生成 `{"type": "string", "enum": [...]}`；严格模式下模型只能输出其中之一，
    本地校验时不在列表里的 ID 会报错。列表为空时抛 ValueError（空枚举不是合法 schema）。
    注意 OpenAI 严格模式的上限：整个 schema 的枚举值合计不超过 1000 个；超出时
    `client.complete_structured` 会退回 JSON 模式（仍按枚举本地校验）。
    """
    uniq = list(dict.fromkeys(str(i) for i in ids))
    if not uniq:
        raise ValueError("ID 枚举不能为空")
    return Literal[tuple(uniq)]  # type: ignore[valid-type]


def slides_response_model(slide_ids: Sequence[str], occurrence_ids: Sequence[str],
                          t_ids: Sequence[str] = ()) -> type[SlidesResponse]:
    """生成本批读幻灯片专用的返回模型：页 `id`、出现 `id`、T `id` 都限定为本批输入中的 ID。

    返回的类是 `SlidesResponse` 的子类（各层记录也是对应基础模型的子类），字段与访问方式不变。
    `t_ids` 为空（本批没有任何讲解）时，`speech_categories` 限定为空数组。
    枚举只保证「ID 来自输入」，不保证「每个出现 / T 恰好一次」，后者由调用方检查
    （可传给 `complete_structured(check=...)`，不通过时带说明重试）。
    """
    speech_item: type[SpeechCategory]
    if t_ids:
        speech_item = create_model("SpeechCategoryBatch", __base__=SpeechCategory,
                                   id=(id_enum(t_ids), Field(description="Speech segment ID from the input")))
        speech_field: Any = (list[speech_item], Field(description="One entry per speech segment of this slide"))
    else:
        speech_field = (list[SpeechCategory], Field(max_length=0, description="Always empty for this batch"))
    occ = create_model("OccurrenceSummaryBatch", __base__=OccurrenceSummary,
                       id=(id_enum(occurrence_ids), Field(description="Occurrence ID from the input")))
    slide = create_model("SlideRecordBatch", __base__=SlideRecord,
                         id=(id_enum(slide_ids), Field(description="Slide ID from the input")),
                         occurrences=(list[occ], ...),
                         speech_categories=speech_field)
    return create_model("SlidesResponseBatch", __base__=SlidesResponse, slides=(list[slide], ...))


# ---------- speech_categories：数组 → 内部字典 ----------

def check_speech_categories(items: Sequence[SpeechCategory], expected_ids: Iterable[str]) -> list[str]:
    """检查每个 ID 都属于 expected_ids、每个 expected ID 恰好出现一次。返回问题列表（英文，可直接作重试反馈）。"""
    expected = list(dict.fromkeys(expected_ids))
    allowed = set(expected)
    seen: dict[str, int] = {}
    for it in items:
        seen[it.id] = seen.get(it.id, 0) + 1
    problems = []
    unknown = [i for i in seen if i not in allowed]
    if unknown:
        problems.append("speech_categories contains IDs not in the input: " + ", ".join(unknown))
    dup = [i for i, n in seen.items() if n > 1]
    if dup:
        problems.append("speech_categories lists these IDs more than once: " + ", ".join(dup))
    missing = [i for i in expected if i not in seen]
    if missing:
        problems.append("speech_categories is missing these IDs: " + ", ".join(missing))
    return problems


def speech_categories_dict(items: Sequence[SpeechCategory]) -> dict[str, str]:
    """`[{id, category}]` → `{id: category}`（§5.4 的存储格式）。重复 ID 以最后一次为准，调用前应先检查。"""
    return {it.id: it.category for it in items}
