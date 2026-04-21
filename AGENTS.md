# AI 知识库助手项目 - Agent 架构文档

## 1. 项目概述

本项目是一个 AI 知识库助手，自动从 GitHub Trending 和 Hacker News 采集 AI/LLM/Agent 领域的技术动态，经过 AI 分析后结构化存储为 JSON，并支持通过 Telegram/飞书等多渠道分发。

## 2. 技术栈

- **Python 3.12**：主要开发语言
- **OpenCode + 国产大模型**：AI 代码生成与智能分析
- **LangGraph**：Agent 工作流编排
- **OpenClaw**：多源数据采集框架

## 3. 编码规范

- **代码风格**：严格遵守 PEP 8，使用 `snake_case` 命名变量、函数、方法及模块名
- **文档字符串**：使用 Google 风格 docstring，为所有模块、类、函数、方法提供清晰的说明
- **日志与输出**：禁止使用裸 `print()`，统一使用 `logging` 模块进行分级日志记录
- **类型提示**：尽可能使用类型注解（Type Hints）提高代码可读性与可维护性
- **导入顺序**：标准库 → 第三方库 → 本地模块，每组之间空一行

## 4. 项目结构

```
.ai-knowledge-base/
├── .opencode/
│   ├── agents/          # OpenCode Agent 定义文件
│   └── skills/          # 可复用的技能模块
├── knowledge/
│   ├── raw/            # 原始采集数据（HTML/JSON）
│   ├── processed/      # 清洗后的中间数据
│   └── articles/       # 最终结构化知识条目
├── src/
│   ├── crawlers/       # 爬虫模块（GitHub Trending, Hacker News）
│   ├── analyzers/      # AI 分析模块
│   ├── storages/       # 存储模块（JSON 文件/数据库）
│   └── notifiers/      # 通知分发模块（Telegram/飞书）
├── tests/              # 单元测试与集成测试
├── requirements.txt    # Python 依赖
├── pyproject.toml     # 项目配置
└── AGENTS.md          # 本文档
```

## 5. 知识条目 JSON 格式

每个知识条目存储为独立的 `.json` 文件，位于 `knowledge/articles/` 目录下。

```json
{
  "id": "uuid_v4_or_sha256",
  "title": "文章或项目的标题",
  "source_url": "https://github.com/trending/python",
  "source_type": "github_trending|hacker_news",
  "summary": "AI 生成的摘要，约 200-300 字，涵盖核心内容与技术要点",
  "content_markdown": "可选，完整的 Markdown 格式内容",
  "tags": ["llm", "agent", "python", "framework"],
  "language": "zh|en",
  "author": "原作者或组织",
  "published_at": "2025-04-21T10:30:00Z",
  "collected_at": "2025-04-21T12:00:00Z",
  "analyzed_at": "2025-04-21T12:30:00Z",
  "status": "pending|analyzed|distributed|archived",
  "metadata": {
    "github_stars": 1500,
    "hn_points": 120,
    "hn_comments": 45,
    "difficulty": "beginner|intermediate|advanced"
  }
}
```

## 6. Agent 角色概览

| 角色 | 职责 | 工具/技能 | 输出 |
|------|------|-----------|------|
| **采集 Agent** | 定时从 GitHub Trending 和 Hacker News 抓取数据 | `fetch_github_trending`, `fetch_hacker_news`, `html2markdown` | 原始 HTML/JSON 保存至 `knowledge/raw/` |
| **分析 Agent** | 对原始内容进行 AI 分析，提取摘要、打标签、结构化 | `summarize_with_llm`, `extract_tags`, `detect_language` | 结构化 JSON 保存至 `knowledge/articles/` |
| **整理 Agent** | 知识库维护：去重、分类、归档、质量检查 | `deduplicate_by_hash`, `categorize_by_topic`, `validate_schema` | 更新索引、清理过期数据 |
| **分发 Agent** | 将新知识推送到配置的渠道（Telegram/飞书） | `send_telegram_message`, `send_feishu_webhook` | 发送成功/失败日志 |

## 7. 红线（绝对禁止的操作）

1. **禁止硬编码密钥**：所有 API Key、Token、Webhook URL 必须通过环境变量或配置文件读取，严禁提交到版本库。
2. **禁止直接删除数据**：所有删除操作必须先移动到 `knowledge/archived/` 目录，保留至少 30 天。
3. **禁止绕过日志**：所有关键步骤（采集、分析、分发）必须记录日志，且日志级别不低于 INFO。
4. **禁止无限循环请求**：爬虫必须设置合理的请求间隔（≥ 5 秒），并遵守目标网站的 robots.txt。
5. **禁止裸 `print()`**：所有输出必须通过 `logging` 模块，并区分 DEBUG、INFO、WARNING、ERROR 级别。
6. **禁止非幂等操作**：Agent 的重试机制必须保证幂等性，避免重复分析或重复分发。
7. **禁止直接修改原始数据**：`knowledge/raw/` 下的文件一经保存即为只读，所有清洗、分析操作均在副本上进行。
8. **禁止编造数据**：所有采集和分析必须基于真实存在的项目或内容，严禁编造不存在的项目或数据。
9. **禁止日志输出敏感信息**：日志中不得包含 API Key、Token、密码等敏感信息，必须使用占位符或脱敏处理。
