# 0029：整课转写与按时间读取关键帧

2026-11-08，用户已授权实施。单课问答先读取完整原文，再按时间核对画面；不等待翻译、读图、大纲或 evidence.json。

## 契约

- `read_video_transcript(video_id, text_offset=0, version=None)`：直接读最终 `transcript.json` 的 ASR segments，只返回原文和秒级起止时间，不重复词级数据或译文。保留实际 language，不调用翻译模型。默认正文上限 160,000 字符，当前约三小时英文课程预计整课一页。超限按字符分页；返回 total_chars、segment_count、next_text_offset、complete、独立源文件版本。续页必须携带首轮 version，源数据变化时拒绝拼接。
- `view_keyframe(video_id, timestamp, offset=0, version=None)`：直接读 `keyframes.json`，按 `[start,end)` 找到全部匹配出现；每次返回一张图片及其 page_id、appearance_index、起止时间、关键帧产物版本和分页位置。优先对应出现的截图；旧数据缺少出现截图时明确标识为代表图。空档返回空匹配，不猜最近页面。不现场抽帧。续页使用该工具自身版本，不使用转写版本。
- 复用 MCP 标准文本/structuredContent/ImageContent、路径边界、只读数据库和图像编码。保留八个既有工具语义；新增两个工具。源文件缺失、损坏、版本变化或越界返回明确错误。
- 转写是当前最终 ASR 文件的全文，不保证识别完整或正确。关键帧是所匹配区间的已保存代表画面，不保证是查询毫秒的逐帧截图。

## 影响与验收

修改 MCP 注册、独立源产物读取、McpParams、标准客户端测试及文档；不改 pipeline 数据格式、模型配置、调度或网页问答。测试 ASR-only 数据、超限无损拼接、版本变化、时间边界/重叠/空档、回看时不同截图、损坏数据、路径逃逸。标准 stdio/HTTP 客户端验证新工具并确认磁盘不变，真实课程只读统计全文长度与时间取图。
