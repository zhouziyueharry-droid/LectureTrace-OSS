# MCP 接入与工具契约

LectureTrace 的 MCP 是通用协议服务，不依赖 Codex 登录、app-server 或专有工具格式。支持 MCP 的客户端可选择 stdio 或 Streamable HTTP；客户端需要支持图片工具结果且所选模型具备视觉能力，才能利用幻灯片图片。

首版读取已经处理的课程。五个工具不上传、删除、重跑视频，不写笔记、不调用模型。MCP 本身不产生模型调用费用，但客户端使用返回的课程材料时仍遵循该客户端的数据处理和计费方式。

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

## 五个工具

| 工具 | 参数 | 结果与下一步 |
| --- | --- | --- |
| `list_videos` | `offset=0` | 视频 ID、课程名、时长、状态；`next_offset` 分页 |
| `get_video_context` | `video_id, offset=0` | 章节标题、时间目录、`version`；`next_offset` 分页。目录不证明读过原文 |
| `search_video` | `video_id, version, queries, offset=0` | 摘录、ID、起始秒数；`next_offset` 分页，需再读取正文 |
| `read_evidence` | `video_id, version, ids, text_offset=0` | 带 ID、时间、来源标识的文字；支持 `T0001-T0005`；`next_text_offset` 继续相同 ids 的文本 |
| `view_slide` | `video_id, version, id` | 标准 JPEG `ImageContent`，另附视频/证据 ID、时间与版本 |

所有正常结果都有 `content` 中的 JSON 文本和等价 `structuredContent`，旧客户端可只使用文本。图片作为独立标准内容块返回，不要求客户端访问服务器磁盘路径。业务失败使用 `isError=true`，不把内部磁盘路径/数据库异常返回给客户端。

从 context 获得 version 后传给其他证据工具；证据变化会拒绝过期请求，重新获取 context 再继续。请求没有全局“当前视频”，每次显式指定 ID，避免不同对话串课。MCP 不保存聊天记录和“已读”日志。

输出边界由 `config.McpParams` 管理：目录每页 50 项；最多 8 条、每条 1000 字符的搜索词；每次读取最多 50 个 ID/区间、每块 24000 字符；HTTP 请求最大 65536 字节。这些是传输边界，不是处理算法阈值，不会让课程重跑。不要将文本分块误当成全文已读。

T 为 ASR 转录（可能识别错误）；页 S 的文字和出现 S/N 的摘要是模型生成内容；公式/图示用图片核对。对外引用需要保留 video_id、version、证据 ID 和时间。外部 AI 的最终回答不经过网页的引用验证器；本版不承诺点击时间戳就能在外部客户端直接播放。

## 已验证范围

标准 Python SDK 客户端：stdio 和真实 localhost Streamable HTTP 均完成握手、发现五个工具、搜索、正文与 JPEG 图片读取。额外使用原始 JSON-RPC 测试 2024-11-05、2025-03-26、2025-06-18 协议初始化；SDK 默认协商 2025-11-25。不是“所有 AI 产品都已实测”的声明。

具体结果见 [验收记录](proposals/0027-experiments/README.md)。未开放 PDF 工具、笔记编辑、新视频导入、付费处理或远程多用户访问。
