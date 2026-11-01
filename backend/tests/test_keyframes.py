"""关键帧判定：用合成帧（numpy 数组），不依赖真实视频。"""
import numpy as np

from app.config import KeyframeParams
from app.pipeline.keyframes import (Run, SlideLibrary, apply_masks, block_match, detect_slides,
                                    find_content_box, frame_diff, ink_percent, is_reveal, masks_in_box,
                                    prepare)

H, W = 180, 320
P = KeyframeParams()


def slide(seed: int) -> np.ndarray:
    """白底 + 若干深色"文字行"，不同 seed 版式不同。"""
    rng = np.random.default_rng(seed)
    img = np.full((H, W), 250, np.uint8)
    img[10:30, 20:300] = 30  # 标题
    for _ in range(6):
        y, x = rng.integers(40, 170), rng.integers(10, 200)
        img[y:y + 8, x:x + rng.integers(40, 120)] = 20
    return img


def noise(seed: int) -> np.ndarray:
    return np.random.default_rng(1000 + seed).integers(0, 256, (H, W), dtype=np.uint8)


def run_seq(frames, t_end=None, params=P, debug=None):
    """frames[i] 为第 i 秒的采样；payload 用采样时间，便于检查保存的是哪一帧。"""
    saved = []
    samples = ((float(t), f, float(t)) for t, f in enumerate(frames))
    slides = detect_slides(samples, params, float(t_end if t_end is not None else len(frames)),
                           lambda name, payload: saved.append((name, payload)), debug)
    return slides, saved


def brief(slides):
    return [{"id": s["id"], "appearances": s["appearances"]} for s in slides]


def test_synthetic_slides_are_distinct():
    a, b = slide(1), slide(2)
    assert frame_diff(a, b) > P.diff_threshold
    assert block_match(a, b) < P.dup_threshold


def test_page_turn_and_flip_back():
    a, b = slide(1), slide(2)
    slides, saved = run_seq([a] * 10 + [b] * 10 + [a] * 10)
    assert brief(slides) == [{"id": "S001", "appearances": [[0, 10], [20, 30]]},
                             {"id": "S002", "appearances": [[10, 20]]}]
    # 每次出现保存换页前的最后一帧；翻回旧页另存一张 S001_2（交接 §3.7.1）
    assert saved == [("S001", 9.0), ("S002", 19.0), ("S001_2", 29.0)]
    assert slides[0]["images"] == ["S001", "S001_2"] and slides[1]["images"] == ["S002"]
    for s in slides:
        assert len(s["images"]) == len(s["appearances"])


def test_short_unstable_period_ignored():
    frames = [slide(1)] * 10 + [noise(1), noise(2)] + [slide(2)] * 8
    slides, _ = run_seq(frames, t_end=21)
    assert [s["appearances"] for s in slides] == [[[0, 10]], [[12, 21]]]


def test_camera_only_gives_no_slides():
    slides, saved = run_seq([noise(i) for i in range(30)])
    assert slides == [] and saved == []


def test_too_short_slide_ignored():
    slides, _ = run_seq([slide(1)] * 10 + [slide(2)] * 2 + [slide(3)] * 10)
    assert len(slides) == 2  # 只停留 2 秒的页不算


def test_bullet_reveal_keeps_last_frame():
    base = slide(5)
    frames = []
    for i in range(12):
        f = base.copy()
        for k in range(i // 3):  # 每 3 秒出现一条要点（小面积变化）
            f[100 + k * 20:108 + k * 20, 200:310] = 0
        frames.append(f)
    # 每条要点出现都会被判为换页，再由 is_reveal 合并回同一张
    debug: dict = {}
    slides, saved = run_seq(frames, debug=debug)
    assert brief(slides) == [{"id": "S001", "appearances": [[0, 12]]}]
    assert {name for name, _ in saved} == {"S001"}
    assert saved[-1] == ("S001", 11.0)  # 最终图片是要点全部出现后的那一帧
    assert {r.get("reason") for r in debug["runs"][1:]} == {"reveal"}


def test_white_slides_with_small_text_change_are_split():
    # 白底幻灯片只换一小块文字：整帧平均差很小，但必须识别为两张
    a = np.full((180, 320), 255, np.uint8)
    a[20:36, 20:200] = 0
    b = np.full((180, 320), 255, np.uint8)
    b[20:36, 20:140] = 0
    b[60:70, 20:180] = 0
    slides, _ = run_seq([a] * 6 + [b] * 6)
    assert [s["id"] for s in slides] == ["S001", "S002"]


def test_big_reveal_merged_as_same_slide():
    # 一次出现大块内容：被判为换页，但旧内容全部保留、新增不多且紧接出现 → 合并成一张，图片换成更完整的
    part = slide(7)
    full = part.copy()
    full[120:175, 200:315] = 60
    assert frame_diff(part, full) > P.diff_threshold
    assert is_reveal(part, full)
    slides, saved = run_seq([part] * 5 + [full] * 6 + [slide(8)] * 5)
    assert brief(slides)[0] == {"id": "S001", "appearances": [[0, 11]]}
    assert [s for s in saved if s[0] == "S001"] == [("S001", 4.0), ("S001", 10.0)]
    assert slides[1]["id"] == "S002"


def test_same_template_different_slide_not_merged():
    # 同一版式（标题相同）的相邻两页不是"追加内容"，不能合并
    for i in range(1, 6):
        assert not is_reveal(slide(i), slide(i + 10))
    slides, _ = run_seq([slide(1)] * 5 + [slide(11)] * 5)
    assert len(slides) == 2


def test_is_reveal_ignores_masked_region():
    # 遮罩占大半画面时背景色仍按未遮罩部分估计；两张不同页不能因遮罩区域相同而被当成追加内容
    masks = ((0.4, 0.0, 0.6, 1.0),)
    a, b = apply_masks(slide(1), masks), apply_masks(slide(2), masks)
    assert frame_diff(a, b) > 0
    assert not is_reveal(a, b)
    full = a.copy()
    full[150:170, 10:120] = 0
    assert is_reveal(a, full)


def test_mask_ignores_webcam_region():
    a = slide(3)
    frames = []
    for i in range(12):
        f = a.copy()
        f[:, 192:] = noise(i)[:, 192:]  # 右侧 40% 是讲师小窗，每帧都在变
        frames.append(f)
    slides, _ = run_seq(frames)
    assert slides == []

    masks = ((0.6, 0.0, 0.4, 1.0),)
    masked = [apply_masks(f, masks) for f in frames]
    slides, _ = run_seq(masked)
    assert brief(slides) == [{"id": "S001", "appearances": [[0, 12]]}]


def test_prepare_resizes_bgr_and_masks():
    bgr = np.full((720, 1280, 3), 200, np.uint8)
    small = prepare(bgr, 320, masks=((0.5, 0.5, 0.5, 0.5),))
    assert small.shape == (180, 320) and small.dtype == np.uint8
    assert small[0, 0] == 200 and small[179, 319] == 0 and small[90, 160] == 0 and small[89, 159] == 200


def test_library_flip_back_requires_close_pixels():
    lib = SlideLibrary(P)
    a, b = slide(1), slide(2)
    first = lib.add(Run(0, 10, a))
    assert (first.slide, first.kind, first.save, first.image) == ("S001", "new", True, "S001")
    assert lib.add(Run(10, 20, b)).slide == "S002"
    back = lib.add(Run(20, 30, a))
    assert (back.slide, back.kind, back.k, back.save, back.image) == ("S001", "revisit", 2, True, "S001_2")
    assert lib.slides[0].appearances == [[0, 10], [20, 30]]


# ---------- 提案 0001 §3：内容框、逐条展开条件、分块比对、空白帧、每次出现的截图 ----------

BORDER_Y, BORDER_X = 15, 20   # 合成帧的黑边


def letterbox(content: np.ndarray) -> np.ndarray:
    """把内容图（白底）放进带黑边的 180×320 画面中，模拟 ROI 裁出的画面带黑边。"""
    img = np.full((H, W), 8, np.uint8)          # 视频里的黑边接近但不等于 0
    img[BORDER_Y:H - BORDER_Y, BORDER_X:W - BORDER_X] = content
    return img


def test_find_content_box_trims_letterbox_and_falls_back():
    frames = [letterbox(np.full((H - 2 * BORDER_Y, W - 2 * BORDER_X), 250, np.uint8))] * 3
    cb = find_content_box(np.median(np.stack(frames), axis=0))
    assert not cb["fallback"] and cb["px"] == [BORDER_X, BORDER_Y, W - BORDER_X, H - BORDER_Y]
    x, y, w, h = cb["box"]
    assert abs(x - BORDER_X / W) < 1e-4 and abs(w - (W - 2 * BORDER_X) / W) < 1e-4
    # 深色背景的幻灯片：亮像素太少，框面积 < ROI 的 50%，退回整个 ROI
    dark = np.full((H, W), 20, np.uint8)
    dark[60:100, 60:200] = 255
    cb = find_content_box(dark)
    assert cb["fallback"] and cb["box"] == [0.0, 0.0, 1.0, 1.0]
    # 裁框后遮罩坐标（相对 ROI）换算到框内
    assert masks_in_box(((0.0, 0.0, 0.5, 1.0),), (0.25, 0.0, 0.5, 1.0)) == ((0.0, 0.0, 0.5, 1.0),)
    assert masks_in_box(((0.0, 0.0, 0.1, 1.0),), (0.25, 0.0, 0.5, 1.0)) == ()


def test_letterboxed_title_page_then_content_page_not_reveal():
    """§3.8：带黑边的"标题页 → 内容页"不判为展开（测试课「Matrix」标题页被并进内容页的根因）。"""
    ch, cw = H - 2 * BORDER_Y, W - 2 * BORDER_X
    title = np.full((ch, cw), 250, np.uint8)
    title[60:75, 105:175] = 20                   # 居中的大标题，约占内容区 2.5%
    content = title.copy()
    for k in range(6):                           # 正文，新增约为标题的 9 倍
        content[85 + k * 10:93 + k * 10, 40:240] = 20
    t_raw, c_raw = letterbox(title), letterbox(content)
    # 不去黑边：黑边被当成"内容"，旧内容多、新增占比小，被误判为逐条展开
    assert is_reveal(t_raw, c_raw, min_ink=0, max_added=float("inf"))
    assert is_reveal(t_raw, c_raw)               # 新条件在带黑边时也挡不住

    cb = find_content_box(np.median(np.stack([t_raw, c_raw]), axis=0))
    t, c = (prepare(f, W, box=cb["box"]) for f in (t_raw, c_raw))
    assert 2.0 < ink_percent(t) < P.reveal_min_ink
    assert not is_reveal(t, c)
    slides, _ = run_seq([t] * 10 + [c] * 10)
    assert [s["id"] for s in slides] == ["S001", "S002"]


def toolbar_page(page_no: int, toolbar: bool) -> np.ndarray:
    img = slide(21)
    img[160:170, 290:300 + page_no % 2 * 6] = 20   # 页码（36 / 37）
    if toolbar:
        img[0:9, :] = 232                           # 浅灰工具栏：与白底只差 18，不算内容
        img[2:6, 225:229] = 140                     # 两个淡色图标
        img[2:6, 265:269] = 140
    return img


def test_same_content_with_toolbar_is_same_page():
    """§3.8：两张内容相同、顶部多一条工具栏（且页码不同）的页判为同一页（测试课旧 S037 / S040）。"""
    a, b = toolbar_page(36, True), toolbar_page(37, False)
    assert block_match(a, b) >= P.dup_threshold
    slides, saved = run_seq([a] * 10 + [slide(22)] * 10 + [b] * 10)
    assert brief(slides) == [{"id": "S001", "appearances": [[0, 10], [20, 30]]},
                             {"id": "S002", "appearances": [[10, 20]]}]
    assert ("S001_2", 29.0) in saved


def test_black_frames_dropped():
    """§3.8：纯黑画面、黑底小字（"End of slide show"）不成为幻灯片，该时段按无幻灯片处理。"""
    black = np.zeros((H, W), np.uint8)
    end_show = black.copy()
    end_show[5:12, 5:33] = 230                   # 顶部一行小字，约 0.34%（测试课实测 End of slide show 为 0.34%）
    assert ink_percent(black) == 0 and ink_percent(end_show) < P.blank_ink
    debug: dict = {}
    slides, saved = run_seq([slide(1)] * 10 + [black] * 10 + [slide(1)] * 10 + [end_show] * 10, debug=debug)
    # 空白段打断紧接合并：黑屏后回到同一页记为第 2 次出现
    assert brief(slides) == [{"id": "S001", "appearances": [[0, 10], [20, 30]]}]
    assert [b[:2] for b in debug["blank_runs"]] == [[10, 20], [30, 40]]
    assert [n for n, _ in saved] == ["S001", "S001_2"]


def titled_page(title_seed: int) -> np.ndarray:
    """同一模板（页眉条 + 相同正文），只有标题文字不同。"""
    img = np.full((H, W), 250, np.uint8)
    img[0:6, :] = 90                             # 模板页眉条
    for k in range(5):                           # 正文相同
        img[70 + k * 18:78 + k * 18, 30:30 + 50 * (k % 3 + 3)] = 20
    rng = np.random.default_rng(title_seed)
    x = 20
    while x < 280:                               # 标题：一串宽度随机的"字母"
        gw = int(rng.integers(6, 14))
        top = int(rng.integers(20, 26))
        img[top:44, x:x + gw] = 25
        x += gw + int(rng.integers(3, 7))
    return img


def test_same_template_only_title_differs_not_same_page():
    """§3.8：同模板、只有标题文字不同的两页不判为同一页；相邻两页记录 similar_prev。"""
    a, b = titled_page(1), titled_page(2)
    score = block_match(a, b)
    assert 0.5 <= score < P.dup_threshold
    assert not is_reveal(a, b)
    slides, _ = run_seq([a] * 10 + [b] * 10)
    assert [s["id"] for s in slides] == ["S001", "S002"]
    assert slides[0]["similar_prev"] is None
    assert slides[1]["similar_prev"] == {"id": "S001", "score": round(score, 3)}
    # 完全不同的相邻页得分 < 0.5，不记录
    slides, _ = run_seq([slide(1)] * 10 + [noise_free_page()] * 10)
    assert slides[1]["similar_prev"] is None


def noise_free_page() -> np.ndarray:
    img = np.full((H, W), 250, np.uint8)
    img[::6, :] = 20                             # 满屏横线，与普通页几乎每块都不同
    return img


def annotated(base: np.ndarray) -> np.ndarray:
    f = base.copy()
    f[122:146, 262:292] = 60                     # 一团手写批注（约 1.25%，落在 2×2 个块内）
    return f


def test_annotation_then_erase_keeps_annotated_frame():
    """§3.4：同一页先有批注、后擦掉，内容相同的紧接合并保留内容像素更多（有批注）的那帧。"""
    clean = slide(9)
    marked = annotated(clean)
    assert frame_diff(marked, clean) > P.diff_threshold    # 擦掉批注会被判为换页
    assert block_match(marked, clean) >= P.dup_threshold
    debug: dict = {}
    slides, saved = run_seq([marked] * 6 + [clean] * 6, debug=debug)
    assert brief(slides) == [{"id": "S001", "appearances": [[0, 12]]}]
    assert saved == [("S001", 5.0)]              # 没有被擦干净的帧覆盖
    assert debug["runs"][1]["kind"] == "merged" and debug["runs"][1]["reason"] == "same"

    # 干净 → 写上批注（展开，取新帧）→ 擦掉（保留有批注的帧）
    slides, saved = run_seq([clean] * 6 + [marked] * 6 + [clean] * 6)
    assert brief(slides) == [{"id": "S001", "appearances": [[0, 18]]}]
    assert saved == [("S001", 5.0), ("S001", 11.0)]


def test_flip_back_saves_each_appearance_image():
    """§3.7.1：翻回旧页产生 _2、_3 截图，images 与 appearances 等长；这次出现内的合并覆盖这次的截图。"""
    a, b = slide(1), slide(2)
    marked = annotated(a)
    slides, saved = run_seq([a] * 5 + [b] * 5 + [a] * 5 + [marked] * 5 + [b] * 5 + [a] * 5)
    s1 = next(s for s in slides if s["id"] == "S001")
    assert s1["appearances"] == [[0, 5], [10, 20], [25, 30]]
    assert s1["images"] == ["S001", "S001_2", "S001_3"]
    assert len(s1["images"]) == len(s1["appearances"])
    # 第 2 次出现先存 14 秒的帧，写上批注后被 19 秒的帧覆盖；第 1 次出现的截图不受影响
    assert [x for x in saved if x[0].startswith("S001")] == [
        ("S001", 4.0), ("S001_2", 14.0), ("S001_2", 19.0), ("S001_3", 29.0)]


def test_run_on_encoded_video(tmp_path, tmp_settings):
    """端到端：ffmpeg 把三张合成幻灯片编码成视频（A 5 秒 → B 5 秒 → A 4 秒），跑完整阶段。"""
    import shutil
    import subprocess

    import cv2
    import pytest

    from app.pipeline import keyframes
    from app.pipeline.base import StageContext

    if shutil.which("ffmpeg") is None:
        pytest.skip("需要 ffmpeg")
    ctx = StageContext(video_id="v1", stage="keyframes", settings=tmp_settings)
    ctx.dir.mkdir(parents=True)
    imgs = []
    for name, seed in (("a", 1), ("b", 2)):
        big = cv2.resize(slide(seed), (640, 360), interpolation=cv2.INTER_NEAREST)
        p = tmp_path / f"{name}.png"
        cv2.imwrite(str(p), big)
        imgs.append(p)
    args = ["ffmpeg", "-y", "-v", "error"]
    for p, d in ((imgs[0], 5), (imgs[1], 5), (imgs[0], 4)):
        args += ["-loop", "1", "-framerate", "25", "-t", str(d), "-i", str(p)]
    args += ["-filter_complex", "[0][1][2]concat=n=3:v=1:a=0,format=yuv420p", "-c:v", "libx264",
             str(ctx.source)]
    subprocess.run(args, check=True)
    (ctx.dir / "keyframes").mkdir()
    (ctx.dir / "keyframes" / "S009.jpg").write_bytes(b"old")  # 重跑时应清掉

    keyframes.run(ctx)
    data = ctx.read("keyframes.json")
    assert [d["id"] for d in data] == ["S001", "S002"]
    assert data[0]["image"] == "keyframes/S001.jpg"
    assert len(data[0]["appearances"]) == 2 and len(data[1]["appearances"]) == 1
    assert data[0]["appearance_images"] == ["keyframes/S001.jpg", "keyframes/S001_2.jpg"]
    assert data[1]["appearance_images"] == ["keyframes/S002.jpg"]
    assert "similar_prev" in data[0]
    a1, a2 = data[0]["appearances"]
    assert a1[0] == 0 and abs(a1[1] - 5) <= 1 and abs(a2[0] - 10) <= 1 and abs(a2[1] - 14) <= 1
    img = cv2.imread(str(ctx.dir / "keyframes" / "S002.jpg"))
    assert img.shape == (360, 640, 3)  # 原分辨率（白底无黑边，内容框即整个画面）
    assert sorted(p.name for p in (ctx.dir / "keyframes").iterdir()) == ["S001.jpg", "S001_2.jpg", "S002.jpg"]
    debug = ctx.read("keyframes_debug.json")
    assert debug["content_box"] == [0.0, 0.0, 1.0, 1.0] and debug["blank_runs"] == []
