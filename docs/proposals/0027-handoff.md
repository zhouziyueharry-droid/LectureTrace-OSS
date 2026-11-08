# MCP 实现交接

- `qa/evidence_reader.py`：提取现有检索、读取、图片逻辑，无工作区与笔记副作用；`qa/workspace.py` 复用并保留日志。
- `mcp_server.py`：标准工具、只读数据库查询、输入边界、分页、结果转换及 stdio 入口。
- `main.py`：可选 SDK、HTTP 挂载与 lifespan；`config.py` 只追加 MCP 参数及 token 配置。
- `pyproject.toml` / `uv.lock`：可选 mcp extra；CI 安装 extra 运行协议测试。
- `tests/test_mcp.py`：合成数据、HTTP/stdio 标准客户端和安全边界验收；相关已有测试回归。
- `docs/mcp.md`：通用接入及 Codex 示例、数据边界、已验证兼容性；ARCHITECTURE/README/AGENTS 同步入口。
- 原课程数据、provider key、模型环境保留；只安装 MCP 所需小型依赖，不执行全量环境同步。

验收：五种工具在两种 transport 下可发现、可调用；文本客户端可读取结果，支持图片的客户端收到标准 ImageContent；数据库/产物无写入；网页相关回归通过。不能据协议测试宣称所有 AI 客户端均已实测。
