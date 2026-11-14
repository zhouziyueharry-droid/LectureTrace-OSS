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
_ITEM = rf"(?:T\d{{4,}}\s*[-–—]\s*T\d{{4,}}|{_ID})"
_GROUP_RE = re.compile(rf"\[\s*({_ITEM}(?:\s*[,，、;；]\s*{_ITEM})*)\s*\]")
_RANGE_RE = re.compile(r"T(\d{4,})\s*[-–—]\s*T(\d{4,})")


def _group_ids(group: str) -> list[str]:
    ids = []
    for item in re.split(r"\s*[,，、;；]\s*", group):
        item = item.strip()
        match = _RANGE_RE.fullmatch(item)
        if match:
            first, last = match.groups()
            # Bound expansion of model/user text; invalid ranges remain IDs that fail validation.
            if max(len(first), len(last)) <= 15:
                start, end = int(first), int(last)
                if 0 <= end - start < 1000:
                    ids.extend(f'T{i:0{len(first)}}' for i in range(start, end + 1))
                    continue
        ids.append(item)
    return ids


def normalize(text: str) -> str:
    """展开合并引用和 T 范围；每个成员仍须通过 resolve 校验。"""
    return _GROUP_RE.sub(lambda m: "".join(f"[{i}]" for i in _group_ids(m.group(1))), text)


def extract_ids(text: str) -> list[str]:
    """按出现顺序去重。"""
    ids: list[str] = []
    for m in _GROUP_RE.finditer(text):
        ids.extend(_group_ids(m.group(1)))
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
