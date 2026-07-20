# Wiki Schema

## Frontmatter（每个 wiki 页面必须有）

每个 `.md` 文件的头部用 YAML frontmatter 标注来源和状态：

```yaml
---
title: 页面标题
source: 编译来源（raw 文件路径、URL、或 "session" 表示来自对话）
source_hash: a1b2c3d4e5f67890
compiled_at: 2026-04-07T12:00:00+00:00
compiled_from: [src_a1b2c3d4e5, src_f6g7h8i9j0]
created: 创建日期 (YYYY-MM-DD)
updated: 最后更新日期 (YYYY-MM-DD)
tags: [标签1, 标签2]
status: current / draft / stale
---
```

### 必填字段
- `title` — 页面标题
- `source` — 信息来源。让每个事实可追溯。
- `created` — 创建日期

### 可选字段
- `updated` — 最后更新日期（不填则等于 created）
- `tags` — 分类标签，Obsidian 可直接用
- `status` — `current`（默认）/ `draft`（未确认）/ `stale`（可能过期）
- `source_hash` — 编译时源文件的 SHA-256 前 16 位。文件来源页面要填；`source: session` 的页面可以省略。`provenance_check.py` 用它检测源文件是否已变更。源文件变了 → 页面标记为 stale。
- `compiled_at` — 最近一次把 raw 编译进这页的时间戳（UTC ISO-8601）。不是装饰，是让人一眼知道这页最后一次吃料是什么时候。
- `compiled_from` — 可选多源列表，写成 `[src_a, src_b]`。`source` 仍然是主来源，`compiled_from` 用来记录这页还吃了哪些 source。

### 为什么这样设计
- AI 读一个页面就知道信息从哪来，不用额外查 manifest
- Obsidian 原生支持 frontmatter 属性面板
- 比在正文里写"来源：xxx"更结构化，脚本可以校验
- 零额外 token 开销（frontmatter 是页面自身的一部分）

## 页面

- `index.md`（免 frontmatter，纯索引）
- `log.md`（免 frontmatter，纯日志）
- `project-overview.md`
- `current-status.md`
- `sources-and-data.md`
- `github-and-raw-strategy.md`

## 规则

1. 新 raw 进来，先登记 manifest
2. 新结论，必须回写 wiki（带 frontmatter）
3. 新规则，必须同时补测试
4. 没证据，不要写成结论
5. memory repo 只放编译结果，不放 raw 本体
