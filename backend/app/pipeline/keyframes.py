"""阶段 keyframes：source.mp4 → keyframes/S###.jpg + keyframes.json（ARCHITECTURE §3.3）。

算法（帧差 + 分块比对，针对"幻灯片 + 讲解"类视频；提案 0001 交接 §3）：
0. 主循环之前在 ROI 内均匀取约 60 帧求中位图，自动去掉黑边得到**内容框**（A）。
   内容框不替代用户确认的 ROI；此后差分、内容像素、比对都只在内容框内做，保存的图片也裁到内容框。
1. 每 interval_s 秒取一帧：裁 ROI → 裁内容框 → 缩到宽 width 像素、转灰度 → 手工遮罩区域置 0。
2. 与上一采样帧（以及本段首帧，防止缓慢渐变）比较变化像素占比；稳定 ≥ stable_s 秒才算一段稳定画面。
3. 内容像素 < blank_ink% 的稳定段（纯黑、"End of slide show"）丢弃，按无幻灯片处理（D）。
4. 新的稳定段先与紧挨着的上一张比较（间隔 ≤ stable_s）：
   - 逐条展开（旧内容保留 ≥ 95%，旧内容 ≥ reveal_min_ink%，新增 / 旧内容 ≤ reveal_max_added，B）：
     合并，取新帧；
   - 分块比对 block_match ≥ dup_threshold（C）：内容相同，合并，保留内容像素更多的一帧
     （批注写了又擦掉时留下有批注的那帧）。
5. 否则用 block_match 与所有已保存页的每次出现比较，取得分最高且 ≥ dup_threshold 者：
   老师翻回旧页，追加一次出现，并为这次出现另存一张截图（S012_2.jpg，§3.7.1）。
6. 长时间没有稳定画面（纯摄像头、板书）不产生幻灯片。
7. 每页记录与时间上紧邻的前一页的 block_match 得分 similar_prev（0.5 ≤ 得分 < dup_threshold 时）。

核心判定（SlideTracker、SlideLibrary、detect_slides）只依赖 numpy 数组，可直接用合成帧单测。
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from ..config import KeyframeParams
from .base import StageContext, StageError

DRIFT_FACTOR = 3.0   # 与本段首帧的差超过 diff_threshold × 此值也视为换页（缓慢渐变、镜头移动）
PIXEL_DELTA = 25     # 灰度差超过此值的像素算"变化"，高于视频压缩噪声
INK_DELTA = 40      # 与背景灰度相差超过此值的像素视为"内容"（文字、线条）；block_match 的"变化"同此口径
REVEAL_KEEP = 0.95   # 新帧保留旧帧内容像素的比例 ≥ 此值，视为在旧页上追加内容
# 内容框（交接 §3.2 给定的固定值）
BOX_SAMPLES = 60     # 均匀取多少帧求中位图
BOX_LUMA = 30        # 中位图中亮度 > 此值的像素算画面
BOX_LINE_FRAC = 0.5  # 行 / 列中画面像素占比 > 此值，属于内容框
BOX_MIN_AREA = 0.5   # 内容框面积 < ROI 的此比例时退回整个 ROI
BOX_MAX_WIDTH = 960  # 求中位图前把过宽的 ROI 缩到此宽度，控制内存
SIMILAR_MIN = 0.5    # similar_prev 只记录 block_match ∈ [此值, dup_threshold) 的相邻页（交接 §3.7）


# ---------- 纯函数 ----------

def crop(frame: np.ndarray, roi: Sequence[float] | None) -> np.ndarray:
    """按相对坐标 (x, y, w, h) 裁出幻灯片区域；roi 为空时原样返回。"""
    if not roi:
        return frame
    h, w = frame.shape[:2]
    x0, y0 = int(roi[0] * w), int(roi[1] * h)
    x1, y1 = int(np.ceil((roi[0] + roi[2]) * w)), int(np.ceil((roi[1] + roi[3]) * h))
    return frame[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]


def get_roi(video_id: str) -> list[float] | None:
    """抽帧使用的幻灯片区域（0001 §2.5）：优先用户确认的区域；没有确认记录时（旧视频）
    用视频自己的设置，其次全局默认。整个画面返回 None。确认记录的读写见 pipeline/roi.py。"""
    from . import roi as roi_store  # 延迟导入：本模块的局部变量也叫 roi

    return roi_store.effective(video_id)


def get_roi_suggestion(video_id: str) -> list[float] | None:
    """框选界面的预填建议：视频上次保存的框，其次全局默认；都没有返回 None（整个画面）。"""
    from . import roi as roi_store

    return roi_store.suggestion(video_id)


def valid_roi(roi) -> bool:
    return (isinstance(roi, (list, tuple)) and len(roi) == 4
            and all(isinstance(v, (int, float)) for v in roi)
            and roi[0] >= 0 and roi[1] >= 0 and roi[2] >= 0.05 and roi[3] >= 0.05
            and roi[0] + roi[2] <= 1.001 and roi[1] + roi[3] <= 1.001)


def prepare(frame: np.ndarray, width: int, masks: Iterable[tuple[float, float, float, float]] = (),
            roi: Sequence[float] | None = None, box: Sequence[float] | None = None) -> np.ndarray:
    """BGR 或灰度帧 → 裁 ROI → 裁内容框 → 缩到宽 width 的灰度 uint8 → 遮罩置 0。

    roi 为整帧的相对坐标；box 为内容框在 ROI 裁剪图中的相对坐标（见 find_content_box）；
    masks 为 ROI 裁剪图中的相对坐标 (x, y, w, h)，这里自动换算到内容框内。
    """
    import cv2

    frame = crop_box(crop(frame, roi), box)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
    h, w = gray.shape
    if w != width:
        gray = cv2.resize(gray, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)
    return apply_masks(gray, masks_in_box(masks, box))


def crop_box(img: np.ndarray, box: Sequence[float] | None) -> np.ndarray:
    """按内容框（相对坐标 x, y, w, h）裁剪，边界四舍五入到像素；box 为空时原样返回。"""
    if not box:
        return img
    h, w = img.shape[:2]
    x0, y0 = round(box[0] * w), round(box[1] * h)
    x1, y1 = round((box[0] + box[2]) * w), round((box[1] + box[3]) * h)
    return img[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]


def masks_in_box(masks: Iterable[tuple[float, float, float, float]],
                 box: Sequence[float] | None) -> tuple[tuple[float, float, float, float], ...]:
    """把 ROI 相对坐标的遮罩换算成内容框相对坐标；落在框外的遮罩丢弃。"""
    masks = tuple(tuple(m) for m in masks)
    if not box:
        return masks
    bx, by, bw, bh = box
    out = []
    for x, y, w, h in masks:
        x0, y0 = max(0.0, (x - bx) / bw), max(0.0, (y - by) / bh)
        x1, y1 = min(1.0, (x + w - bx) / bw), min(1.0, (y + h - by) / bh)
        if x1 > x0 and y1 > y0:
            out.append((x0, y0, x1 - x0, y1 - y0))
    return tuple(out)


def find_content_box(median: np.ndarray) -> dict:
    """ROI 内中位灰度图 → 内容框（去黑边，交接 §3.2）。

    行、列中亮度 > BOX_LUMA 的像素占比 > BOX_LINE_FRAC 的范围即为内容框；
    面积 < ROI 的 BOX_MIN_AREA 时退回整个 ROI。
    返回 {"box": [x, y, w, h]（相对 ROI）, "px": [x0, y0, x1, y1], "size": [W, H], "fallback": bool}。
    """
    h, w = median.shape[:2]
    bright = median > BOX_LUMA
    rows = np.flatnonzero(bright.mean(axis=1) > BOX_LINE_FRAC)
    cols = np.flatnonzero(bright.mean(axis=0) > BOX_LINE_FRAC)
    px = [0, 0, w, h]
    fallback = True
    if len(rows) and len(cols):
        x0, y0, x1, y1 = int(cols[0]), int(rows[0]), int(cols[-1]) + 1, int(rows[-1]) + 1
        if (x1 - x0) * (y1 - y0) >= BOX_MIN_AREA * w * h:
            px, fallback = [x0, y0, x1, y1], False
    box = [px[0] / w, px[1] / h, (px[2] - px[0]) / w, (px[3] - px[1]) / h]
    return {"box": [round(v, 6) for v in box], "px": px, "size": [w, h], "fallback": fallback}


def estimate_content_box(cap, roi: Sequence[float] | None, duration: float = 0.0, n: int = BOX_SAMPLES) -> dict:
    """在视频中均匀 seek 取约 n 帧，裁 ROI、转灰度后求中位图，再求内容框。读不到帧时退回整个 ROI。"""
    import cv2

    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    grays = []
    for i in range(n):
        if frames > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int((i + 0.5) * frames / n))
        elif duration > 0:
            cap.set(cv2.CAP_PROP_POS_MSEC, (i + 0.5) * duration / n * 1000)
        elif i:
            break
        ok, f = cap.read()
        if not ok or f is None:
            continue
        g = crop(f, roi)
        g = cv2.cvtColor(g, cv2.COLOR_BGR2GRAY) if g.ndim == 3 else g
        if g.shape[1] > BOX_MAX_WIDTH:
            g = cv2.resize(g, (BOX_MAX_WIDTH, max(1, round(g.shape[0] * BOX_MAX_WIDTH / g.shape[1]))),
                           interpolation=cv2.INTER_AREA)
        if grays and g.shape != grays[0].shape:
            continue
        grays.append(g)
    if not grays:
        return {"box": [0.0, 0.0, 1.0, 1.0], "px": None, "size": None, "fallback": True, "samples": 0}
    median = np.median(np.stack(grays), axis=0)
    return {**find_content_box(median), "samples": len(grays)}


def apply_masks(gray: np.ndarray, masks: Iterable[tuple[float, float, float, float]]) -> np.ndarray:
    masks = list(masks)
    if not masks:
        return gray
    out = gray.copy()
    h, w = out.shape
    for x, y, mw, mh in masks:
        x0, y0 = int(x * w), int(y * h)
        x1, y1 = int(np.ceil((x + mw) * w)), int(np.ceil((y + mh) * h))
        out[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = 0
    return out


def frame_diff(a: np.ndarray, b: np.ndarray) -> float:
    """灰度差超过 PIXEL_DELTA 的像素占比（0~100）。

    不用整帧平均差：白底幻灯片只换文字时，变化被大片背景稀释，平均差只有 3~4。
    """
    changed = np.abs(a.astype(np.int16) - b.astype(np.int16)) > PIXEL_DELTA
    return float(changed.mean() * 100)


def valid_region(shape: tuple[int, ...], masks: Iterable[tuple[float, float, float, float]]) -> np.ndarray | None:
    """遮罩以外的像素为 True；没有遮罩时返回 None（全部有效）。masks 为小图的相对坐标。"""
    masks = tuple(masks)
    if not masks:
        return None
    return apply_masks(np.ones(shape[:2], np.uint8), masks) > 0


def ink_mask(img: np.ndarray, valid: np.ndarray | None = None) -> np.ndarray:
    """内容像素：与背景灰度（有效区域内的众数）相差 > INK_DELTA 的像素。"""
    px = img[valid] if valid is not None else img.ravel()
    if px.size == 0:
        return np.zeros(img.shape, bool)
    bg = int(np.bincount(px, minlength=256).argmax())
    ink = np.abs(img.astype(np.int16) - bg) > INK_DELTA
    return ink & valid if valid is not None else ink


def ink_percent(img: np.ndarray, valid: np.ndarray | None = None) -> float:
    """内容像素占有效区域的百分比（0~100）。"""
    n = int(valid.sum()) if valid is not None else img.size
    return float(ink_mask(img, valid).sum() / max(n, 1) * 100)


def is_reveal(old: np.ndarray, new: np.ndarray,
              min_ink: float = KeyframeParams.reveal_min_ink,
              max_added: float = KeyframeParams.reveal_max_added,
              valid: np.ndarray | None = None) -> bool:
    """new 是否只是在 old 上追加内容（逐条出现的要点）。三个条件同时满足（交接 §3.3）：
    - old 的内容像素在 new 中保留 ≥ REVEAL_KEEP；
    - old 的内容像素占比 ≥ min_ink%（几乎空白的标题页不算"展开"的起点）；
    - new 新增的内容像素 / old 内容像素 ≤ max_added（新增过多是换了一页，例如「Matrix」标题页 → 内容页）。

    不同的幻灯片即使版式相同，文字位置也会变，旧内容像素大量改变，返回 False。
    valid 为遮罩以外的区域；不传时把两帧都为 0 的像素当作遮罩。
    """
    if valid is None:
        valid = ~((old == 0) & (new == 0))  # 遮罩区域两帧都为 0，不参与判断
    if not valid.any():
        return False
    ink = ink_mask(old, valid)
    n = int(ink.sum())
    if n < old.size * 0.005 or n / valid.sum() * 100 < min_ink:  # 旧帧几乎空白，无从判断
        return False
    changed = np.abs(new.astype(np.int16) - old.astype(np.int16)) > INK_DELTA
    if 1 - (changed & ink).sum() / n < REVEAL_KEEP:
        return False
    added = int((ink_mask(new, valid) & ~ink).sum())
    return added / n <= max_added


def block_match(a: np.ndarray, b: np.ndarray, grid: Sequence[int] = KeyframeParams.block_grid,
                tol: float = KeyframeParams.block_tol, valid: np.ndarray | None = None) -> float:
    """分块比对得分（0~1，交接 §3.4）：两帧差 > INK_DELTA 的像素为"变化"，
    按 grid =（列, 行）分块，块内变化占比 ≤ tol 的块所占比例。全在遮罩内的块不计。
    """
    if a.shape != b.shape:
        import cv2
        b = cv2.resize(b, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    changed = (np.abs(a.astype(np.int16) - b.astype(np.int16)) > INK_DELTA).astype(np.int32)
    h, w = changed.shape
    ones = np.ones_like(changed)
    if valid is not None:
        changed, ones = changed * valid, valid.astype(np.int32)
    ys = np.unique(np.linspace(0, h, min(grid[1], h) + 1).astype(int))
    xs = np.unique(np.linspace(0, w, min(grid[0], w) + 1).astype(int))

    def block_sum(m: np.ndarray) -> np.ndarray:
        return np.add.reduceat(np.add.reduceat(m, ys[:-1], axis=0), xs[:-1], axis=1)

    area, ch = block_sum(ones), block_sum(changed)
    used = area > 0
    if not used.any():
        return 1.0
    return float((ch[used] <= tol * area[used]).mean())


def phash(gray: np.ndarray):
    import imagehash
    from PIL import Image

    return imagehash.phash(Image.fromarray(gray))


@dataclass
class Run:
    """一段稳定画面。small 与 payload 都取自该段最后一个采样（换页前的最后一帧）。"""
    start: float
    end: float
    small: np.ndarray
    payload: Any = None


class SlideTracker:
    """顺序喂入采样帧；检测到换页且上一段稳定 ≥ stable_s 时返回该段 Run。

    trace 记录每个采样的 [t, 与上一采样的差, 与本段首帧的差]，short_runs 记录因不够稳定被丢弃的段，
    供抽帧展示台核对算法。
    """

    def __init__(self, diff_threshold: float, stable_s: float):
        self.th = diff_threshold
        self.stable_s = stable_s
        self.start: float | None = None
        self.anchor: np.ndarray | None = None   # 本段首帧
        self.last: np.ndarray | None = None     # 上一采样
        self.last_payload: Any = None
        self.trace: list[list[float]] = []
        self.short_runs: list[list[float]] = []

    def feed(self, t: float, small: np.ndarray, payload: Any = None) -> Run | None:
        done = None
        if self.last is not None:
            d_prev, d_anchor = frame_diff(self.last, small), frame_diff(self.anchor, small)
            self.trace.append([round(t, 2), round(d_prev, 3), round(d_anchor, 3)])
            if d_prev > self.th or d_anchor > self.th * DRIFT_FACTOR:
                done = self._close(t)
                self.last = None
        else:
            self.trace.append([round(t, 2), 0.0, 0.0])
        if self.last is None:
            self.start, self.anchor = t, small
        self.last, self.last_payload = small, payload
        return done

    def finish(self, t_end: float) -> Run | None:
        return self._close(t_end) if self.last is not None else None

    def _close(self, t_end: float) -> Run | None:
        if t_end - self.start >= self.stable_s:
            return Run(self.start, t_end, self.last, self.last_payload)
        self.short_runs.append([round(self.start, 2), round(t_end, 2)])
        return None


@dataclass
class Appearance:
    """页的一次连续显示。small 为这次出现的代表帧（合并规则选出），ink 为其内容像素数。"""
    start: float
    end: float
    small: np.ndarray
    ink: int


@dataclass
class Slide:
    id: str
    shows: list[Appearance] = field(default_factory=list)
    similar_prev: dict | None = None

    @property
    def appearances(self) -> list[list[float]]:
        return [[a.start, a.end] for a in self.shows]

    @property
    def small(self) -> np.ndarray:
        return self.shows[0].small

    def image(self, k: int) -> str:
        """第 k 次出现（从 1 起）的截图文件名（不含扩展名）：S012、S012_2……与出现 ID S012.k 对应。"""
        return self.id if k == 1 else f"{self.id}_{k}"


@dataclass
class Placement:
    """SlideLibrary.add 的结果：稳定段归到哪一页的第几次出现，是否需要写入 / 覆盖这次出现的截图。"""
    slide: str
    kind: str            # new | merged | revisit
    k: int               # 第几次出现（从 1 起）
    save: bool
    image: str           # 这次出现的截图文件名（不含扩展名）
    reason: str | None = None   # merged 时：reveal（逐条展开）| same（内容相同）
    score: float | None = None  # 与比较对象的 block_match 得分


class SlideLibrary:
    """已保存的幻灯片（交接 §3.4）。

    - 紧接出现（间隔 ≤ stable_s）先与上一页这次出现的代表帧比较：逐条展开 → 合并并取新帧；
      block_match ≥ dup_threshold（或展开判定成立但内容像素变少，即只擦掉了少量内容）→ 合并并保留内容像素更多的一帧；
    - 否则与所有页的每次出现比较 block_match，取得分最高且 ≥ dup_threshold 者，作为翻回旧页，新增一次出现；
    - 都不满足则新建一页。
    """

    def __init__(self, params: KeyframeParams, valid: np.ndarray | None = None):
        self.p = params
        self.valid = valid                  # 遮罩以外的区域（小图坐标）；None 表示全部有效
        self.slides: list[Slide] = []
        self.prev: Slide | None = None      # 最近一次出现的幻灯片；空白段会把它清空

    def _match(self, a: np.ndarray, b: np.ndarray) -> float:
        return block_match(a, b, self.p.block_grid, self.p.block_tol, self.valid)

    def add(self, run: Run) -> Placement:
        p = self.p
        ink = int(ink_mask(run.small, self.valid).sum())
        prev = self.prev
        if prev is not None and run.start - prev.shows[-1].end <= p.stable_s:
            cur = prev.shows[-1]
            # 显式传有效区域：不用"两帧都为 0 即遮罩"的推断，否则纯黑的黑边、文字会被排除在内容像素之外
            valid = self.valid if self.valid is not None else np.ones(run.small.shape[:2], bool)
            reveal = is_reveal(cur.small, run.small, p.reveal_min_ink, p.reveal_max_added, valid)
            score = self._match(cur.small, run.small)
            reason, take = None, False
            if reveal and ink >= cur.ink:              # 逐条展开：取新帧（内容更完整）
                reason, take = "reveal", True
            elif reveal or score >= p.dup_threshold:   # 内容相同：保留内容像素更多的一帧
                reason, take = "same", ink > cur.ink
            if reason is not None:
                cur.end = run.end
                if take:
                    cur.small, cur.ink = run.small, ink
                k = len(prev.shows)
                return Placement(prev.id, "merged", k, take, prev.image(k), reason, round(score, 4))

        best: tuple[float, Slide] | None = None
        for s in self.slides:
            for a in s.shows:
                sc = self._match(a.small, run.small)
                if sc >= p.dup_threshold and (best is None or sc > best[0]):
                    best = (sc, s)
        if best is not None:                    # 翻回旧页：追加一次出现，另存这次的截图
            s = best[1]
            s.shows.append(Appearance(run.start, run.end, run.small, ink))
            self.prev = s
            k = len(s.shows)
            return Placement(s.id, "revisit", k, True, s.image(k), score=round(best[0], 4))

        s = Slide(f"S{len(self.slides) + 1:03d}", [Appearance(run.start, run.end, run.small, ink)])
        self.slides.append(s)
        self.prev = s
        return Placement(s.id, "new", 1, True, s.image(1))

    def fill_similar_prev(self) -> None:
        """每页与时间上紧邻的前一页（该页首次出现之前最近的那次出现）比较 block_match，
        得分在 [SIMILAR_MIN, dup_threshold) 时记为 similar_prev（交接 §3.7，供 V2 批注状态提示）。"""
        shows = sorted(((a, s) for s in self.slides for a in s.shows), key=lambda x: x[0].start)
        for i, (a, s) in enumerate(shows):
            if i == 0 or a is not s.shows[0]:
                continue
            pa, ps = shows[i - 1]
            sc = self._match(pa.small, a.small)
            s.similar_prev = ({"id": ps.id, "score": round(sc, 3)}
                              if SIMILAR_MIN <= sc < self.p.dup_threshold else None)


def detect_slides(samples: Iterable[tuple[float, np.ndarray, Any]], params: KeyframeParams,
                  t_end: float | Callable[[], float], on_image=None, debug: dict | None = None) -> list[dict]:
    """samples 为 (t, 已 prepare 的灰度小图, payload)。params.masks 为小图中的遮罩（相对坐标）。
    on_image(image_name, payload) 在需要写入 / 覆盖某次出现的截图时调用，image_name 为 S012、S012_2……
    t_end 为视频结束时间；可传函数，在读完全部采样后再取（容器时长可能缺失）。
    debug 传入 dict 时填入 trace、short_runs、runs（每个稳定段归到哪张幻灯片）、
    blank_runs（丢弃的空白段 [start, end, 内容像素%]），供展示台使用。

    返回 [{"id", "appearances": [[start, end], ...], "images": [每次出现的截图名],
           "similar_prev": {"id", "score"} | None}]。
    """
    tracker = SlideTracker(params.diff_threshold, params.stable_s)
    lib = SlideLibrary(params)
    runs: list[dict] = []
    blank_runs: list[list[float]] = []
    shape: list = [None]

    def handle(run: Run | None) -> None:
        if run is None:
            return
        if shape[0] != run.small.shape:
            shape[0] = run.small.shape
            lib.valid = valid_region(run.small.shape, params.masks)
        ink = ink_percent(run.small, lib.valid)
        if ink < params.blank_ink:          # 空白段（纯黑、"End of slide show"）：按无幻灯片处理
            blank_runs.append([round(run.start, 2), round(run.end, 2), round(ink, 3)])
            lib.prev = None
            return
        pl = lib.add(run)
        entry = {"start": round(run.start, 2), "end": round(run.end, 2), "slide": pl.slide, "kind": pl.kind,
                 "appearance": pl.k, "ink": round(ink, 2)}
        if pl.reason:
            entry["reason"] = pl.reason
        if pl.score is not None:
            entry["score"] = pl.score
        runs.append(entry)
        if pl.save and on_image:
            on_image(pl.image, run.payload)

    for t, small, payload in samples:
        handle(tracker.feed(t, small, payload))
    handle(tracker.finish(t_end() if callable(t_end) else t_end))
    lib.fill_similar_prev()
    if debug is not None:
        debug.update(trace=tracker.trace, short_runs=tracker.short_runs, runs=runs, blank_runs=blank_runs)
    return [{"id": s.id, "appearances": [[round(a, 2), round(b, 2)] for a, b in s.appearances],
             "images": [s.image(k) for k in range(1, len(s.shows) + 1)], "similar_prev": s.similar_prev}
            for s in lib.slides]


# ---------- 读视频 ----------

def iter_samples(cap, interval_s: float, prepare_fn, fps: float, on_time=None):
    """顺序 grab 每一帧，只在采样点 retrieve 解码成图像（比按时间 seek 快得多）。"""
    import cv2

    idx, next_t = 0, 0.0
    while cap.grab():
        t = idx / fps if fps > 0 else cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        idx += 1
        if t + 1e-6 < next_t:
            continue
        ok, frame = cap.retrieve()
        if not ok or frame is None:
            continue
        while next_t <= t + 1e-6:
            next_t += interval_s
        if on_time:
            on_time(t)
        yield t, prepare_fn(frame), frame


def process_video(src, roi: Sequence[float] | None, p: KeyframeParams, out_dir, duration: float = 0.0,
                  progress: Callable[[float, str], None] | None = None) -> tuple[list[dict], dict]:
    """读视频 src，求内容框、切分幻灯片，把每次出现的截图写到 out_dir/S###[_k].jpg。

    返回 (keyframes.json 的 data, keyframes_debug.json 的内容)。不清理 out_dir 里的旧图，由调用方负责。
    """
    import cv2
    from pathlib import Path

    out_dir = Path(out_dir)
    progress = progress or (lambda f, m: None)
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise StageError("OpenCV 无法打开视频文件，无法切分幻灯片")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if not 0 < fps < 1000:
        fps = 0.0
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = duration or (frames / fps if fps and frames > 0 else 0.0)

    progress(0.0, "正在确定幻灯片内容框")
    try:
        cb = estimate_content_box(cap, roi, duration)
    finally:
        cap.release()
    box = None if cb["fallback"] else cb["box"]
    cap = cv2.VideoCapture(str(src))    # 重新打开，从头顺序读（seek 回 0 在部分容器上不可靠）
    if not cap.isOpened():
        raise StageError("OpenCV 无法打开视频文件，无法切分幻灯片")

    def save(name: str, frame: np.ndarray) -> None:
        ok, buf = cv2.imencode(".jpg", crop_box(crop(frame, roi), box), [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise StageError(f"保存关键帧 {name} 失败")
        (out_dir / f"{name}.jpg").write_bytes(buf.tobytes())  # 避免 cv2.imwrite 在 Windows 非 ASCII 路径失败

    count = [0]
    last_t = [0.0]

    def on_time(t: float) -> None:
        last_t[0] = t
        count[0] += 1
        if duration and count[0] % 30 == 0:
            progress(t / duration, "正在切分幻灯片")

    progress(0.0, "正在切分幻灯片")
    debug: dict = {}
    # 判定在内容框小图上进行，遮罩换算到内容框坐标
    dp = replace(p, masks=masks_in_box(p.masks, box))
    try:
        samples = iter_samples(cap, p.interval_s, lambda f: prepare(f, p.width, p.masks, roi=roi, box=box),
                               fps, on_time)
        slides = detect_slides(samples, dp, lambda: max(duration, last_t[0] + p.interval_s), save, debug)
    finally:
        cap.release()

    data = [{"id": s["id"], "image": f"keyframes/{s['id']}.jpg", "appearances": s["appearances"],
             "appearance_images": [f"keyframes/{n}.jpg" for n in s["images"]],
             "similar_prev": s["similar_prev"]}
            for s in slides]
    debug = {
        "duration_s": round(max(duration, last_t[0]), 2),
        "threshold": p.diff_threshold, "drift_threshold": p.diff_threshold * DRIFT_FACTOR, "roi": roi,
        # 内容框：box 为 ROI 裁剪图中的相对坐标 (x, y, w, h)；px 为 size（求框时的图像尺寸）下的像素 [x0, y0, x1, y1]
        "content_box": cb["box"], "content_box_px": cb["px"], "content_box_size": cb["size"],
        "content_box_fallback": cb["fallback"],
        **debug,
    }
    return data, debug


def run(ctx: StageContext) -> None:
    p = ctx.settings.keyframe
    src = ctx.source
    if not src.exists():
        raise StageError("找不到视频文件 source.mp4")
    duration = 0.0
    try:
        duration = float(ctx.read("audio.json")["duration_s"]) or 0.0
    except (FileNotFoundError, KeyError, TypeError, ValueError):
        pass

    roi = get_roi(ctx.video_id)
    if roi is not None and not valid_roi(roi):
        raise StageError("幻灯片区域设置无效，请在抽帧展示台重新框选")

    out_dir = ctx.dir / "keyframes"
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("S*.jpg"):  # 重跑时清掉旧图（含 S012_2.jpg 这类出现截图）
        old.unlink()

    data, debug = process_video(src, roi, p, out_dir, duration, ctx.progress)
    # 先写调试数据再写主产物：主产物存在即代表本阶段完成
    ctx.write("keyframes_debug.json", debug)
    ctx.write("keyframes.json", data)
    ctx.progress(1.0, f"得到 {len(data)} 张幻灯片")
