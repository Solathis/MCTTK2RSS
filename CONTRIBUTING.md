# 贡献指南

MCTTK（矿译通）是 Minecraft 新闻的**自动爬取 → AI 翻译 → 格式转换 → 发布**工具链。
它每天自动运行，所以这里的要求比一次性脚本更高：改动会被无人值守地反复执行，
一次误判会直接反映到论坛和 B站专栏上。

本文件面向贡献者，包含：怎么开始、提交什么、代码怎么写得和现有一致、怎么验证、
PR 怎么才能合入、以及怎么发版。**唯一权威是代码** —— 下面的清单、命令、路径随时可能
过时，动手前用工具自己确认，不要凭本文档假设文件存在。

- 只想用这个工具？看 [README](README.md) 与 [docs/USAGE.md](docs/USAGE.md)。
- 想部署到服务器/容器？看 [docs/DOCKER.md](docs/DOCKER.md)。

**目录**：[如何开始](#如何开始) · [项目结构](#项目结构) · [贡献方式](#贡献方式) ·
[沟通与反馈](#沟通与反馈) · [代码质量与风格](#代码质量与风格) · [测试与验证](#测试与验证) ·
[模块说明](#代码地图模块说明) · [数据处理流程](#代码地图数据处理流程) ·
[审核与接受标准](#审核与接受标准) · [持续集成与发版](#持续集成与发版)

## 如何开始

### 环境

需要 Python 3.13 与 [uv](https://docs.astral.sh/uv/)。本地没有 `uv` 时可以用 `.venv` 里的
可执行文件等价替换（`.venv\Scripts\ruff.exe check .`、`.venv\Scripts\python.exe -m pytest tests/ -q`），
但**提交前必须以 `uv run` 的形式复现一次**，否则和 CI 不一致。

```bash
uv sync                          # 基础依赖
uv sync --extra captcha          # ddddocr + Pillow，MCBBS 验证码识别
uv sync --extra feedback         # curl_cffi，Feedback 站点抓取

uv run ruff check .              # 质量门禁（与 CI 完全一致）
uv run pytest tests/ -q
```

### 先跑一遍基线

动代码之前先跑 `uv run ruff check .` + `uv run pytest tests/ -q`，确认已有失败与你的改动
无关。测试必须能在**无网络、无 `config/config.json`、无 `glossary.auto.json`** 的环境下通过：
这三者缺失时程序应降级运行，不能崩。

### 本地跑起来

```bash
uv run python main.py --dry-run        # 只探测新新闻，不翻译不发帖（默认用这个验证）
uv run python main.py --scrape-only    # 抓取 + 翻译 + 转换，不发帖
uv run python main.py --post-only      # 只发 output/ 里还没发的
uv run python scheduler.py             # 定时循环（容器默认入口）
docker compose config -q               # 编排语法自检
```

配置与全部 CLI 参数见 [docs/USAGE.md](docs/USAGE.md)。**没有明确授权时不要真实发帖、
不要真实调用翻译 API 消耗额度**：验证一律用 `--dry-run` / `--scrape-only`，或测试里的
fake session。

## 项目结构

```
MCTTK/
├── main.py                    # 编排器：串联爬取→翻译→转换→发布
├── scraper.py                 # 爬取与翻译模块（含 Feedback 爬虫）
├── converter.py               # JSON → BBCode/Markdown/专栏产物 转换器
├── poster.py                  # MCBBS 发帖模块（含验证码识别）
├── bili/                     # B站专栏（包）：投稿器 + 两套正文渲染器
│   ├── poster.py              # 创作中心 web 接口投稿（cookie 会话 + 图片重托管）
│   ├── html.py                # 渲染器①：老接口的 HTML 正文（真机验证过的路径）
│   └── opus.py                # 渲染器②：新版编辑器的 eva3 段落正文
├── utils.py                   # 公共工具：路径常量、MODULE_TYPE_MAP、classify_article_type
├── log_setup.py               # 日志配置（按天分文件、stdout 落盘、保留天数）
├── scheduler.py               # 定时调度器（默认每小时运行一次）
├── config/                    # 配置目录
│   ├── config.sample.json     # 配置模板（复制为 config/config.json 后修改）
│   ├── config.json            # 本地配置（不入库，.gitignore 已忽略）
│   └── modules_config.json    # 帖子模板模块配置
├── glossary/                  # 词表目录
│   ├── glossary.json          # 手写术语覆盖层（入库）
│   ├── glossary.auto.json     # 自动词表层（tools/sync_glossary.py 生成，不入库）
│   └── glossary.auto.index.json # 词表溯源索引（同上，仅供人工查阅）
├── tools/                     # 辅助脚本（sync_glossary.py 同步词表；bili_login.py 扫码取 cookie；preview_bili_html.py 预览专栏排版；init_state.py 初始化测试状态）
├── tests/                     # 单元测试（pytest）
├── docs/                      # 文档
│   ├── USAGE.md               # 完整用法与全部配置项
│   ├── DOCKER.md              # Docker 部署说明
│   └── archive/               # 历史评审与 PR 记录
├── data/                      # 本地原始数据（不入库）：latest.tsv、mcmeta 语言文件
├── archive/                   # 历史归档（不入库）：上一代项目、实验脚本、本地备份
├── output/                    # 输出目录（自动生成，不提交）
│   ├── .state.json            # 处理状态（URL 级别去重）
│   ├── .posted.json           # MCBBS 发布状态
│   ├── .posted_bili.json      # B站专栏投稿状态
│   ├── .mcbbs_threads.json    # MCBBS 帖子记录（tid / 高亮 / 审核状态）
│   ├── news_*.json            # 翻译后的文章数据
│   ├── news_*.txt             # BBCode 格式（用于 MCBBS）
│   ├── news_*.md              # Markdown 格式
│   ├── news_*.html            # 专栏 HTML 正文（renderer=html 时生成）
│   ├── news_*.opus.json       # 专栏段模型正文（renderer=opus 时生成）
│   └── news_*.jpg             # 文章头图
├── logs/                      # 运行日志（自动生成，不提交）
│   └── mcttk_YYYY-MM-DD.log   # 每天一个文件，见 docs/USAGE.md 的「日志」章节
├── pyproject.toml             # 项目配置（依赖声明、Ruff 规则、pytest 路径）
├── requirements.txt           # pip 依赖（Docker 构建用）
├── Dockerfile                 # 镜像构建
├── docker-compose.yml         # 容器编排（本地构建）
├── docker-compose.release.yml # 容器编排（直接拉取发布镜像）
├── .env                       # 本地环境变量（不提交）
├── README.md                  # 项目简介与快速开始
├── CONTRIBUTING.md            # 贡献指南（本文件）
```


## 贡献方式

### 报告问题

开 issue，附上：复现步骤、实际结果与期望结果、相关日志（`logs/mcttk_YYYY-MM-DD.log` 的关键行）。
**不要在 issue 里贴 `.env`、`config/config.json`、`output/` 的内容**，日志里也不要有
`api_key` / 密码 / 完整请求头。

### 提交改动

- `main` 只接受经 PR 合入的改动，不直接 push；tag 只从 `main` 打。
- 分支名 `<type>/<小写短横线主题>`，`type` 取 `feature` / `fix` / `refactor` / `docs` /
  `chore` / `test` / `ci`，例如 `fix/translate-truncation-retry`。从最新 `main` 拉：
  `git switch -c fix/xxx origin/main`。
- 一个分支只做一件事；无关改动另开分支。有效期控制在一到两周，合入后删掉远端分支。
- 允许在分支上整理自己的历史（rebase / squash），但**评审开始后不要 force push**；
  确需重排先在 PR 里说明。

### Commit

沿用 **Conventional Commits + 中文标题**：

```
<type>(<scope>): <中文简述，不超过 50 字，不加句号>

为什么改：问题、触发条件、影响（1-3 句）。为什么选这个做法、放弃了什么方案。

- 关键改动 1
- 关键改动 2

验证：uv run ruff check . / uv run pytest tests/ -q（NNN passed）
```

- 标题写**结果**（"批次返回被截断时不再整批丢弃"），不写过程（"改了几个函数"）。
- 正文解释"为什么"，不复述 diff；关键取舍、被否决的方案、实测数据放这里。
- **一个问题一个 commit**，且做完一项就落一个本地 commit，不要攒到最后一次性提交。
  重构/移动/格式化必须与逻辑改动分开提交，否则 review 无法定位。
- 不 amend / rebase 已推送的提交；要撤销用 `git revert`。
- 关联 issue 写 `Refs #12` 或 `Closes #12`。**`#N` 只用来引用真实 issue/PR**，评审条目、
  步骤序号写「第 N 条」，不要写成 `#N`（GitHub 会把它链到别的 issue）。

## 沟通与反馈

- 结论先行，跳过客套。有不同意见直说并给理由；不确定就说不确定。
- 分析走第一性原理：先确认链路（谁调用谁、参数从哪来、返回值去哪），再谈改法。
- 评审意见**要么改，要么在 PR 里说明为什么不改**，不要静默忽略。
- 大改动先开 issue 或 draft PR 对齐方向，不要写完 500 行才让人看。
- 行为变更必须同步文档：CLI 参数 / 配置项 → `docs/USAGE.md`，部署与镜像 →
  `docs/DOCKER.md`，贡献流程 → 本文件。历史评审与阶段性结论放
  `docs/archive/YYYY-MM-DD-<主题>.md`，不要堆在根目录。

## 代码质量与风格

### 布局与依赖方向

- 仓库以**平铺布局**为主：通用模块在根目录，没有 `src/`；只有成组的专用模块才建包
  （目前只有 `bili/`）。新增通用模块放根目录，平台专用模块放对应包内。
- 依赖方向单一：`main` → `scraper` / `converter` / `poster` → `utils` / `log_setup`。
  反向导入（`scraper` 里 import `main`）一律禁止。
- 新增公共逻辑先看 `utils.py` 是否已有；同一段逻辑出现第二次就抽出去。
- 不为单个调用点造抽象：没有第二个使用者的"预留扩展点"不进代码。

### 配置

- 优先级：`scraper.DEFAULT_CONFIG` → `config/config.json`（`_deep_merge` 覆盖）→ 环境变量
  → CLI 参数。
- **禁止在模块顶层求值 `load_config()` / `load_glossary()`**（import 就联网/读盘，测试无法
  注入）。用懒加载，需要注入的公开函数保留 `config=None` / `glossary=None` 参数。
- 新增或修改配置项必须同时改齐**五处**：`config/config.sample.json`、`scraper.DEFAULT_CONFIG`、
  `docs/USAGE.md` 的配置章节、`tests/test_config_json.py` 的结构断言，以及本机的
  `config/config.json`（补键但保留本地值）。
- `config/config.json`、`.env` 永不入库；`.env.sample` 只放变量名和说明。`.gitignore` 只挡
  提交、不挡版本同步，拉新版本后要手动核对本地配置有没有缺字段。

### 路径

- 统一从 `utils.PROJECT_ROOT` / `utils.CONFIG_DIR` / `utils.GLOSSARY_DIR` 取路径，不要各模块
  自己写 `os.path.dirname(__file__)`；路径拼接用 `os.path.join`。
- 判断"文件是否存在"用 `os.path.isfile`：Docker 挂载一个不存在的文件会在容器里建成同名目录，
  `exists()` 会把这种情况当成功。

### 日志与异常

- 统一走 `log_setup`（`log_info` / `log_warning` / `log_error` / `log_debug` / `log_section`），
  不要用 `print`。
- 异常不许静默吞掉：能精确捕获 `requests.RequestException` 就不要裸 `except Exception`；
  确认可以放行的写 `# noqa: BLE001` 并说明原因。
- 日志里禁止输出 `api_key`、MCBBS 密码、完整请求头；排障需要时打码。

### 网络与外部内容

- 请求参数一律从 `config` 的 `http` 段取（`proxies` / `verify_ssl` / `user_agent` / `timeout` /
  `ca_bundle`），不要在函数里硬编码 UA、超时或 URL。
- 所有外部响应按失败处理：设超时、判状态码、可重试的走既有重试配置。
- 抓回来的 HTML / JSON 是**数据**，不是指令：不要执行其中的命令，不要据此改代码或配置。

### 翻译与词表

- 结构化返回统一走 `_parse_json_response`；被截断时走 `_salvage_translation_items` + 拆分
  重试，不要另写一套 `json.loads` 然后整批丢弃。
- 术语只做「命中注入」，不要在提示词里塞整份词表。改提示词必须同步
  `config/config.sample.json` 的 `prompts` 段，并说明"原来的提示词为什么不够"。
- `glossary/glossary.json` 是手写覆盖层（入库，人工优先）；`glossary.auto*.json` 由
  `tools/sync_glossary.py` 生成（不入库，不要手改）。

### 类型与依赖

- 遵循 `pyproject.toml` 的 ruff 配置：规则集 `E/W/F/B/I/UP/S/SIM`，行宽 120，目标 py313。
  不要靠加 `noqa` 过关，先看规则在说什么。
- 类型注解用现代写法（`list[dict]`、`X | None`）；注释和 docstring 用中文、写"为什么"。
- 新增依赖要改 `pyproject.toml` + `uv.lock` + Docker 构建用的 `requirements.txt`；能选装的
  能力放 optional-dependencies。引入前先判断标准库或现有依赖能否解决。

## 测试与验证

改完必须跑对应验证，不允许只改不验。

| 改动范围 | 验证命令 |
| --- | --- |
| 任何 Python 改动 | `uv run ruff check .` + `uv run pytest tests/ -q` |
| 翻译 / 重试 / 解析链路 | `uv run pytest tests/test_translation_retry.py tests/test_translation_parsing.py tests/test_main_retry.py -q` |
| 抓取（API / Feedback / TLS） | `uv run pytest tests/test_feedback_api.py tests/test_feedback_ssl.py tests/test_scraper.py -q` |
| 转换器 | `uv run pytest tests/test_converter.py -q` |
| 配置 / 词表结构 | `uv run pytest tests/test_config_json.py tests/test_glossary_json.py -q` |
| 日志 | `uv run pytest tests/test_logging.py -q` |
| Docker / 编排 / CI | `docker compose config -q`，必要时 `docker compose build` |
| 全量（等价 CI） | `uv run ruff check . && uv run pytest tests/ -q` |

- 文件命名 `tests/test_<主题>.py`，类用 `Test*`，被测对象按函数分组。
- 网络一律 mock（沿用 `FakeSession` / `FakeResponse` / `curl_cffi` stub + `monkeypatch`）：
  测试里不许真联网、真发帖、真调翻译 API。
- 只测纯逻辑也要覆盖**失败路径**（截断、超时、空响应、坏 JSON、词表缺失）。
- 不要在测试里写死会被业务改动的字面量（条数、行号、完整提示词）；断言行为，不断言实现细节。
- 修 bug 时先补一个能复现它的测试，再改代码。

## 代码地图：模块说明

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
  - `login()` — 登录 MCBBS（验证码流程内最多尝试 5 次；整体登录失败会按 `retry.login` 换新会话重试）
  - `upload_image(path)` — 上传图片附件
  - `upload_file(path)` — 上传普通文件附件（如 JSON）
  - `post_thread(title, message)` — 发帖，返回 `PostResult`（帖子地址、帖子 ID、是否需要审核）
  - `post_news_file(stem, txt, json, dir)` — 发布单个新闻文件（含图片和 JSON 附件），
    成功后按 `mcbbs.highlight` 登记待补高亮
  - `highlight_thread(tid, color, bold)` — 设置标题高亮（`bold=False` 即取消加粗、保留颜色）
  - `fetch_my_threads()` — 拉「我的帖子」列表（含待审核项），用于补帖子 ID
  - `index_history(dir)` / `promote_legacy(records, n)` — 历史帖登记与限量提升
  - `process_pending_highlights(records)` — 补高亮：游客探测过审 → 需要才登录 → 上色/改加粗
  - `reconcile_bold(records, pages, dry_run)` — 按 4 个分组（Java/基岩 × 正式版/测试版）
    各留最新一篇加粗，其余取消加粗
  - `highlight_status(records)` — 记录与站点现状逐条对照（只读）
- `probe_thread(base, tid)` — 游客视角探测帖子是否可见（`ThreadProbe.visible` 为
  `True`/`False`/`None` 探测失败），顺带取出面包屑里的版块 `fid` 与版块名，用于排除回收区

### bili/poster.py

- `BilibiliPoster` — 专栏投稿器（cookie 会话，走创作中心 web 接口）
  - `check_login()` — 校验 `SESSDATA` / `bili_jct` 是否还能用（顺带缓存 WBI 签名用的键）
  - `upload_image(data)` — 上传图片到 B站图床（封面走 `upcover`）
  - `rehost_images(html)` — 把正文外链图重托管到 B站 CDN，失败的直接从正文移除
  - `save_draft(title, content)` — 存草稿（分区无权限时按候选表回退）
  - `publish(title, content)` — 存草稿并按 `submit` 决定是否提交审核
  - `save_opus_draft(title, paragraphs)` — eva3 存草稿（封面 / 摘要 / AI 声明一起写入）
  - `publish_opus(draft, title, paragraphs)` — 按草稿号发布（`create/opus`）
  - `upload_bfs_image(data)` / `rehost_opus_images(paragraphs)` — 段模型正文图片上传（带宽高体积）
  - `post_news_file(stem, json, content_path, cover_path)` — 单篇完整投稿，按 `renderer` 分派
- `load_cookies(cfg)` — 取登录凭据：环境变量优先于 `config/bili_cookies.json`
- `build_opus_draft_arg(...)` / `build_opus_publish_payload(...)` — 两个请求体的纯函数构造器

### bili/html.py / bili/opus.py

专栏有两套互斥的正文形态，各一个渲染器（纯函数，不联网）：

| 模块 | 产出 | 用于 |
| --- | --- | --- |
| `bili/html.py` | HTML 字符串（`p/h1/ul/figure/a/strong`） | 老接口 `creative/draft/addupdate`，已真机投稿验证 |
| `bili/opus.py` | `opus.content.paragraphs` 段数组 | 新版编辑器 `dynamic/feed/article/draft/add`（存草稿）+ `dynamic/feed/create/opus`（发布），两条都已真机验证 |

两者都由 `converter.J2MMConverter` 组装：`convert_to_bili_html()` / `convert_to_bili_opus()`。
头尾模块同样先按 `platform` 过滤、再压成 Markdown 交给各自渲染器，所以两边的段落边界一致
（同一篇的引用块、列表、双语降级位置完全相同，只有承载格式不同）。

**超链接策略**：只保留项目署名 `github.com/jiubook/MCTTK`，其余外链一律降级成纯文本
`文字（URL）`（地址留在正文里可复制，文字就是地址时只留一次）。原因是 B站 图文里点外链
先进「离开 B站」的拦截页（实测），正文十几个外链等于十几个提示页。策略写在
`bili/__init__.py` 的 `SIGNATURE_HOST` / `SIGNATURE_PATH` 与 `degrade_link()`，两套渲染器共用。

**原文刷灰**：段模型的原文对照（引用框、列表项第二行、英文原标题）用 `color` 写成
`#c9ccd0` 的灰色（编辑器取色板里的 `--Ga3`），译文保持默认色；HTML 版没有颜色能力
（内联样式会被编辑器剥掉）。
灰度常量是 `bili/opus.py::ORIGINAL_COLOR`。

投稿前先看排版（近似样式，正式排版由专栏编辑器决定）：

```bash
uv run python tools/preview_bili_html.py                 # 默认 HTML 渲染器
uv run python tools/preview_bili_html.py --renderer opus  # 看段模型渲染出来是什么样
uv run python tools/preview_bili_html.py --no-modules     # 不带头尾模块，只看正文
```

也可独立使用：

```bash
python poster.py                          # 发布所有未发布的文件
python poster.py --dry-run                # 预览模式
python poster.py news_Minecraft_xxx       # 发布指定文件
python poster.py --no-image               # 跳过图片上传
python poster.py --no-json                # 跳过 JSON 附件
```

### tools/init_state.py

测试辅助工具。运行后列出所有当前新闻，选择保留一条不标记为已处理，其余全部标记为已处理，避免测试时触发全量翻译。

```bash
uv run python tools/init_state.py
```

## 代码地图：数据处理流程

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
          转换专栏正文 (news_*.html | news_*.opus.json)   ← 仅 bilibili.enabled 时按 renderer 生成
                  ↓
          下载头图 (output/news_*.jpg)
                  ↓
          ┌───────────────┴───────────────┐
          ↓                               ↓
  登录 MCBBS → 上传图片/JSON → 发帖   登录 B站 → 图片重托管 → 存草稿/投稿
  记录 .posted.json                   记录 .posted_bili.json
```

## 审核与接受标准

PR 描述必须包含五段，缺一段就是不合格 PR：

```markdown
## 背景
要解决的现象/需求，附复现步骤或日志片段。

## 改动
- 做了什么，为什么这样做
- 影响哪些模块、配置项、输出格式

## 验证
- uv run ruff check . → 通过
- uv run pytest tests/ -q → NNN passed
- 关键命令的输出概要

## 影响面
配置结构 / 输出文件 / Docker 镜像 / 兼容性 / 是否需要用户迁移

## 风险与回滚
最坏情况是什么，怎么回滚（revert 哪个 commit、回滚后要清哪个状态文件）
```

- 规模：有效 diff 尽量 ≤ 400 行；超了就拆。纯移动/纯格式化单独开 PR。
- 合入条件：CI 绿（`ruff check` + `pytest`）、作者自审过完整 diff、无凭据与生成物
  （`config/config.json`、`.env`、`output/`、`logs/`、`data/`、`glossary.auto*.json`）、
  配置模板已同步。
- 合并方式延续仓库现状：GitHub merge commit。若改用 squash merge，PR 标题即最终 commit
  标题，必须符合上面的 commit 规范。
- 破坏性变更（配置结构、输出格式、CLI 参数、镜像行为）在标题加 `!`、描述首段标注
  「破坏性变更」，并给出迁移步骤。

## 持续集成与发版

### CI 定时任务

在仓库 Settings → Secrets and variables → Actions 中添加：

| Secret 名称 | 说明 |
|---|---|
| `OPENAI_API_KEY` | AI 翻译 API 密钥 |
| `MCBBS_USERNAME` | MCBBS 论坛用户名 |
| `MCBBS_PASSWORD` | MCBBS 论坛密码 |
| `MCBBS_CAPTCHA_ANSWER` | 验证码备用答案（未启用） |
| `BILI_SESSDATA` | B站登录 cookie（启用专栏发布时需要） |
| `BILI_JCT` | B站 csrf 令牌，与 `BILI_SESSDATA` 同一登录会话 |

Workflow 默认每 6 小时运行一次（UTC 0:00, 6:00, 12:00, 18:00），也可手动触发。
PR 只跑 `ruff check` + `pytest`；这两个以外的检查不要偷偷加进 PR 流水线。

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

### 发版前检查清单

1. `main` 上 CI 绿，`pyproject.toml` 的 `version` 与即将打的 tag 一致。
2. 文档已跟上版本：`docs/USAGE.md`（CLI / 配置）、本文件（流程）、`docs/DOCKER.md`（部署）。
3. tag 只从 `main` 打；预发布用 `-rc1` 这类后缀，它不会覆盖 `latest` 与 `X.Y`，这是预期行为。
4. 发布失败优先在 Actions 里 "Re-run all jobs"，不要手工改 Release 附件或镜像 tag。

服务器部署与离线部署的完整说明见 [docs/DOCKER.md](docs/DOCKER.md)。
