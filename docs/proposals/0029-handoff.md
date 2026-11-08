# 0029 实现交接

- `mcp_server.py`：新增两个只读工具及共同源产物边界检查。复用已有 fingerprint、图像编码、错误封装与 MCP 内容格式。无 app.main/runner 引入、无模型调用。
- `config.McpParams.transcript_chunk_chars=160000`：只控制 MCP 正文分页，不进入处理参数，不使课程产物失效。
- `tests/test_mcp_sources.py`：只有原始转写/关键帧的课程夹具；分页拼齐、版本、空转写、长正文、时间边界、不同出现图片、重叠、无匹配、损坏源与路径逃逸。
- `tests/test_mcp.py`：新旧十工具的标准 stdio/HTTP 客户端、只读文件快照与三个旧协议版本。
- 契约见 [MCP](../mcp.md)，实测见 [验收](0029-experiments/README.md)。源语言原样返回，不保证录音识别完整；关键帧为已有代表图，不能称为精确时刻抽帧。
- 运行中的 stdio MCP 连接需重新加载工具；HTTP 后端需在任务空闲时重启。未修改客户端配置、网页问答与资源池调度。
