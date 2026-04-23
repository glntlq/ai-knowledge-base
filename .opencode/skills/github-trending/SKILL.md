---
name: github-trending
description: 当需要采集 GitHub 热门开源项目时使用此技能
allowed-tools:
  - Read
  - Grep
  - Glob
  - WebFetch
---

## 使用场景

- 每日定时采集 GitHub Trending 上的热门开源项目
- 筛选 AI/LLM/Agent 领域的技术动态
- 为知识库提供原始数据输入

## 执行步骤

1. **搜索热门仓库** — 通过 WebFetch 调用 GitHub Trending API (`https://github.com/trending?since=daily`)，获取当日所有热门仓库列表

2. **提取信息** — 从每个仓库条目中提取：名称、URL、描述、Star 数、编程语言、标签（topics）

3. **过滤** — 仅保留与 AI/LLM/Agent 相关的项目；排除 Awesome 列表类项目（标题含 "awesome" 不区分大小写）

4. **去重** — 对比已有 `knowledge/raw/` 下的历史数据，跳过已采集过的项目（按仓库全名 `owner/name` 去重）

5. **撰写中文摘要** — 为每个项目撰写中文摘要，公式：**项目名 + 做什么 + 为什么值得关注**（每条 30-60 字）

6. **排序取 Top 15** — 按 Star 数降序排列，取前 15 个

7. **输出 JSON** — 保存到 `knowledge/raw/github-trending-YYYY-MM-DD.json`

## 注意事项

- 严格遵守 robots.txt，请求间隔 ≥ 5 秒
- 禁用裸 `print()`，全部使用 `logging` 模块
- `knowledge/raw/` 下的文件一经保存即为只读，不得修改
- 所有数据必须基于真实存在的开源项目，严禁编造

## 输出格式

```json
{
  "source": "github_trending",
  "skill": "github-trending",
  "collected_at": "2026-04-23T12:00:00Z",
  "items": [
    {
      "name": "owner/repo",
      "url": "https://github.com/owner/repo",
      "summary": "OpenCode：开源 AI 编程代理，支持多种 LLM 提供商，已获 140K+ Star，是当前最活跃的 AI 编码工具之一",
      "stars": 140000,
      "language": "TypeScript",
      "topics": ["ai", "coding-agent", "llm"]
    }
  ]
}
```
