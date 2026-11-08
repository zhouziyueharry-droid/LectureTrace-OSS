"""解析回答中的证据引用，校验并映射为跳转时间（交接 0001 §10.2 第 3 步）。

可引用的 ID 共 4 种（evidence.ID_PATTERN）：页 [S012]、出现 [S012.2]、无幻灯片时段 [N001]、讲解片段 [T0031]。
"""
from __future__ import annotations

import logging
import re
from typing import Collection, Mapping

from ..pipeline.evidence import ID_PATTERN

log = logging.getLogger(__name__)

_ID = ID_PATTERN
# 兼容模型偶尔写成 [S012, T0142] / [S012，T0142]
_GROUP_RE = re.compile(rf"\[\s*({_ID}(?:\s*[,，、;；]\s*{_ID})*)\s*\]")
_LINK_RE = re.compile(rf"\[\s*({_ID})\s*\]\(\s*#cite-({_ID})\s*\)")
_ID_RE = re.compile(_ID)


def normalize(text: str) -> str:
    """把证据链接和组合 ID 规范成简单的 [ID]，便于校验并交给前端渲染。"""
    text = _LINK_RE.sub(lambda m: f"[{m.group(1)}]" if m.group(1) == m.group(2) else m.group(0), text)
    return _GROUP_RE.sub(lambda m: "".join(f"[{i}]" for i in _ID_RE.findall(m.group(1))), text)


def extract_ids(text: str) -> list[str]:
    """按出现顺序去重。"""
    ids: list[str] = []
    for m in _GROUP_RE.finditer(text):
        ids.extend(_ID_RE.findall(m.group(1)))
    return list(dict.fromkeys(ids))


def resolve(text: str, index: Mapping[str, dict], allowed: Collection[str] | None = None
            ) -> tuple[list[dict], list[str]]:
    """返回 (有效引用 [{"id","start"}], 无效 ID)。

    index：`evidence.id_index` 的结果（ID → {"jump", ...}），start 取 jump。
    allowed：本轮允许引用的 ID；为 None 时只要求存在于 index。
    """
    valid, invalid = [], []
    for i in extract_ids(text):
        if i in index and (allowed is None or i in allowed):
            valid.append({"id": i, "start": float(index[i]["jump"])})
        else:
            invalid.append(i)
    if invalid:
        log.warning("回答中引用了无效证据 ID：%s", ", ".join(invalid))
    return valid, invalid
