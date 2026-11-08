# 0028 实现交接

- `qa/evidence_reader.py`：新增 section 简介、按时间定位、完整 section 转写；保持旧读取语义。
- `qa/slide_locator.py`：输入图片解码、SIFT/RANSAC 匹配、受限图像缓存，独立于语言模型和 MCP。
- `mcp_server.py`：新增三个只读工具，复用现有错误封装、版本校验、分页和图片读取。
- `config.py`：匹配与图片输入参数集中到独立 Params，不改变 pipeline 参数或已有产物。
- `tests/test_mcp.py`、`tests/test_video_units.py`、`tests/test_slide_locator.py`：单元完整性、渐进披露、匹配行为、标准协议和输入边界。
- `docs/mcp.md`、`ARCHITECTURE.md`、`AGENTS.md`：同步已实现契约及边界。

分批：先做单元读取，再接图片定位，最后做标准客户端及真实课程只读验证。保持 MCP 分支，不修改客户端配置、不推送远程。
