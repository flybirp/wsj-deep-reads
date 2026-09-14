# wsj-deep-reads

《华尔街日报》深度分析文筛选 skill（for WorkBuddy / Claude Code 等 agent）。

与 `wsj-daily-brief` 互补：**daily 求全求快，本 skill 求深求慢**。从全站 200+ 条目里用多信号打分筛出 20 条左右的长文/特写/调查/评论，再对 Top 篇做外部搜索补全，输出真正的中文解读。

## 核心难题：正文在付费墙后

WSJ 文章页直连返回 **401**。以下路线全部实测无效，不要浪费时间：

| 路线 | 结果 |
|---|---|
| `r.jina.ai` | 连接失败 |
| `api.allorigins.win` | 522 |
| `web.archive.org` | 429 限流 |
| `archive.today` | 不通 |
| MarketWatch（同属 Dow Jones） | 401 |

**有效办法是外部搜索补全**：用英文原标题搜索，找 ① 其他媒体的二手转述 ② **报道引用的原始研究报告**（比 WSJ 二手更权威）③ 社交平台讨论。

实测成功率约 **1/3**——热点大事件和引用公开报告的能补出大量数据，调查型独家与文化评论补不到。补不到的诚实标注为「摘要级」，不编造。

## 深度打分模型

| 信号 | 权重 |
|---|---|
| 官方摘要 ≥200 / ≥120 字符 | +25 / +16 |
| 伪摘要（Bing 把标题复读一遍）、摘要 < 70 字符 | **直接出局** |
| 分析式提问开头（How/Why/What/Inside/Is...） | +18 |
| 突发行情词（dies / says / falls / rises / hits...） | −20 |
| 标题 ≥9 词 / ≤4 词 | +8 / −6 |
| 副标题结构（含 `: `） | +6 |
| 栏目权重（arts-culture / style / health 等 +12，us-news −8） | −8 ~ +12 |
| 跨天存活 ≥3 天 / 2 天 | +18 / +10 |

存活时长靠 `.history.json` 自动累积：深度文会在 RSS 里连续多天出现，快讯 24 小时就消失。（冷启动第一次没有这个信号）

## 用法

```bash
# 纯 Python 标准库，无依赖
python3 scripts/fetch_deep.py --hours 96 --min-score 50 --top 20 --out /tmp/wsj_deep.json
python3 scripts/fetch_deep.py --hours 96 --md                 # 直接看候选清单
python3 scripts/fetch_deep.py --exclude review                # 只要分析文，剔除书评影评
```

参数：`--hours`（默认 72，深度文建议 96-168）、`--min-score`（默认 45）、`--top`、`--exclude review,opinion,analysis,feature,news`、`--md`、`--no-history`。

脚本负责筛选，**深度补全与中文解读由 LLM 完成**——见 `SKILL.md`。

## 输出结构

```
## 深度解读
### 1. 中文标题 [分数 · type · 板块]
    WSJ 的核心论点 → 补全到的关键事实（表格）→ 为什么值得读 → 我的判断

## 摘要级（未获取到正文细节）
    | 分数 | 类型 | 标题 | 官方摘要 | 链接 |
```

## 安装

```bash
git clone git@github.com:flybirp/wsj-deep-reads.git ~/.workbuddy/skills/wsj-deep-reads
```

## 姐妹 skill

- [`wsj-daily-brief`](https://github.com/flybirp/wsj-daily-brief) —— 求全求快，每日全景

## License

MIT
