# MCTTK 代码审查记录

**日期**: 2026-04-09  
**分支**: ruffcheck  
**仓库**: https://github.com/jiubook/MCTTK

## 项目简介

MCTTK — Minecraft 新闻自动爬取 + 翻译 + 转换 + 发布工具。从 Minecraft 官方 API 和 Feedback 网站抓取新闻，调用 AI 翻译为中文，转换为 BBCode/Markdown，发布到 MCBBS。

**主要文件**:
- `main.py` — 编排器（爬取→过滤→翻译→转换→发布）
- `scraper.py` — 爬取与翻译模块（Minecraft API + Feedback 网站）
- `converter.py` — JSON→BBCode/Markdown 转换器
- `poster.py` — MCBBS 发帖模块
- `scheduler.py` — Docker 定时调度器（每10分钟）
- `init_state.py` — 测试辅助，初始化状态文件
- `config.json` — 统一配置
- `glossary.json` — 专业术语词汇表
- `pyproject.toml` — Ruff 配置（本次新增）

## 最近提交

| 提交 | 时间 | 内容 |
|---|---|---|
| `bc02452` | 04-09 14:51 | 修正 noqa S110：`pass` → `logging.debug/warning`，target-version 升至 py313 |
| `5e202e8` | 04-09 13:01 | 大规模 Ruff 修复 + 新增 pyproject.toml |
| `f3a4371` | 04-08 19:37 | glossary.json 更新 |

## 审查结论

### 做得好的
- `pass` → `logging`，异常可追踪
- `List[Dict]` → `list[dict]`，类型注解现代化
- import 排序统一、去冗余参数、清理未使用变量
- pyproject.toml 选了合理的 lint 规则

### 发现的问题

1. **`scraper.py` 模块级全局状态** — `CFG = load_config()` 在 import 时执行，配置无法动态更新，测试难 mock。建议改为注入式或类封装。
2. **`classify_news_type` 重复定义** — scraper.py、main.py、converter.py (`_detect_article_type`) 各有一份，逻辑略有差异（返回 `"other"` vs `"normal"` vs `"commentary"`）。应统一。
3. **`.env` 加载逻辑重复** — main.py 和 init_state.py 有完全相同的 8 行代码，应提取公共函数。
4. **`traceback` 延迟 import** — main.py 在异常处理循环内 import traceback，应放到顶部。
5. **`scheduler.py` 无超时控制** — `subprocess.run` 没设 timeout，main.py 卡死则调度器永久阻塞。
6. **`pyproject.toml` 缺 `requires-python`** — target-version 只影响 lint，不阻止低版本运行。应加 `requires-python = ">=3.13"`。
7. **过于宽泛的 `except Exception`** — 部分地方仍用裸 `except Exception`，应尽量精确。

### 关于 `find_duplicate_sequences` 的讨论

**结论：三层去重设计合理，不需要重构。**

三层去重（连续去重→序列去重→长文本去重）是针对真实 HTML 结构的工程决策：
- Minecraft 官网在 sidebar、archive 模块、related articles 中重复展示文章内容
- `extract_blocks_in_order` 遍历 DOM 时不同容器各自 walk，同段文字被多次收录
- 容器签名（`seen_containers`）无法处理部分重叠的情况（archive 只取标题+第一段，不是整个容器复制）
- 实际文章 block 数量通常 < 200，O(n³) 完全可接受

如果要优化，可做微调：找到第一个匹配后 `break`，预计算 block key 减少重复序列化。但不改变架构。

### 关于 pytest 的建议

**结论：强烈建议添加。**

理由：
- 核心逻辑是纯函数，最容易测（`_parse_date`、`_bbcode_to_markdown`、`find_relevant_terms`、`classify_news_type`、`_deep_merge`）
- 当前零测试覆盖
- Ruff 只管 lint，不管逻辑正确性

建议在 pyproject.toml 添加：
```toml
[dependency-groups]
dev = [
    "ruff>=0.15.9",
    "pytest>=8.0",
    "pytest-cov>=5.0",
]
```

建议的测试文件：
```
tests/
├── test_converter.py    # _parse_date, _bbcode_to_markdown, _detect_article_type
├── test_scraper.py      # find_relevant_terms, _deep_merge, classify_news_type
└── test_config.py       # load_config 合并逻辑
```

## 待办

- [ ] 添加 pytest 测试骨架
- [ ] 统一 `classify_news_type`（消除重复定义）
- [ ] 提取 `.env` 加载逻辑为公共函数
- [ ] `pyproject.toml` 添加 `requires-python = ">=3.13"`
- [ ] `scheduler.py` 添加 `timeout` 参数
- [ ] 考虑 `scraper.py` 全局状态的重构（可选，非紧急）
