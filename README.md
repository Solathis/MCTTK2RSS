# MinecraftTranslationToolkit

> 矿译通 MCTTK

> Minecraft 新闻自动爬取 + 翻译 + 发布

自动从 Minecraft 官方网站及 Feedback 网站爬取最新新闻，通过 AI 翻译为中文，转换为 BBCode/Markdown 格式，并自动发布到 MCBBS 论坛。

## 功能特性

- **自动爬取**：从 Minecraft 官方 API 获取最新新闻，同时支持从 Feedback 网站爬取更新日志
- **版本清单补齐**：额外读取 Mojang Piston 版本清单，避免因 API `sortType=Recent` 排序不可靠而漏掉 Java 版本日志
- **Cloudflare 绕过**：使用 `curl_cffi` 模拟真实浏览器，绕过 Feedback 网站的 Cloudflare 防护
- **AI 翻译**：调用 OpenAI 兼容 API 翻译为简体中文，支持并发批量翻译与 JSON Schema 结构化输出
- **智能词汇表**：动态检测专业术语，只把命中项加进提示词；手写 `glossary/glossary.json` 与自动层 `glossary/glossary.auto.json` 合并，术语按"强制 / 条件"分层注入
- **三层去重**：连续去重 + 大段重复检测 + 长文本去重，避免内容重复
- **格式转换**：自动生成 BBCode（MCBBS）和 Markdown 格式
- **模块系统**：通过 `config/modules_config.json` 配置帖子头部/尾部模板，按新闻类型自动匹配
- **自动发布**：登录 MCBBS 论坛自动发帖，支持图片上传和 JSON 附件
- **类型过滤**：通过配置控制只发布指定类型的新闻
- **安全去重**：基于 URL 的状态追踪，不会重复爬取或发布
- **失败可重试**：处理失败的文章不会被标记为已处理，下次运行自动重试（超过上限才放弃）
- **首次运行保护**：首次运行时自动将所有现有新闻标记为已处理，避免刷屏

## 项目结构

```
MCTTK/
├── main.py                    # 编排器：串联爬取→翻译→转换→发布
├── scraper.py                 # 爬取与翻译模块（含 Feedback 爬虫）
├── converter.py               # JSON → BBCode/Markdown 转换器
├── poster.py                  # MCBBS 发帖模块（含验证码识别）
├── utils.py                   # 公共工具：路径常量、MODULE_TYPE_MAP、classify_article_type
├── log_setup.py               # 日志配置（按天分文件、stdout 落盘、保留天数）
├── scheduler.py               # 定时调度器（默认每小时运行一次）
├── init_state.py              # 初始化状态工具（测试用）
├── config/                    # 配置目录
│   ├── config.sample.json     # 配置模板（复制为 config/config.json 后修改）
│   ├── config.json            # 本地配置（不入库，.gitignore 已忽略）
│   └── modules_config.json    # 帖子模板模块配置
├── glossary/                  # 词表目录
│   ├── glossary.json          # 手写术语覆盖层（入库）
│   ├── glossary.auto.json     # 自动词表层（tools/sync_glossary.py 生成，不入库）
│   └── glossary.auto.index.json # 词表溯源索引（同上，仅供人工查阅）
├── tools/                     # 辅助脚本（sync_glossary.py：同步官方术语表）
├── tests/                     # 单元测试（pytest）
├── docs/                      # 文档
│   ├── DOCKER.md              # Docker 部署说明
│   └── archive/               # 历史评审与 PR 记录
├── data/                      # 本地原始数据（不入库）：latest.tsv、mcmeta 语言文件
├── archive/                   # 历史归档（不入库）：上一代项目、实验脚本、本地备份
├── output/                    # 输出目录（自动生成，不提交）
│   ├── .state.json            # 处理状态（URL 级别去重）
│   ├── .posted.json           # MCBBS 发布状态
│   ├── news_*.json            # 翻译后的文章数据
│   ├── news_*.txt             # BBCode 格式（用于 MCBBS）
│   ├── news_*.md              # Markdown 格式
│   └── news_*.jpg             # 文章头图
├── logs/                      # 运行日志（自动生成，不提交）
│   └── mcttk_YYYY-MM-DD.log   # 每天一个文件，见上文「日志」章节
├── pyproject.toml             # 项目配置（依赖声明、Ruff 规则、pytest 路径）
├── requirements.txt           # pip 依赖（Docker 构建用）
├── Dockerfile                 # 镜像构建
├── docker-compose.yml         # 容器编排（本地构建）
├── docker-compose.release.yml # 容器编排（直接拉取发布镜像）
├── .env                       # 本地环境变量（不提交）
└── README.md
```

## 快速开始

### 1. 安装依赖

推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
```

可选依赖按需安装：

```bash
uv sync --extra captcha   # ddddocr + Pillow，用于 MCBBS 验证码自动识别
uv sync --extra feedback  # curl_cffi，用于爬取 Feedback 网站
```

也可使用 pip：

```bash
pip install -r requirements.txt
```

或手动安装：

```bash
pip install requests beautifulsoup4 urllib3
# 可选：
pip install ddddocr Pillow   # 验证码识别
pip install curl_cffi        # Feedback 爬虫
```

### 2. 配置

先复制配置模板（`config/config.json` 已被 `.gitignore` 忽略，不会提交到仓库）：

```bash
cp config/config.sample.json config/config.json   # Windows PowerShell: Copy-Item config/config.sample.json config/config.json
```

然后编辑 `config/config.json`，至少配置 AI 翻译接口：

```json
{
  "openai_compat": {
    "host": "api.your-provider.com",
    "endpoint": "/v1/chat/completions",
    "api_key": "sk-xxx",
    "model": "gpt-4o",
    "max_tokens": 10000,
    "timeout": 120
  }
}
```

如果需要自动发布到 MCBBS：

```json
{
  "mcbbs": {
    "enabled": true,
    "base_url": "https://www.mcbbs.co",
    "username": "你的用户名",
    "password": "你的密码",
    "forum_fid": 45,
    "attach_json": true,
    "sortid_map": {
      "java_snapshot": 4,
      "java_prerelease": 4,
      "java_rc": 4,
      "java_release": 4,
      "bedrock_beta": 20,
      "bedrock_release": 20
    }
  }
}
```

也可以通过 `.env` 文件或环境变量传入敏感信息（优先级高于 config.json）：

```
OPENAI_API_KEY=sk-xxx
MCBBS_USERNAME=your_username
MCBBS_PASSWORD=your_password
MCBBS_CAPTCHA_ANSWER=备用验证码   # 注意：该项请勿在.env中配置，当前版本未启用
```

MCBBS_CAPTCHA_ANSWER 仅作占位，未来添加安全回答验证时使用

当前版本下配置后，会导致验证码输出错误


### 3. 运行

```bash
# 全流程：爬取 → 翻译 → 转换 → 发布
python main.py

# 仅检测新新闻（不实际处理）
python main.py --dry-run

# 只爬取翻译，不发布
python main.py --scrape-only

# 只发布 output 目录中已翻译但未发布的文件
python main.py --post-only

# 发布时跳过图片上传
python main.py --no-image

# 发布时跳过 JSON 附件
python main.py --no-json
```

## 日志

日志按天写入 `logs/mcttk_YYYY-MM-DD.log`，同时打印到控制台（容器里就是 `docker logs`）。
除 `logging` 记录的消息外，`print()` 与标准错误也会同步进同一份文件（标记为
`[STDOUT]` / `[STDERR]`），所以容器 stdout 里出现过的内容不会只留在 `docker logs` 里。

每次运行都会记录下面这些内容：

| 阶段 | 记录内容 |
|---|---|
| 运行 | 模式、输出目录、状态文件、翻译模型、启用类型、Feedback 栏目、MCBBS 开关、日志文件路径 |
| 采集 | 每个来源（官方 API / Java 版本清单 / Feedback）拿到多少条、去重与类型过滤结果、耗时 |
| 状态 | 候选 / 已处理 / 本次待处理条数，首次运行的标记情况 |
| 解析 | 文章标题、内容块数量与类型分布、发布日期、耗时 |
| 翻译 | 标题原文与译文、正文可译块数、成功/失败块数、原文字符 → 译文字符、批次数、每次 API 调用的耗时与 token 用量 |
| 保存 | JSON / 头图 / BBCode / Markdown 的完整路径与文件大小 |
| 发布 | 登录结果、每篇发帖结果、**帖子地址**、耗时；失败时附异常堆栈 |
| 汇总 | 每轮结束一行：来源条数 / 新增条数 / 翻译篇数 / 发帖成功失败 / 总用时 |

日志示例（节选）：

```text
2026-11-14 20:01:09 [INFO] ================== MCTTK 运行开始 ==================
2026-11-14 20:01:09 [INFO] [配置] 模式: full | 输出目录: output | 状态文件: output/.state.json
2026-11-14 20:01:10 [INFO] [主] 来源 minecraft_api: 新并入 3 条（原始 5 条，用时 1.2s）
2026-11-14 20:01:10 [INFO] [主] 来源 version_manifest: 新并入 1 条（原始 1 条，用时 0.6s）
2026-11-14 20:01:12 [INFO] [主] 来源 feedback: 6 条（基岩正式版更新日志 3 条，基岩测试版更新日志 3 条，用时 1.8s）
2026-11-14 20:01:12 [INFO] [主] 状态检查: 候选 10 条，已处理 9 条，本次待处理 1 条
2026-11-14 20:01:12 [INFO] ================== [1/1] Minecraft Beta & Preview - 26.70.24 ==================
2026-11-14 20:01:12 [INFO] [主] 来源: feedback | 类型: feedback | 链接: https://feedback.minecraft.net/...
2026-11-14 20:01:15 [INFO] [翻译] 标题: 原文「Minecraft Beta & Preview - 26.70.24」→ 译文「Minecraft 基岩版 测试版 - 26.70.24」
2026-11-14 20:02:31 [INFO] [翻译] 正文完成: 58/61 块成功，3 块保留原文（原文 18342 字符 → 译文 16210 字符，6 批次，用时 76.4s）
2026-11-14 20:02:31 [INFO] [主] 翻译覆盖: 58/61 块（95%），原文 18342 字符 → 译文 16210 字符
2026-11-14 20:02:31 [INFO] [保存] JSON 已写入: output/news_xxx.json（60.8 KB，61 块）
2026-11-14 20:02:33 [INFO] [主] 头图已保存: output/news_xxx.jpg（127.3 KB，用时 1.9s）
2026-11-14 20:02:33 [INFO] [主] 已生成 BBCode: output/news_xxx.txt（28.4 KB）
2026-11-14 20:02:33 [INFO] [主] 已生成 Markdown: output/news_xxx.md（26.4 KB）
2026-11-14 20:02:41 [INFO] [发布] ✓ 发帖成功: https://www.mcbbs.co/thread-123456-1-1.html（用时 7.8s）
2026-11-14 20:02:44 [INFO] [汇总] 模式=full | 来源 10 条 | 新增 1 条 | 翻译完成 1 篇 | 发帖成功 1 | 发帖失败 0 | 总用时 95.3s
```

### 调整日志行为

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MCTTK_LOG_LEVEL` | `INFO` | 日志级别；排查问题时设 `DEBUG`（多出请求头、逐条新闻、分批明细） |
| `MCTTK_LOG_DIR` | `logs` | 日志目录 |
| `MCTTK_LOG_KEEP_DAYS` | `90` | 保留天数，启动时清理超期文件；`0` 表示永久保留 |
| `MCTTK_LOG_CONSOLE` | `1` | 是否同时输出到控制台 |
| `MCTTK_LOG_CAPTURE` | `1` | 是否把 `print()` / stderr 一并写进日志文件 |

Docker 默认入口 `scheduler.py` 另有：

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `MCTTK_INTERVAL` | `3600` | 调度间隔秒数（1 小时），同时作为对齐网格（见下） |
| `MCTTK_RUN_TIMEOUT` | `3600` | 单轮 `main.py` 超时秒数（1 小时），超时会记录错误并进入下一轮 |
| `TZ` | 镜像默认 `UTC` | 容器时区，决定"整点"按哪个时区算；北京时间用 `Asia/Shanghai` |

调度按**本地时间对齐**：以当天 00:00 为网格起点，每轮跑完后等到下一个
`MCTTK_INTERVAL` 的整数倍时刻再跑（`3600` → 整点，`600` → 每 10 分钟），
不会因为单轮耗时而逐轮漂移；容器启动后的第一轮仍然是立即执行。
`UTC` 与北京时间相差整小时，所以两种时区下"整点"是同一批时刻。

排查历史问题直接看 `logs/` 目录即可；常见过滤：

```bash
grep -E "\[汇总\]|发帖成功|发帖失败|ERROR" logs/mcttk_*.log
grep "来源 minecraft_api" logs/mcttk_*.log    # 确认官方 API 是否有返回
```


## 新闻来源

### Mojang Piston 版本清单（补充 Java 版本日志）

官方搜索 API 的 `sortType=Recent` 按**索引时间**而非发布日期排序，版本更新日志可能不在返回窗口内。因此程序额外读取 Mojang 版本清单来补齐 Java 版本日志：

```json
{
  "version_manifest": {
    "enabled": true,
    "manifest_url": "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
    "max_versions": 5,
    "url_templates": {
      "java_snapshot": "https://www.minecraft.net/zh-hans/article/minecraft-{version_id}",
      "java_release": "https://www.minecraft.net/zh-hans/article/minecraft-java-edition-{version_id}"
    }
  }
}
```

- 两个来源的结果会**按 URL 合并去重**；API 已返回同一篇文章时保留 API 版本（元数据更全）
- 官网对快照与正式版使用**不同 slug**（`minecraft-26-4-snapshot-1` / `minecraft-java-edition-26-3`），
  因此按类型分别配置模板；`url_templates` 中未列出的类型会被跳过
- RC / 预发布版官网没有对应文章页，默认不生成候选，避免产生 404
- 实测：API 返回窗口内只有 `26.4 Snapshot 1`，而版本清单补齐了 `26.3-snapshot-8/9/10`

### Minecraft 官方 API

从 `https://net-secondary.web.minecraft-services.net/api/v1.0/zh-cn/search` 获取最新新闻，支持按类型过滤。

> 注意：`sortType` 必须为 `Recent`（首字母大写）。写成 `recent` 或省略该参数会返回 2018 年的旧文章。

### Feedback 网站

从 `https://feedback.minecraft.net` 爬取更新日志，在 `config/config.json` 的 `feedback_site` 中配置：

> **默认走 Zendesk Help Center 公开 API**（`use_api`，默认 `true`）。页面和 RSS 会触发 Cloudflare
> 挑战（`403` + `cf-mitigated: challenge`），而官方 JSON API 可稳定访问，且列表接口已包含正文，
> 因此优先用 API 抓取；API 不可用时才回退到页面解析。每个栏目需要配置 `section_id`。

```json
{
  "feedback_site": {
    "enabled": true,
    "base_url": "https://feedback.minecraft.net",
    "use_api": true,
    "api_locale": "en-us",
    "sections": [
      {
        "name": "Release Changelogs",
        "name_cn": "基岩正式版更新日志",
        "section_id": "360001186971",
        "enabled": true,
        "articles_count": 3
      },
      {
        "name": "Beta and Preview Information and Changelogs",
        "name_cn": "基岩测试版更新日志",
        "section_id": "360001185332",
        "enabled": true,
        "articles_count": 3
      }
    ]
  }
}
```

Feedback 文章不受 `news_types` 过滤控制，由各 section 的 `enabled` 字段单独控制。

## 新闻类型过滤

在 `config/config.json` 的 `news_types` 中控制只处理哪些类型的官方 API 新闻：

```json
{
  "news_types": {
    "java_release":   true,
    "java_snapshot":  true,
    "java_prerelease": false,
    "java_rc":        false,
    "bedrock_release": true,
    "bedrock_beta":   true
  }
}
```

类型根据文章标题关键词自动判断：

| 关键词 | 类型 |
|---|---|
| `Snapshot` | `java_snapshot` |
| `Pre-Release` / `Prerelease` | `java_prerelease` |
| `Release Candidate` | `java_rc` |
| `Java Edition` / 版本号如 `1.21` | `java_release` |
| `Bedrock` | `bedrock_release` |
| `Beta` / `Preview` | `bedrock_beta` |

## 模块系统

`config/modules_config.json` 定义帖子的头部/尾部模板，按新闻类型自动匹配插入。

- `default_modules`：内置模板（Java 快照、预发布、RC、正式版；基岩测试版、正式版；时评；普通资讯）
- `custom_modules`：自定义模板，始终插入（位于内容与尾部之间）
- `position`：`"start"` 插入头部，`"end"` 插入尾部
- `enabled`：`true` 表示对所有类型生效，`false` 表示仅对匹配类型生效
- `order`：排序权重，数字越小越靠前

每个模块支持 `content`（BBCode）、`bbcode_content`、`markdown_content` 字段，转换时自动选择对应格式。

## 智能词汇表

词汇表分两层，**同名条目以手写层为准**：

| 文件 | 来源 | 说明 |
|---|---|---|
| `glossary/glossary.json` | 人工维护（入库） | 覆盖层，可写"条件说明"，优先级最高 |
| `glossary/glossary.auto.json` | `tools/sync_glossary.py` 生成（不入库） | 自动层词条，来自 [CFPAOrg 官方术语表](https://github.com/CFPAOrg/Glossary) |
| `glossary/glossary.auto.index.json` | 同上（不入库） | 溯源索引：术语 → TSV 原始键。**运行时不会加载，也不进提示词**，只用于人工复核/调试 |

手写层示例：

```json
{
  "placeholders": { "(*)": "[自动替换为英文原词]" },
  "terms": {
    "Snapshot": "快照",
    "Bedrock": "独立出现译为'基岩'，'Minecraft 26.10 Bedrock'此类译为'Minecraft: 基岩版 26.10'"
  }
}
```

翻译时自动扫描文本，只把**本批次命中的术语**加到提示词里，批量翻译时按批次检测。

### 同步官方术语表

```bash
python tools/sync_glossary.py                          # 默认：mcmeta 最新快照
python tools/sync_glossary.py --source cfpa            # 用 CFPAOrg 稳定版 TSV
python tools/sync_glossary.py --source both            # 两者合并，快照优先
python tools/sync_glossary.py --stats                  # 只看统计，不写文件
python tools/sync_glossary.py --lang en.json zh.json   # 离线：用本地语言文件
python tools/sync_glossary.py --tsv data/raw/latest.tsv   # 离线：用本地 CFPA TSV
python tools/sync_glossary.py --no-index               # 不写 glossary/glossary.auto.index.json
```

**数据源怎么选**：

| 源 | 取自 | 新鲜度 | 适用 |
|---|---|---|---|
| `mcmeta`（默认） | [misode/mcmeta](https://github.com/misode/mcmeta) 的 `assets` 分支，即每次快照的游戏资源 | 快照当天更新 | 跟进新内容 |
| `cfpa` | [CFPAOrg/Glossary](https://github.com/CFPAOrg/Glossary) 的 `Vanilla/latest.tsv` | 以正式版为基准 | 追求稳定 |

实测差异：`mcmeta` 当时是 `26.4-snapshot-8`，CFPA 的 `latest.tsv` 只到 `26.3`，
于是 `Frostbite`（新生物，官译**冻尸**）整个缺失 —— 新内容术语只有前者能覆盖。
两条源都跑过：mcmeta 8760 行 → 2199 强制 + 318 提示 + 47 严格提示，比 CFPA 多 23 个强制词条
（`冰球` `冰晶` `脆冰` `方解石柱` `冰冻之箭`…）。

快照译名可能随后续快照调整，追求稳定时可以 `--source cfpa`，或用 `--source both` 兼顾。

脚本不做"把语言文件拍平"这种傻事，而是：

1. **过滤**：丢掉整句游戏内提示语（含 `%s` / `§` / 句号）、未汉化条目（`Fern → Fern`），
   以及 `subtitles` / `options` / `gui` / `commands` 等命名空间（那些是 UI 文案和整句）
2. **分层**：同一个英文词在句中和独立出现时译法不同，用注入条件区分

   | 层 | 规模 | 注入条件 | 例 |
   |---|---|---|---|
   | `terms`（强制） | ~2200 | 命中即注入 | `Sulfur Cube → 硫方怪` |
   | `soft` | ~300 | 整段独立出现，或原文大小写与词条完全一致 | `Frostbite → 冻尸` |
   | `soft_strict` | ~45 | 仅整段独立出现 | `Speed → 迅捷`（句中 `speed` 是"速度"） |

3. **记录歧义**：同一英文有多个官方译名时写入 `ambiguous`，提示词会附带"另有官方译名：…，按语境选择"
4. **独立溯源索引**：`glossary/glossary.auto.index.json` 记下每个术语来自哪个语言文件键
   （如 `Speed ← effect.minecraft.speed`），用来解释"为什么它被分到这一层"、
   排查误注入。它**只给人看**：运行时 `_read_glossary_file` 不读它，更不进提示词，所以是零 token 成本。
   文件头还记了 `source` 与 `source_version`，一眼能看出词表对应哪个游戏版本。

### 成本

按命中注入，**词表多大都不影响 token**。250 批真实文章实测：每批平均注入 1.3 条、约 48 字符（≈32 token），
匹配耗时 0.2 ms/批。反例是把全表塞进 prompt：2590 条约 31000 token/次，250 批就是 780 万 token。

### 译名一致性校验（默认开启）

翻译完成后做一次本地校验：**强制层**译名是否真的出现在译文里，漏用的条目单独补译一轮。

- 只校验"译名是纯中文短词"的条目。`Minecraft: 基岩版`、`混沌立方(*)`、
  `只有是状态效果时译为…` 这些值没法做包含判断，直接跳过；提示层看语境，也不校验
- **不会死循环**：判定是纯本地的子串包含（同一输入永远同一结论，不存在 A/B 振荡），
  轮数固定为 `verify_max_rounds`（默认 1），且"只重译仍未通过的条目、新译文通过校验才替换旧译文"，
  修复数为 0 时立即停止 —— 允许 5 轮时实测也只调用 2 次

配置：

```json
"glossary": {
  "verify": true,
  "verify_max_rounds": 1,
  "verify_max_items": 20
}
```

代价：最坏情况每批多 1 次请求，且只含漏用译名的条目。按历史译文估算，未注入词表时约 24% 的批次会触发补译，
注入后应显著低于此值。

> 注：**不做**"占位符锁词"（把术语替换成 `⟦T1⟧` 让模型保留）。实测代价大于收益：
> 中文语序/量词会跟着占位符走样、模型偶尔会翻译或改写占位符、和 Markdown 链接/反引号冲突，
> 而一致性收益用上面的注入 + 校验已经拿到绝大部分。

## 智能去重机制

为避免网页结构问题导致的内容重复，系统采用三层去重：

1. **连续去重**：去除相邻的重复 block
2. **大段重复检测**：检测连续 5 个以上 block 的重复序列，移除整段重复内容
3. **长文本去重**：对超过 80 字符的长文本进行跟踪，15 个 block 内再次出现则认为是异常重复（列表项除外）

## 模块说明

### utils.py

- `classify_article_type(title, *, chinese, commentary, fallback)` — 统一的文章类型分类函数，供 scraper / converter / poster 共用
- `load_dotenv(project_dir)` — 加载 `.env` 文件到环境变量
- `MODULE_TYPE_MAP` — 模块 ID 到文章类型的映射表

### scraper.py

- `get_latest_news_list()` — 从官方 API 获取新闻列表（按 URL 去重）
- `get_java_news_from_manifest()` — 从 Mojang Piston 版本清单补齐 Java 版本日志候选
- `classify_news_type(title)` — 根据标题判断新闻类型
- `parse_article_page(url)` — 解析文章页面，提取结构化内容块
- `translate_text(text, response_schema=...)` — 调用 AI API 翻译单段文本（支持词汇表与结构化输出）
- `translate_blocks(blocks)` — 批量翻译内容块（支持并发，兼容多种返回格式）
- `process_article(news_item)` — 完整处理单篇官方 API 文章
- `FeedbackScraper` — Feedback 网站爬虫类（使用 curl_cffi 绕过 Cloudflare）
- `process_feedback_news(news_item, config)` — 完整处理单篇 Feedback 文章
- `save_article_json(data, save_dir)` — 安全保存 JSON（自动处理文件名冲突）

### converter.py

- `J2MMConverter` — 主转换器类
  - `convert_to_bbcode(json_data)` — 转为 MCBBS BBCode 格式
  - `convert_to_markdown(json_data)` — 转为 Markdown 格式
- `convert_json_file(json_path)` — 文件级别转换

也可独立使用：

```bash
python converter.py news_xxx.json            # 同时输出 .txt 和 .md
python converter.py --batch ./output/        # 批量转换目录
python converter.py news.json --bbcode-only  # 仅输出 BBCode
python converter.py news.json --markdown-only
```

### poster.py

- `MCBBSPoster` — 发帖器类
  - `login()` — 登录 MCBBS（支持验证码自动识别，最多重试 5 次）
  - `upload_image(path)` — 上传图片附件
  - `upload_file(path)` — 上传普通文件附件（如 JSON）
  - `post_thread(title, message)` — 发帖
  - `post_news_file(stem, txt, json, dir)` — 发布单个新闻文件（含图片和 JSON 附件）

也可独立使用：

```bash
python poster.py                          # 发布所有未发布的文件
python poster.py --dry-run                # 预览模式
python poster.py news_Minecraft_xxx       # 发布指定文件
python poster.py --no-image               # 跳过图片上传
python poster.py --no-json                # 跳过 JSON 附件
```

### init_state.py

测试辅助工具。运行后列出所有当前新闻，选择保留一条不标记为已处理，其余全部标记为已处理，避免测试时触发全量翻译。

```bash
python init_state.py
```

## 处理流程

```
Minecraft 官方 API      Piston 版本清单        Feedback 网站
       ↓                      ↓                     ↓
  获取新闻列表          取最新 Java 版本      获取各 section 文章列表
       ↓                      ↓                     ↓
  按类型过滤 (news_types)  按 url_templates    按 section.enabled 过滤
       └──────────┬───────────┴─────────────────────┘
                  ↓
          按 URL 合并去重
                  ↓
          检查已处理状态 (.state.json)
                  ↓
          [对每篇新文章]
                  ↓
          解析文章页面 → 提取结构化 blocks
                  ↓
          AI 翻译标题 + 内容（并发批量，JSON Schema 结构化输出）
                  ↓
          保存 JSON (output/news_*.json)   ← 失败则记录次数，下次重试
                  ↓
          转换 BBCode (output/news_*.txt)
          转换 Markdown (output/news_*.md)
                  ↓
          下载头图 (output/news_*.jpg)
                  ↓
          登录 MCBBS → 上传图片 → 上传 JSON → 发帖
                  ↓
          记录已发布状态 (.posted.json)
```

## GitHub Actions 部署

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 说明 |
|---|---|
| `OPENAI_API_KEY` | AI 翻译 API 密钥 |
| `MCBBS_USERNAME` | MCBBS 论坛用户名 |
| `MCBBS_PASSWORD` | MCBBS 论坛密码 |
| `MCBBS_CAPTCHA_ANSWER` | 验证码备用答案（未启用） |

Workflow 默认每 6 小时运行一次（UTC 0:00, 6:00, 12:00, 18:00），也可手动触发。

### 发布版本（打 tag 自动出镜像 + Release）

推送一个 `v主.次.修订` 形式的 tag，`.github/workflows/release.yml` 会自动：

1. 复用 `ci.yml` 跑 Ruff + pytest 作为门禁（不通过就不发布）
2. 构建镜像并推送到 `ghcr.io/jiubook/mcttk`，标签为 `1.0.0`、`1.0`、`latest`
3. 生成离线包 `mcttk-v1.0.0.tar`（未压缩，兼容只收未压缩包的导入界面；校验和取 GitHub
   为附件自动生成的 SHA256 digest，不再单独上传 `.sha256`）
4. 创建 GitHub Release，自动生成变更说明并挂上离线包

```bash
git tag v1.0.0
git push origin v1.0.0
```

服务器部署与离线部署的完整说明见 [docs/DOCKER.md](docs/DOCKER.md)。

## 配置参考

完整的 `config/config.json` 结构：

```json
{
  "openai_compat": {
    "host": "api.example.com",
    "endpoint": "/v1/chat/completions",
    "api_key_env": "OPENAI_API_KEY",
    "api_key": "",
    "model": "gpt-4o",
    "json_schema": true,
    "max_tokens": 10000,
    "timeout": 120
  },
  "minecraft_api": {
    "search_url": "https://net-secondary.web.minecraft-services.net/api/v1.0/zh-cn/search",
    "pageSize": 5,
    "sortType": "Recent",
    "category": "News",
    "site_base": "https://www.minecraft.net"
  },
  "version_manifest": {
    "enabled": true,
    "manifest_url": "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
    "max_versions": 5,
    "url_templates": {
      "java_snapshot": "https://www.minecraft.net/zh-hans/article/minecraft-{version_id}",
      "java_release": "https://www.minecraft.net/zh-hans/article/minecraft-java-edition-{version_id}"
    }
  },
  "feedback_site": {
    "enabled": true,
    "base_url": "https://feedback.minecraft.net",
    "knowledge_base_url": "https://feedback.minecraft.net/hc/en-us/categories/115000410252-Knowledge-Base",
    "timeout": 30,
    "sections": []
  },
  "http": {
    "verify_ssl": false,
    "ca_bundle": "",
    "user_agent": "Mozilla/5.0 ...",
    "proxies": { "http": "", "https": "" },
    "timeout": 120
  },
  "news_types": {
    "java_release": true,
    "java_snapshot": true,
    "java_prerelease": true,
    "java_rc": true,
    "bedrock_release": true,
    "bedrock_beta": true
  },
  "mcbbs": {
    "enabled": false,
    "base_url": "https://www.mcbbs.co",
    "forum_fid": 45,
    "username": "",
    "password": "",
    "captcha_answer": "",
    "attach_json": true,
    "sortid_map": {}
  },
  "output": {
    "save_dir": "output"
  },
  "retry": {
    "translation": { "max_retries": 3 },
    "download": { "max_retries": 3 },
    "max_article_attempts": 3
  },
  "concurrency": {
    "translation_workers": 3,
    "batch_max_chars": 1000,
    "batch_max_items": 10
  }
}
```

## 注意事项

- **首次运行**：程序会自动将当前所有新闻标记为已处理，下次运行才开始处理真正的新新闻，避免刷屏
- **API Key**：支持通过环境变量 `OPENAI_API_KEY` 传入，优先级高于 config.json
- **MCBBS 账号**：支持通过环境变量 `MCBBS_USERNAME` / `MCBBS_PASSWORD` 传入
- **验证码**：登录时若遇到验证码，会自动使用 `ddddocr` 识别，最多重试 5 次；识别失败时使用 `captcha_answer` 备用答案
- **输出文件**：文件名自动处理非法字符，同名文件自动加序号避免冲突
- **失败重试**：处理失败的文章**不会**被标记为已处理，下次运行会自动重试；
  连续失败达到 `retry.max_article_attempts`（默认 3）次后才放弃，避免个别坏链接被反复重试
- **翻译丢译文**：批量翻译开启了结构化输出（`openai_compat.json_schema`），
  解析同时兼容 `{"translations": [...]}` / 裸数组、`translated_text` / `text` 两种字段名。
  若某批返回完全无法解析，日志会输出 `[翻译] 警告: ...` 而不是静默丢弃
- **400 错误 / 结构化输出降级**：结构化输出按能力降级——
  `response_format={"type": "json_schema"}` → `{"type": "json_object"}` → 不传该字段（纯提示词约束）。
  DeepSeek 等接口只支持 `json_object`，收到 `json_schema` 会返回 400
  （`This response_format type is unavailable now`），此时程序会自动降级并记住该模型不支持，
  后续批次不再重复撞 400。HTTP 错误会连同响应体一起打印，便于定位原因
- **Feedback 抓取 TLS 报错**：`curl_cffi` 自带 certifi 信任库、不走系统证书存储，
  代理 / VPN / 杀毒软件做 TLS 中间人解密时会报
  `curl: (60) unable to get local issuer certificate`。`http.verify_ssl: false`
  现在同时作用于 `curl_cffi`（跳过校验）；想保留校验时用 `http.ca_bundle` 指向
  中间人软件的 CA 证书（`.pem`）。日志会给出对应的处理建议
- **Feedback 被 Cloudflare 拦截**：页面与 RSS 会返回挑战页（`403` + `cf-mitigated: challenge`），
  这是站点的机器人防护，硬碰没有意义。抓取默认走 Zendesk Help Center 公开 API
  （见上文 [Feedback 网站](#feedback-网站)）：列表接口一次带回标题、链接与正文，
  失败时才回退页面解析。若机房 IP 连 API 都被挑战，请降低频率或改用代理，而不是加大重试
- **配置文件不入库**：仓库只保留 `config/config.sample.json` 模板，`config/config.json` 已被 `.gitignore` 忽略；
  首次使用请复制模板。API Key、MCBBS 密码等敏感信息建议放在 `.env`（同样被忽略）
- **磁盘管理**：`output/` 目录下的文件不会被自动清理，需手动管理
- **日志**：每轮运行的来源条数、翻译量、保存的文件、发帖地址都会写进 `logs/mcttk_YYYY-MM-DD.log`
  （`print()` 与 stderr 也一并落盘）。容器重建后 `docker logs` 会清空，排查历史问题请以该目录为准
- **状态重置**：删除 `output/.state.json` 后会重新处理所有新闻；删除 `output/.posted.json` 后会重新发布所有已翻译文件

## 许可证

本工具以 [GPL-3.0](https://www.gnu.org/licenses/gpl-3.0.zh-cn.html) 协议发布。

AI 翻译作品以 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.zh-hans) 协议发布。
