# MCP 只读接入验收

2026-11-08，Windows / Python 3.12 / 官方 MCP SDK 1.30.0，复用已有后端环境；只增量安装 SDK 依赖，未同步或移除 GPU/OCR 环境。

- 后端全套：564 passed，1 个既有重复 ZIP 条目测试警告。
- 新增 8 项 MCP 测试：标准客户端的 HTTP/stdio 全链路、只读快照、错误参数、未知工具、版本过期、图片路径越界、缺失数据库不创建、token/Origin/Host 检查、分页与文本分块；三个原始 JSON-RPC 旧版本初始化。
- 协议协商：SDK 客户端 2025-11-25；原始客户端 2024-11-05、2025-03-26、2025-06-18。
- `verify_local.py`：使用现有课程，通过 stdio 和真实 localhost HTTP socket 各读取一次目录、正文、搜索和图片；两种 transport 得到同样的成功结果。
- 真实数据验证前后：数据库和 .env 哈希一致；2395 个可访问数据文件大小与修改时间一致。一个既有旧测试目录无读取权限，未纳入扫描；未对其作任何操作。
- 本机 Codex 已登记 stdio 入口。当前聊天是否已动态加载工具，需要客户端重载后核对；未运行付费模型。

修复验证中发现的集成问题：SDK HTTP session manager 必须按每个应用 lifespan 新建；PDF 搜索保留原 `_terms` 导出以兼容旧调用方。

## 复现

```sh
uv run --directory backend --no-sync pytest -q
backend/.venv/Scripts/python.exe docs/proposals/0027-experiments/verify_local.py
```

第二条只在拥有本机课程数据且允许本机子进程/端口的环境运行。不输出字幕正文、图片数据、API Key 或访问令牌。临时 HTTP 服务不挂载网页业务，不启动处理队列，检查完成即退出。

## 边界

未做其他商业 AI 客户端的真实会话、视觉理解质量、macOS 实机或公网部署验收。兼容性结论针对标准协议与结果格式。外部 AI 最终回答不经过网页引用校验；其对课程材料的使用遵循客户端/模型的数据政策和费用。
