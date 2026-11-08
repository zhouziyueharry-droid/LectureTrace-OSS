# LectureTrace — contributor navigation

当前：MCP 分支增加标准只读视频工具；实现和验收见 [MCP](docs/mcp.md)、[0027](docs/proposals/0027-mcp.md)、[交接](docs/proposals/0027-handoff.md)、[实验](docs/proposals/0027-experiments/README.md)。

2026-11-08：新增图片定位时间 → 单元简介 → 按需转写/截图的八工具流程；[0028 提案](docs/proposals/0028-slide-location.md)、[交接](docs/proposals/0028-handoff.md)、[验收](docs/proposals/0028-experiments/README.md)。SIFT/RANSAC 复用 OpenCV，无 embedding；单元沿用 section，一次出现与整页的所有出现不能混同。

- 产品与启动：[README](README.md)、[中文说明](docs/README.zh-CN.md)、[Setup](docs/setup.md)。
- 当前架构：[ARCHITECTURE](ARCHITECTURE.md)。MCP 工具契约在 [docs/mcp.md](docs/mcp.md)。
- `backend/app/mcp_server.py` 提供通用 MCP；`qa/evidence_reader.py` 与网页共享读取逻辑。不要从 stdio 入口导入 `app.main` 或启动 runner。
- 默认用中文与本项目维护者沟通，关键术语保留英文。用户要求只讨论时，不改代码和文档。
- 保留既有未提交工作。不要提交 data、课程媒体、.env、凭据、模型权重或环境。
- 付费模型调用先说明目的、模型、样本与预估费用，由用户决定；普通测试使用 mock。
- 复用已有环境。运行使用 `uv run --directory backend --no-sync ...`；同步时保留原 extras，避免卸载 ASR/CUDA/OCR。
- 后端验证：`uv run --directory backend --no-sync pytest -q`。MCP 测试需安装 mcp extra；stdio 测试只读合成数据库。
- Git 的 main 保留稳定版本，当前工作在 MCP 分支；不要将 private 仓库历史合入 public。
- 较大改动先讨论方案并记录提案/交接，实现后更新架构、契约与验收。已授权实施的方案继续执行，无需重复确认。
