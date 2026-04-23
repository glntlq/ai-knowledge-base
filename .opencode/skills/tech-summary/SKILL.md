---
name: tech-summary
description: 当需要对采集的技术内容进行深度分析总结时使用此技能
allowed-tools:
  - Read
  - Grep
  - Glob
  - WebFetch
---

## 使用场景

- 对 GitHub Trending / Hacker News 采集的原始数据进行深度分析
- 为每个技术项目生成结构化摘要、评分与标签
- 发现跨项目的技术趋势与新兴概念

## 执行步骤

1. **读取最新采集文件** — 读取 `knowledge/raw/` 目录下最新的采集 JSON 文件（按文件名时间排序取最新）

2. **逐条深度分析** — 对每个项目执行三项分析：
   - **摘要**（≤ 50 字）：一句话概括项目核心价值
   - **技术亮点**（2-3 个）：用事实和数据说话，如 Star 增速、独特架构、关键特性
   - **评分**（1-10）：按评分标准给出整数分并附简要理由
   - **标签建议**：推荐 2-4 个技术标签

3. **趋势发现** — 跨项目识别：
   - 共同主题（如 Agent 框架、MCP 协议、多模态）
   - 新兴概念（近期首次出现的技术方向）

4. **输出分析结果 JSON** — 保存到 `knowledge/raw/analysis-YYYY-MM-DD.json`

## 评分标准

| 分数 | 含义 | 说明 |
|------|------|------|
| 9-10 | 改变格局 | 可能重塑行业方向的技术突破 |
| 7-8 | 直接有帮助 | 解决实际问题，值得立即关注 |
| 5-6 | 值得了解 | 有参考价值但非必需 |
| 1-4 | 可略过 | 关注度低、创新不足或重复轮子 |

## 约束

- 每批 15 个项目中，评分 9-10 的项目不超过 **2 个**
- 禁用裸 `print()`，全部使用 `logging` 模块
- 所有分析必须基于真实数据，严禁编造

## 输出格式

```json
{
  "source": "tech_summary",
  "skill": "tech-summary",
  "input_file": "github-trending-2026-04-23.json",
  "analyzed_at": "2026-04-23T12:30:00Z",
  "items": [
    {
      "name": "owner/repo",
      "url": "https://github.com/owner/repo",
      "summary": "开源 AI 编码代理，支持多 LLM 提供商",
      "highlights": [
        "140K+ Star，月活 650 万开发者",
        "支持 LSP、MCP、自定义工具等多种协议"
      ],
      "score": 9,
      "score_reason": "重新定义了 AI 编码助手的开源标准，社区生态活跃，技术架构先进",
      "tags": ["ai", "coding-agent", "llm", "open-source"]
    }
  ],
  "trends": {
    "common_themes": ["Agent 框架趋同于 MCP 协议", "多模态支持成为标配"],
    "emerging_concepts": ["Agent 自进化引擎", "语音合成与交互"]
  }
}
```
