# LectureTrace

把本机课程录像变成可以回到原始片段核对的学习工作区。

**项目发起人和主要维护者：** [Harry（@zhouziyueharry-droid）](https://github.com/zhouziyueharry-droid)。

[English](../README.md) · [安装与配置](setup.md) · [贡献指南](../CONTRIBUTING.md)

## 功能

- 管理本机 MP4 与课程分组，边看视频边阅读字幕、幻灯片和笔记。
- 可选本机 Whisper 转录；使用 DeepL、Google、API 模型或 Ollama 翻译字幕。
- 确认幻灯片区域后抽帧，生成带时间戳的大纲。
- 通过 API 模型或可选 Codex runtime 问答，沿时间戳和 PDF 引用核对来源。
- Markdown 笔记、PDF 阅读与批注、可调整分栏的阅读工作区。
- 导出单讲或整门课的 `.ltcourse` 包，在另一台设备恢复已处理资料。

前端 React + TypeScript，后端 FastAPI + Python，课程保存在本机 SQLite 和文件目录。界面目前以简体中文为主。

## 启动

准备 uv、Node.js 22.18+（推荐 Node 24 LTS）；视频处理还需要 FFmpeg/ffprobe。Python 使用 3.12。

```sh
git clone https://github.com/zhouziyueharry-droid/LectureTrace-OSS.git
cd LectureTrace-OSS
uv sync --directory backend --locked
npm ci --prefix frontend
npm --prefix frontend run build
uv run --directory backend --no-sync python -m app.main
```

打开 http://127.0.0.1:8000。默认轻量模式不安装 Whisper/CUDA/OCR 模型，不需要 Key 就能打开界面、管理笔记和阅读已导入的课程包。处理新录像需要按 [配置说明](setup.md) 启用相应能力。

**本机存储不等于所有功能离线。** API 翻译、读图、大纲和问答会向选定服务发送内容；实际调用前核对发送范围和费用。生成内容需要回到原片段验证。应用面向单用户本机使用，不适合直接暴露到公网。

## 参与

欢迎中文或英文 Issue/PR。不需要 Collaborator 权限：Fork → 新分支 → 提交 PR。每个人使用自己的 Git 身份，保留真实贡献署名。请勿提交密钥、课件、录像或未获授权的示例资料。

**NTU AI6130（26S1，Large Language Models）小组同学请看这里：** [小组协作须知](AI6130-GROUP.md)。这里说明本次小组作业的协作方式、课程结束后的权限安排、长期参与方式和贡献署名。

公开仓库从整理后的源码快照开始，不包含私人课程及历史实验产物。后续贡献通过公开 commit、PR 和 review 记录。

采用 [MIT License](../LICENSE)，允许商业使用；第三方依赖及模型权重遵守各自许可。软件许可不覆盖用户导入的课程内容。
