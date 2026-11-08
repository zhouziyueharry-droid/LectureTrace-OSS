# 0029 验收：整课转写与时间取图

2026-11-08，Windows，复用现有 Python/MCP 环境与本机课程，无依赖安装、下载或付费调用。

## 自动化

- 实现前，两个功能用例均因工具不存在而失败。
- `uv run --directory backend --no-sync pytest -q tests/test_mcp_sources.py tests/test_mcp.py`：24 passed。标准 stdio 与 Streamable HTTP 客户端发现十工具、读取整课原文、按原文秒数取得 JPEG；旧八工具和三种旧协议版本继续通过。
- `uv run --directory backend --no-sync pytest -q`：597 passed，101.52 秒；1 个既有重复 ZIP entry 警告。
- 仅含原始产物的合成课程证明不依赖 cues、segments、slides、outline 或 evidence；覆盖超限逐字符无损拼接、缺失/过期 version、长正文默认一次返回、空转写、原始语言保留、时间边界/空档/重叠、回看截图、非法参数、损坏源及路径逃逸。HTTP/stdio 测试数据目录哈希与 mtime 不变。
- 源产物符号链接逃逸采用 resolve 边界模拟，不要求 Windows 创建符号链接权限；不声称真实符号链接系统调用验收。

## 真实课程只读调用

直接调用当前服务工具实现，未启动 runner；四节约三小时课程的最终 transcript/keyframes 源文件哈希和 mtime 均未变化。逐段比对格式化输出与完整原始 ASR；每课一次调用即 `complete=true`、`next_text_offset=null`。每课选一个已登记显示区间的时间，获得并解码验证 JPEG。

| 课程 | 原始段数 | 正文字符数 | 正文 tokens（o200k_base） | 时间取图 |
| --- | ---: | ---: | ---: | --- |
| week1 | 260 | 80,183 | 18,960 | 成功 |
| week2 | 312 | 93,075 | 22,211 | 成功 |
| week3 | 219 | 66,533 | 16,208 | 成功 |
| week4 | 168 | 50,203 | 12,148 | 成功 |

以上不含 JSON 包装、工具说明及客户端重复注入开销。客户端可能有自己的输出/上下文上限，不能把服务器完整返回等同于所有宿主都完整放入模型上下文。未验证问答质量、所有时间点图片正确性或 ASR 漏识别；不自动重跑课程。

## 运行状态

确认无运行中的课程任务后重启网页后端，`/api/videos` 返回 HTTP 200，代码根与 DATA_DIR 正确。当前未配置 MCP HTTP token，因此生产 HTTP MCP 保持禁用，没有修改认证配置；HTTP 新接口由合成数据标准客户端测试验证。现有 stdio 客户端需重新连接，重启网页后端不会替换它们各自的 MCP 子进程。
