# Core 当前架构

> 核对日期：2026-09-24；源码基线 `428f4fe`。运行参数以调用方环境和任务保存的输入为准。

## 模块与执行流程

`CLI / 调用方 → execution → 独立 Python Worker → pipeline → Pi RPC → HTML → Chromium PNG`

| 模块 | 当前职责 |
|---|---|
| [cli.py](../src/video_report_agent/cli.py)、[execution.py](../src/video_report_agent/execution.py) | CLI 参数、Worker 进程组、期限与取消监督 |
| [pipeline.py](../src/video_report_agent/pipeline.py) | 创建输入和状态，顺序组织下载、转写、生成、截图与错误落盘 |
| [ingest.py](../src/video_report_agent/ingest.py)、[audio.py](../src/video_report_agent/audio.py) | Bilibili 来源验证、下载、16 kHz 单声道音频、时长与静音检测 |
| [asr.py](../src/video_report_agent/asr.py)、[paraformer.py](../src/video_report_agent/paraformer.py) | 本地/云端 ASR、云任务轮询、两段结果与时间戳合并 |
| [transcript.py](../src/video_report_agent/transcript.py)、[transcript_foundation.py](../src/video_report_agent/transcript_foundation.py) | 来源事件、规范单元、可选字幕/OCR 融合 |
| [pi.py](../src/video_report_agent/pi.py)、[产品 Skill](../src/video_report_agent/skills/video-report/SKILL.md) | 选择模式资产、Pi RPC 和完成判断；Skill 规定内容与表达 |
| [report_content.py](../src/video_report_agent/report_content.py)、[report_image.py](../src/video_report_agent/report_image.py) | 原始简介处理、HTML 后处理与长图导出 |
| [reuse.py](../src/video_report_agent/reuse.py)、[retention.py](../src/video_report_agent/retention.py) | 兼容输入复用与媒体清理 |
| [trace.py](../src/video_report_agent/trace.py)、[usage.py](../src/video_report_agent/usage.py) | 阶段事件、调用用量和费用估算缓存 |
| [inspect_report.py](../src/video_report_agent/inspect_report.py)、[evaluate_report.py](../src/video_report_agent/evaluate_report.py) | 程序检查与受控比较；不提供自动语义验收 |

生成先尝试完整转写复用，否则检查媒体与 ASR 复用；随后构建完整转写供 Agent 阅读。单任务主链路顺序执行，长音频 ASR 的两段并发不改变单 Agent 架构。没有向量检索或多 Agent 调度。

## 任务数据与接口

`create_run` 保存 `input.json`（来源、ASR 配置、报告模式、模型选择）与初始 `status.json`；`generate(run)` 执行管线，外层 execution 监督 Worker。

每个 `runs/<run-id>/` 保存媒体、`asr.json`、`source-text-events.jsonl`、`canonical-transcript.jsonl`、`transcript-manifest.json`、`transcript.md`、所选 Skill 资产、Pi 会话与最终报告。条件路径和失败任务不会拥有所有产物。

规范单元具有 ID、时间范围与来源关系，`transcript.md` 为模型可读视图；报告的 `data-source-units` 用来定位来源，不能单凭绑定合法判断内容忠实。

`run.trace.jsonl` 记录下载、FFmpeg、ASR、转写、Agent、截图阶段；`pi.events.jsonl` 记录模型与工具事件。两层保留各自职责。

Agent 日志默认保留每轮结束消息与 usage、工具参数/结果、生命周期和错误，丢弃流式碎片、重复结束快照与原始推理正文。`request_trace.ts` 使用 Pi 的 `before_provider_request` 与非空 `thinking_delta` / `text_delta` / `toolcall_delta` 记录请求起点、首个有效响应和单调时钟延迟；经 RPC notification 传回 Python，不记录请求凭证或完整 payload。该值包含客户端、网络与供应商等待，不代表供应商纯推理时间；未提供这些钩子的历史日志保持未知。

`PI_TRACE_FULL=1` 额外写入 `pi.raw.events.jsonl` 供诊断；终态任务的原始流在清理时按 `PI_TRACE_FULL_MAX_AGE_DAYS`（默认 7，0 禁用）过期。精简日志与历史 `pi.events.jsonl` 不自动删除，Pi 自身会话保留策略不变。

## 完成与检查

Pi consumer 等待 `agent_settled`，并要求最终 assistant 的 `stopReason == "stop"`；`agent_end` 可能出现在重试或压缩之前。之后还检查实际 HTML 交付条件。截图失败记录 `image_error`，HTML 仍可进入 `RENDERED`。

`REPORT_REVIEW` 默认关闭。开启后向同一 Agent 提供检查工具；工具返回 `semantic_review: not_performed`、`visual_quality: not_scored`，不保证模型调用、修复或质量提升。

## 配置与费用

CLI 从当前运行目录加载 `.env`，进程环境优先；调用方可传模型选择，或通过 `PiRunner(skill_dir=...)` / `VIDEO_REPORT_SKILL_DIR` 指定完整 Skill。Pi 状态存于运行目录的 `config/pi/`（可由环境指定），初始化默认模型配置不覆盖已有设置。安装与依赖细节沿用 [README](../README.md)。

`usage.json` 缓存 LLM 聚合，按日志文件元信息及语义版本失效。ASR backend/时长元数据有进程内有界缓存，费用每次按当前传入费率计算。部分 QwenAI 模型采用代码中标注 2026-09-21 的人民币价目估算，其他可用 Pi 美元估算单列；不是实时查询供应商价格。缺失价格保持未知。
