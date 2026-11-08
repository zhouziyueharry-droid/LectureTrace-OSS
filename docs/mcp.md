# MCP 接入与工具契约

LectureTrace 的 MCP 是通用协议服务，不依赖 Codex 登录、app-server 或专有工具格式。支持 MCP 的客户端可选择 stdio 或 Streamable HTTP；客户端需要支持图片工具结果且所选模型具备视觉能力，才能利用幻灯片图片。

读取已经处理的课程。八个工具不导入、删除、重跑视频，不写笔记、不调用模型。图片定位接收客户端明确提供的查询图片，仅在内存里处理。MCP 本身不产生模型调用费用，但客户端使用返回的课程材料时仍遵循该客户端的数据处理和计费方式。

## 安装

全新轻量环境：

```sh
uv sync --directory backend --locked --extra mcp
```

已有 CUDA/ASR/OCR 环境同步时应继续带上原有 extras。若只增量安装 MCP，避免 uv sync 移除既有可选包：

```sh
uv pip install --python backend/.venv/Scripts/python.exe "mcp>=1.30,<2"
```

macOS/Linux 的 Python 路径为 `backend/.venv/bin/python`。SDK 锁定为兼容的 v1 maintenance line；底层协议版本由标准初始化协商。

## stdio：本地客户端优先

服务器入口：在 `backend` 目录执行 `python -m app.mcp_server`。不需要启动网页后端。使用与网页相同的 `DATA_DIR`；未显式指定时读取项目 `.env`。数据库不存在会报错，不会创建一个空课程库。

以下是常见客户端的 `mcpServers` 配置示例，具体配置文件位置由客户端决定。替换三个绝对路径；不要把 Python 入口改成 `app.main`：

```json
{
  "mcpServers": {
    "lecturetrace": {
      "command": "C:/path/LectureTrace-OSS/backend/.venv/Scripts/python.exe",
      "args": ["-m", "app.mcp_server"],
      "env": {
        "PYTHONPATH": "C:/path/LectureTrace-OSS/backend",
        "DATA_DIR": "C:/path/LectureTrace-OSS/data"
      }
    }
  }
}
```

Codex 使用同一入口，例如：

```sh
codex mcp add lecturetrace --env PYTHONPATH=C:/path/LectureTrace-OSS/backend --env DATA_DIR=C:/path/LectureTrace-OSS/data -- C:/path/LectureTrace-OSS/backend/.venv/Scripts/python.exe -m app.mcp_server
codex mcp get lecturetrace
```

添加后让客户端重新加载 MCP 连接；已打开的对话可能需要重启客户端或新开对话。示例提问：“使用 lecturetrace 列出已有视频，再读取一段讲解和对应幻灯片。”不要把 stdio 输出重定向到普通日志；SDK 协议使用 stdout，日志使用 stderr。

## Streamable HTTP：复用网页后端

1. 在未提交的项目 `.env` 中设置一个随机 `LECTURETRACE_MCP_TOKEN`（建议用 `secrets.token_urlsafe(32)` 生成）。它是 MCP 访问令牌，不是模型 API Key。
2. 安装 mcp extra 后按平时方式启动/重启后端。
3. 客户端连接 `http://127.0.0.1:8000/mcp`，发送 `Authorization: Bearer <token>`。

未设置 token 返回 503，缺少或错误 token 返回 401。服务校验 Host/Origin，只面向同机客户端；不是公网 OAuth 服务，不要通过改绑定地址把课程暴露到公网。仅支持远程 HTTPS/OAuth 的托管客户端不能直接连接这个本机端点，可使用支持本地执行的客户端。

HTTP 共享网页的现有处理队列；连接 MCP 本身不会提交任务，但正常网页后端启动可能恢复已有处理任务。stdio 不启动该队列。

## 十个工具

| 工具 | 参数 | 结果与下一步 |
| --- | --- | --- |
| `list_videos` | `offset=0` | 视频 ID、课程名、时长、状态；`next_offset` 分页 |
| `read_video_transcript` | `video_id, text_offset=0, version=None` | 直接读取整课原始 ASR 转写与秒级起止时间，默认最多 160,000 字符；超限按 `next_text_offset` 续读，无需翻译、读图、大纲或 evidence |
| `view_keyframe` | `video_id, timestamp, offset=0, version=None` | 直接按显示时间查询已有关键帧，每次最多一张 JPEG；重叠匹配按 `next_offset` 续读，无需 evidence |
| `get_video_context` | `video_id, offset=0` | 章节标题、单元简介和时间目录、`version`；`next_offset` 分页。目录不证明读过原文 |
| `locate_slide` | `video_id, image_base64, offset=0` | 在指定视频内匹配 PNG/JPEG 页面；返回候选 `page_id`、`unit_id`、`timestamp`、结束时间、几何匹配诊断和 `version`，不附摘要、转写或图片；`next_offset` 分页 |
| `get_video_units` | `video_id, version, timestamp, offset=0` | 返回该秒对应的单元简介、显示/语音边界、摘要状态和读取入口；不返回转写或图片 |
| `read_video_unit` | `video_id, version, unit_id, text_offset=0` | 返回一次出现单元所属的全部 T 转写；按 `next_text_offset` 续读，不额外附图 |
| `search_video` | `video_id, version, queries, offset=0` | 摘录、ID、起始秒数；`next_offset` 分页，需再读取正文 |
| `read_evidence` | `video_id, version, ids, text_offset=0` | 带 ID、时间、来源标识的文字；支持 `T0001-T0005`；`next_text_offset` 继续相同 ids 的文本 |
| `view_slide` | `video_id, version, id` | 标准 JPEG `ImageContent`，另附视频/证据 ID、时间与版本 |

所有正常结果都有 `content` 中的 JSON 文本和等价 `structuredContent`，旧客户端可只使用文本。图片作为独立标准内容块返回，不要求客户端访问服务器磁盘路径。业务失败使用 `isError=true`，不把内部磁盘路径/数据库异常返回给客户端。

从 context 获得 version 后传给其他证据工具；证据变化会拒绝过期请求，重新获取 context 再继续。`read_video_transcript` 和 `view_keyframe` 各自返回源产物的独立 version，不接受另一个工具或 context 的版本。请求没有全局“当前视频”，每次显式指定 ID，避免不同对话串课。MCP 不保存聊天记录和“已读”日志。

## 全课原文 → 时间戳 → 画面

单课全文问答可直接使用 `list_videos` → `read_video_transcript(video_id)`。只读最终 `transcript.json`，不读取 partial、不调用模型；返回实际 ASR language，英文课程保持英文，不补翻译。每个原始 ASR segment 格式为 `[开始秒-结束秒] 原文`，保留毫秒精度，省略词级时间戳与译文。`segment_count` 是源 segment 数量，`total_chars` 是格式化全文字符数。原始转写可能识别错误或漏识别，不保证包含录音中的全部发言。

默认正文上限为 160,000 字符（不是 tokens），超过才分页。`complete=true` 仅表示这一次从头返回了整份文本；否则应携带首轮 `version` 和相同 video_id，按 `next_text_offset` 继续，直到 null。末页的 complete 仍为 false；只有客户端拼齐所有页才能声称已读完整。续页不带 version 或源内容已变化时拒绝请求，避免混合两个版本。返回的是文件快照，已有最终转写文件在重新处理期间也可能是上一次结果。

从文本选取相关秒数，调用 `view_keyframe(video_id, timestamp)`，首次不要传 transcript 的 version。该工具只读取 `keyframes.json` 与已登记图片，按原始显示区间 `[start,end)` 匹配，不依赖 segments、slides、outline 或 evidence。每次返回 `matches`（最多一项）与独立 ImageContent，包含 page_id、appearance_index（源 appearances 数组的一基序号）、start/end、image_role。同页回看优先读取对应出现截图；旧数据缺少该图片条目时退回页代表图，并标为 `page_representative`。已登记路径指向缺失/损坏图片时明确报错，不悄悄替代。

空档返回空 matches、无图片；重叠区间保留全部候选，用该工具自己的 version、相同 timestamp 与 next_offset 逐张读取。截图是区间对应的已有代表画面，不是现场从视频抽取的精确时刻画面，不保证包含查询时刻所有批注变化。两个工具都不启动处理、不写课程文件，不修改既有八工具语义。

## 从 PPT 页面到老师讲解

推荐调用顺序：`locate_slide` → `get_video_units` → 按问题选择 `read_video_unit` / `view_slide`。图片定位返回的 version 同样可用于后续读取，不必先加载整讲目录。

1. 客户端将当前 PPT/PDF 页渲染成 PNG/JPEG，再将文件字节编码为普通 base64 字符串传入 `image_base64`。不含 data-URL 前缀，不接受 URL、服务器文件路径或聊天附件 ID；服务不会自动读取客户端的 PPT。模型不应手工拼造 base64，应由客户端的文件/代码工具传递。
2. `locate_slide` 只给定位候选，包括同页不同批注状态、回看时的独立单元。可用匹配结果里的 `timestamp` 调用 `get_video_units`，并用 `unit_id` 选择相应单元。`match` 里的 inliers、inlier_ratio、feature_fraction、coverage 是几何诊断值，不是匹配正确的概率。
3. 单元是现有 section：一张画面的一次连续出现（`S012.1`），无画面讲解段为 `N001`。它不是新生成的“完整主题”；同一主题可能跨多个单元。按 `[start,end)` 查显示/语音区间；交界时语音归属可能重叠，因此返回所有相符单元，空档不猜最近的单元。
4. 简介来自既有 `spoken_summary`，不会触发读图或生成摘要。`summary_status` 为 available / partial / missing；available 仅指存储摘要未被标记截断，不保证语义完整。简介最长 600 字符；没有简介仍可读取正文。`get_video_context.timeline` 保留原 id/start/end/page/speech_first/speech_last，并增加相同的简介字段。
5. `read_video_unit` 依据准确的 `t_ids` 成员集合读取，而不是假定编号连续；成员缺失时报错。正文超过 24,000 字符时分块，直到 next_text_offset=null 才算读取全部。该接口只读转写，图片另用 view_slide 的 image_id 获取。旧 `read_evidence(S012.1)` 仍只读摘要，避免破坏原客户端。

匹配使用已依赖的 OpenCV SIFT + ratio test + RANSAC，无 embedding、模型下载或独立向量库。全图特征覆盖检查用于降低只匹配到页眉/Logo 的风险，不能保证所有课件不误匹配。低特征图片会返回空结果；缺失/损坏的库内图片通过 skipped_images 披露。客户端应保留所有候选，关键匹配用原图确认，不把第一名当作自动确认。

当前仅查询指定视频，扫描全部已登记的画面与出现截图。256 项进程内 LRU 缓存按图片路径、修改时间、大小和算法参数失效；不写磁盘索引或课件页码映射表，重启后首次查询重新提取特征。图片最长边缩到 960，最多 1200 个 SIFT 特征。首次调用延迟取决于视频截图数量，支持 next_offset 继续返回候选（不是扫描续跑）。

stdio 在启动时加载原生图像依赖，避免 Windows 上在 MCP stdin 阻塞读取期间从工作线程首次初始化 NumPy 时卡住。无需启动网页，正常请求工作仍在线程中完成。

输出边界由 `config.McpParams` 管理：目录每页 50 项；最多 8 条、每条 1000 字符的搜索词；证据读取每次最多 50 个 ID/区间、每块 24000 字符；整课转写每块 160000 字符；时间取图每次最多一张；HTTP 请求最大 4 MiB + 64 KiB，以容纳 base64 图片。`SlideMatchParams` 将查询图片限制为 3 MiB / 1200 万像素，匹配阈值集中管理。两组参数均不进入课程 pipeline 参数，不会让课程重跑。不要将文本分块误当成全文已读；客户端可能另有工具结果或上下文上限。

T 为 ASR 转录（可能识别错误）；页 S 的文字和出现 S/N 的摘要是模型生成内容；公式/图示用图片核对。对外引用需要保留 video_id、version、证据 ID 和时间。外部 AI 的最终回答不经过网页的引用验证器；本版不承诺点击时间戳就能在外部客户端直接播放。

## 已验证范围

标准 Python SDK 客户端：stdio 和 Streamable HTTP 均完成握手、发现十个工具、整课原文、时间取图、图片定位、单元简介、完整单元转写与 JPEG 图片读取。额外使用原始 JSON-RPC 测试 2024-11-05、2025-03-26、2025-06-18 协议初始化；SDK 默认协商 2025-11-25。不是“所有 AI 产品都已实测”的声明。

基础协议见 [0027 验收](proposals/0027-experiments/README.md)，图片定位和单元读取见 [0028 验收](proposals/0028-experiments/README.md)，整课转写和时间取图见 [0029 验收](proposals/0029-experiments/README.md)。未开放 PDF 工具、笔记编辑、新视频导入、付费处理或远程多用户访问。已经运行的 MCP 进程需重新加载后才能发现新增工具。
