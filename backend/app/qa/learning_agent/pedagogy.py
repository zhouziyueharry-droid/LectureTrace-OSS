"""Deterministic intent routing and answer-shape guidance."""
from __future__ import annotations

import re


_INTENTS: tuple[tuple[str, str], ...] = (
    ("example", r"例题|举例|实例|案例|练习题|做一道|给个例子"),
    ("formula", r"公式|推导|证明|怎么算|计算|定理|方程|符号|为什么.*等于"),
    ("compare", r"区别|比较|对比|异同|vs\.?|versus"),
    ("summary", r"总结|概括|梳理|复习.*要点|知识点"),
    ("confusion", r"不懂|不理解|没明白|看不懂|为什么.*不|卡住|困惑"),
)

PLANS = {
    "concept": (
        "先用一句话回答，再用直观语言解释概念；接着给准确含义和与相邻概念的区别；"
        "优先用课程中的例子，没有时可给一个明确标注为‘补充例子’的简单例子；最后用一两句收束。"
    ),
    "confusion": (
        "先承认并定位问题中的关键概念，不要责备学生；从最小前置概念开始，按因果/逻辑顺序解释，"
        "每步说明‘为什么’；给一个贴近课程的例子。不要只反问；结尾可给一个可选的自测问题。"
    ),
    "formula": (
        "先说明公式解决什么问题，再逐一解释符号、单位和成立条件；按顺序解释推导或代入步骤；"
        "优先引用课堂例题，必要时给标明来源的补充算例；指出容易混淆的符号或条件。"
    ),
    "example": (
        "先说明例子对应的概念和目标；给出题目/场景，再逐步解释每一步为何这样做，并核对结果。"
        "区分课堂例子与助手补充例子；不要只给答案。"
    ),
    "compare": (
        "先给最关键的区别，再用相同维度逐项比较；定义比较中用到的术语，指出适用场景；"
        "有课程证据时引用，缺少一方证据时明确标出资料缺口。"
    ),
    "summary": (
        "按概念依赖或课堂顺序组织要点，说明各点之间的联系；保留重要条件和公式，不逐句复述字幕；"
        "最后给一个简短的整体脉络。"
    ),
}


def classify(question: str) -> str:
    text = question.lower()
    for intent, pattern in _INTENTS:
        if re.search(pattern, text):
            return intent
    return "concept"


def plan_for(intent: str) -> str:
    return PLANS.get(intent, PLANS["concept"])

