# MCTTK2RSS

> Minecraft 新闻自动爬取 + 翻译 + RSS 生成

基于 [jiubook/MCTTK](https://github.com/jiubook/MCTTK) 的 fork，移除了 BBCode/Markdown 转换和 MCBBS 论坛自动发布功能，改为自动生成 RSS Feed，支持通过 GitHub Actions 和 Docker 部署。

**所有配置均从 `config.json` 读取，不使用环境变量。** Docker 部署时通过 volume 挂载 config.json；GitHub Actions 部署时通过 Secrets（前缀 `MCTTK2RSS_`）注入到 config.json。

## 功能特性

- **自动爬取**：从 Minecraft 官方 API 获取最新新闻，同时支持从 Feedback 网站爬取更新日志
- **Cloudflare 绕过**：使用 `curl_cffi` 模拟真实浏览器，绕过 Feedback 网站的 Cloudflare 防护
- **AI 翻译**：调用 OpenAI 兼容 API 翻译为简体中文，支持并发批量翻译
- **智能词汇表**：动态检测专业术语，自动添加译名对照到提示词（`glossary.json`）
- **RSS 生成**：将爬取翻译后的新闻自动生成 RSS 2.0 Feed（含 `content:encoded` 全文）
- **GitHub Pages 发布**：通过 GitHub Actions 定时运行并发布 RSS 到 GitHub Pages
- **Docker 部署**：支持 Docker Compose 一键部署，定时爬取并生成 RSS
- **类型过滤**：通过配置控制只处理指定类型的新闻
- **安全去重**：基于 URL 的状态追踪，不会重复爬取
- **首次运行保护**：首次运行时自动将所有现有新闻标记为已处理，避免刷屏（可通过 `first_run_protection` 配置关闭）
- **纯配置文件驱动**：不依赖环境变量，定时规则和首次运行保护开关均写在 config.json 中

## 与上游 MCTTK 的区别

| 项目 | 上游 MCTTK | 本仓库 MCTTK2RSS |
|------|-----------|-----------------|
| 输出格式 | BBCode + Markdown | RSS 2.0 XML |
| 发布方式 | 自动发布到 MCBBS 论坛 | GitHub Pages / Docker 部署 |
| 配置方式 | config.json + 环境变量 | 仅 config.json |
| 验证码识别 | ddddocr | 已移除 |
| 定时调度 | 固定 10 分钟 | config.json 中 cron 规则可配 |
| 默认爬取范围 | Java + 基岩 | 仅 Java 端更新日志 |
| 首次运行保护 | 不可配 | 通过 `first_run_protection` 可配开关 |

## 项目结构

```
MCTTK2RSS/
├── main.py              # 编排器：串联爬取→翻译→RSS生成
├── scraper.py           # 爬取与翻译模块（含 Feedback 爬虫，上游维护）
├── rss_generator.py     # RSS Feed 生成器（本项目新增）
├── scheduler.py         # 定时调度器 + 内置 HTTP 服务器（从 config.json 读取 cron）
├── server.py           # 轻量 HTTP 服务器（提供 feed.xml 静态访问）
├── utils.py             # 公共工具（上游维护）
├── log_setup.py         # 日志系统（上游维护）
├── init_state.py        # 初始化状态工具（测试用）
├── config.json          # 统一配置文件（所有配置仅在此处）
├── glossary.json        # 专业术语词汇表（上游维护）
├── pyproject.toml       # 项目配置（依赖声明、Ruff 规则、pytest 路径）
├── requirements.txt     # pip 依赖（Docker 构建用）
├── tests/               # 单元测试（pytest）
├── output/              # 输出目录（自动生成，不提交，Docker 挂载写回）
│   ├── .state.json      # 处理状态（URL 级别去重）
│   ├── news_*.json      # 翻译后的文章数据
│   ├── feed.xml         # RSS Feed 输出
│   └── news_*.jpg       # 文章头图
├── .github/workflows/
│   ├── ci.yml           # CI 测试
│   ├── rss-publish.yml  # RSS 定时爬取 + GitHub Pages 发布
│   └── docker-build.yml # Docker 镜像构建 + 推送到 GHCR
├── docker-compose.yml   # Docker 部署配置
├── Dockerfile           # Docker 镜像构建
├── .gitattributes       # 上游同步保护策略
└── UPSTREAM_SYNC.md     # 上游同步指南
```

## 快速开始

### 1. 安装依赖

推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
# 可选：Feedback 网站爬虫需要 curl_cffi
uv sync --extra feedback
```

也可使用 pip：

```bash
pip install -r requirements.txt
```

### 2. 配置

所有配置写在 `config.json` 中，不使用任何环境变量：

```json
{
  "openai_compat": {
    "host": "api.your-provider.com",
    "endpoint": "/v1/chat/completions",
    "api_key": "sk-xxx",
    "model": "gpt-4o",
    "json_schema": true,
    "max_tokens": 10000,
    "timeout": 120
  },
  "first_run_protection": true,
  "scheduler": {
    "cron": "0 */6 * * *",
    "interval_seconds": 21600,
    "timeout_seconds": 600
  },
  "server": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "rss": {
    "feed_title": "Minecraft News (中文翻译)",
    "output_path": "output/feed.xml",
    "max_items": 50
  }
}
```

### 3. 运行

```bash
# 全流程：爬取 → 翻译 → 生成 RSS
python main.py

# 仅检测新新闻（不实际处理）
python main.py --dry-run

# 仅从 output 目录重新生成 RSS（不爬取）
python main.py --rss-only

# 也可单独运行 RSS 生成器
python rss_generator.py --dir output --out output/feed.xml
```

## 部署方式

### 方式一：GitHub Actions（推荐）

无需服务器，GitHub Actions 定时运行并发布 RSS 到 GitHub Pages。

**定时规则**：GitHub Actions 的 cron 在 workflow yml 中配置（默认每 6 小时），应与 config.json 中 `scheduler.cron` 保持一致。Docker 部署时由 config.json 的 cron 控制。

1. 在仓库 **Settings → Secrets and variables → Actions** 中添加以下 Secrets（前缀 `MCTTK2RSS_`）：

   | Secret 名称 | 说明 |
   |---|---|
   | `MCTTK2RSS_AI_HOST` | AI 翻译 API 地址（如 `api.your-provider.com`） |
   | `MCTTK2RSS_AI_MODEL` | AI 模型名称（如 `gpt-4o`） |
   | `MCTTK2RSS_OPENAI_API_KEY` | AI 翻译 API 密钥 |

   Workflow 运行时会将这些 Secrets 注入到 config.json 中，不使用环境变量。

2. 在仓库 **Settings → Pages** 中配置部署来源：
   - Source 选择 **Deploy from a branch**
   - 分支选择 **gh-pages**
   - 目录选择 **/ (root)**
   - 点击 **Save**

   > Workflow 使用 `peaceiris/actions-gh-pages` 将内容推送到 `gh-pages` 分支，因此 Pages 必须设置为从 `gh-pages` 分支部署。

3. Workflow `.github/workflows/rss-publish.yml` 会自动：
   - 每 6 小时爬取最新新闻
   - 翻译并生成 RSS Feed
   - 将 `feed.xml` 部署到 `https://<你的用户名>.github.io/MCTTK2RSS/feed.xml`

4. 将该 RSS 地址添加到你的 RSS 阅读器即可

也可在 Actions 页面手动触发。

#### 手动运行 workflow

所有 workflow 均支持手动触发：

1. 进入仓库的 **Actions** 页面
2. 在左侧列表选择要运行的 workflow：
   - **Scrape & Publish RSS** — 爬取新闻并发布 RSS 到 GitHub Pages
   - **Docker Build & Push** — 构建并推送 Docker 镜像到 GHCR
3. 点击右侧 **Run workflow** 按钮
4. 选择分支（`main`）
5. 对于 **Scrape & Publish RSS**，可直接点击 **Run workflow** 运行完整流程（爬取 → 翻译 → 生成 RSS → 发布），无需勾选任何参数
   - 可选参数（通常不需要）：
     - `dry_run` — 仅检测新新闻，不实际处理
     - `rss_only` — 仅从已有 JSON 重新生成 RSS，不爬取
     - `skip_first_run_protection` — **首次使用时勾选**，跳过首次运行保护，立即处理所有新闻并发布 RSS
3. 点击绿色 **Run workflow** 确认运行

运行进度和日志在 Actions 页面对应运行记录中查看。

**状态持久化**：`.state.json` 会被保存到 gh-pages 分支，下次运行时自动恢复，避免重复处理。

### 方式二：Docker 部署

Docker 部署时所有配置从挂载的 `config.json` 读取，数据通过 volume 挂载写回宿主机。容器内置 HTTP 服务器，直接在 8080 端口提供 RSS Feed 访问。

```bash
# 1. 编辑 config.json 填入 API 配置和定时规则
# 2. 启动容器
docker-compose up -d
# 3. 访问 RSS Feed：http://localhost:8080/feed.xml
```

**配置文件挂载**：
- `config.json` 挂载为只读（修改后重启容器生效）
- `output/` 目录挂载为读写（容器写回 feed.xml 和文章数据）
- `logs/` 目录挂载为读写

#### 使用 GHCR 预构建镜像

`docker-compose.yml` 默认使用 GitHub Container Registry 上的预构建镜像 `ghcr.io/solathis/mcttk2rss:latest`，无需本地构建。

如需本地构建（如修改了源码），编辑 `docker-compose.yml` 将 `image` 行注释，取消注释 `build: .`。

#### 访问 RSS Feed

容器内置 HTTP 服务器（`server.py`），随 `scheduler.py` 一同启动，监听 `config.json` 中 `server.port` 指定的端口（默认 8080）：

```
http://localhost:8080/feed.xml
```

将该地址添加到 RSS 阅读器即可。如需外部访问，可修改 `docker-compose.yml` 中 `ports` 映射，或在宿主机上配置 Nginx/Caddy 反向代理。

**Docker 镜像构建**：`.github/workflows/docker-build.yml` 会在 push 到 main 时自动构建多架构镜像（linux/amd64 + linux/arm64）并推送到 GHCR，也可在 Actions 页面手动触发。

### 方式三：docker run 单命令启动

无需 docker-compose，直接使用 `docker run` 启动：

```bash
docker run -d \
  --name mcttk-rss \
  -p 8080:8080 \
  -v $(pwd)/config.json:/app/config.json:ro \
  -v $(pwd)/glossary.json:/app/glossary.json:ro \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  --restart unless-stopped \
  ghcr.io/solathis/mcttk2rss:latest
```

启动后访问 `http://localhost:8080/feed.xml`。参数说明：

| 参数 | 说明 |
|---|---|
| `-d` | 后台运行 |
| `-p 8080:8080` | 映射 HTTP 服务端口（左侧可改为宿主机任意端口） |
| `-v .../config.json:ro` | 挂载配置文件（只读） |
| `-v .../glossary.json:ro` | 挂载词汇表（只读） |
| `-v .../output` | 挂载输出目录（读写，存储 feed.xml 和文章数据） |
| `-v .../logs` | 挂载日志目录（读写） |
| `--restart unless-stopped` | 容器崩潢后自动重启 |

本地构建运行：

```bash
docker build -t mcttk2rss .
docker run -d --name mcttk-rss -p 8080:8080 \
  -v $(pwd)/config.json:/app/config.json:ro \
  -v $(pwd)/glossary.json:/app/glossary.json:ro \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  --restart unless-stopped \
  mcttk2rss
```

## 新闻来源

### Minecraft 官方 API

从 `https://net-secondary.web.minecraft-services.net/api/v1.0/zh-cn/search` 获取最新新闻，支持按类型过滤。

### Feedback 网站

从 `https://feedback.minecraft.net` 爬取更新日志，在 `config.json` 的 `feedback_site` 中配置各 section 的启用状态和文章数量。

## 新闻类型过滤

在 `config.json` 的 `news_types` 中控制只处理哪些类型的官方 API 新闻。

**默认配置（只爬 Java 端更新日志）**：

```json
{
  "news_types": {
    "java_release":   true,
    "java_snapshot":  true,
    "java_prerelease": true,
    "java_rc":        true,
    "bedrock_release": false,
    "bedrock_beta":   false
  }
}
```

Feedback 网站默认也只启用 Java 快照 section，基岩版 section 全部禁用。

| 关键词 | 类型 |
|---|---|
| `Snapshot` | `java_snapshot` |
| `Pre-Release` / `Prerelease` | `java_prerelease` |
| `Release Candidate` | `java_rc` |
| `Java Edition` / 版本号如 `1.21` | `java_release` |
| `Bedrock` | `bedrock_release` |
| `Beta` / `Preview` | `bedrock_beta` |

## 定时调度

定时规则在 `config.json` 的 `scheduler` 中配置：

```json
{
  "scheduler": {
    "cron": "0 */6 * * *",
    "interval_seconds": 21600,
    "timeout_seconds": 600
  }
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `cron` | cron 表达式（5 字段标准格式），scheduler.py 会解析 `*/N` 语法为执行间隔 | `0 */6 * * *` |
| `interval_seconds` | 执行间隔秒数（优先于 cron 解析结果） | `21600`（6 小时） |
| `timeout_seconds` | 单次 main.py 执行超时 | `600` |

- **Docker 部署**：scheduler.py 读取此配置控制调度
- **GitHub Actions**：cron 在 workflow yml 中配置（应与此处保持一致）

## 智能词汇表

编辑 `glossary.json` 添加或修改术语：

```json
{
  "terms": {
    "Snapshot": "快照",
    "Pre-Release": "预发布版",
    "Release Candidate": "候选版本",
    "Bedrock Edition": "基岩版",
    "Java Edition": "Java版"
  }
}
```

翻译时自动扫描文本，只将相关术语添加到提示词中，批量翻译时按批次检测。

## 智能去重机制

为避免网页结构问题导致的内容重复，系统采用三层去重：

1. **连续去重**：去除相邻的重复 block
2. **大段重复检测**：检测连续 5 个以上 block 的重复序列，移除整段重复内容
3. **长文本去重**：对超过 80 字符的长文本进行跟踪，15 个 block 内再次出现则认为是异常重复（列表项除外）

## 处理流程

```
Minecraft 官方 API          Feedback 网站
       ↓                          ↓
  获取新闻列表              获取各 section 文章列表
       ↓                          ↓
  按类型过滤 (news_types)    按 section.enabled 过滤
       └──────────┬───────────────┘
                  ↓
         检查已处理状态 (.state.json)
                  ↓
         [对每篇新文章]
                  ↓
         解析文章页面 → 提取结构化 blocks
                  ↓
         AI 翻译标题 + 内容（并发批量）
                  ↓
         保存 JSON (output/news_*.json)
                  ↓
         下载头图 (output/news_*.jpg)
                  ↓
         生成 RSS Feed (output/feed.xml)
```

## 首次运行保护

通过 `config.json` 的 `first_run_protection` 字段控制：

| 值 | 行为 |
|---|---|
| `true`（默认） | 首次运行时将当前所有新闻标记为已处理，下次运行才开始处理新新闻，避免刷屏 |
| `false` | 取消首次运行保护，首次运行即处理所有爬取到的新闻 |

首次运行状态记录在 `output/.state.json` 的 `_first_run` 字段中，首次执行后自动移除。

## 上游同步

本仓库是 [jiubook/MCTTK](https://github.com/jiubook/MCTTK) 的 fork，支持持续接收上游爬取逻辑更新而不影响自定义功能。

详见 [UPSTREAM_SYNC.md](UPSTREAM_SYNC.md)。

核心策略：通过 `.gitattributes` 的 `merge=ours` 策略保护自定义文件（`main.py`、`rss_generator.py`、`scheduler.py`、`config.json` 等），上游的 `scraper.py`、`glossary.json` 等可正常合并更新。

**注意**：`scraper.py` 的 `load_config()` 已被我们修改（移除环境变量逻辑），上游合并恢复该逻辑时需手动删除。

## 配置参考

完整的 `config.json` 结构：

```json
{
  "openai_compat": {
    "host": "api.example.com",
    "endpoint": "/v1/chat/completions",
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
  "feedback_site": {
    "enabled": true,
    "base_url": "https://feedback.minecraft.net",
    "timeout": 30,
    "sections": []
  },
  "http": {
    "verify_ssl": false,
    "user_agent": "Mozilla/5.0 ...",
    "proxies": { "http": "", "https": "" },
    "timeout": 120
  },
  "news_types": {
    "java_release": true,
    "java_snapshot": true,
    "java_prerelease": true,
    "java_rc": true,
    "bedrock_release": false,
    "bedrock_beta": false
  },
  "first_run_protection": true,
  "scheduler": {
    "cron": "0 */6 * * *",
    "interval_seconds": 21600,
    "timeout_seconds": 600
  },
  "server": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "rss": {
    "feed_title": "Minecraft News (中文翻译)",
    "feed_link": "",
    "feed_description": "Minecraft 官方新闻与更新日志的中文翻译 RSS",
    "output_path": "output/feed.xml",
    "max_items": 50
  },
  "output": {
    "save_dir": "output"
  },
  "retry": {
    "translation": { "max_retries": 3 },
    "download": { "max_retries": 3 }
  },
  "concurrency": {
    "translation_workers": 8,
    "batch_max_chars": 1000,
    "batch_max_items": 10
  }
}
```

## 注意事项

- **配置方式**：所有配置仅从 `config.json` 读取，不使用环境变量
- **JSON Schema 结构化输出**：`openai_compat.json_schema`（默认 `true`），开启后翻译 blocks 时通过 `response_format` 的 `json_schema` 强制 AI 返回标准 JSON，避免翻译结果因格式问题被丢弃。设为 `false` 时退回纯文本模式解析（兼容不支持结构化输出的 API）
- **首次运行保护**：通过 `config.json` 的 `first_run_protection` 字段控制（默认 `true`）。设为 `false` 时首次运行即处理所有新闻
- **默认爬取范围**：默认只爬取 Java 端更新日志（Java 正式版/快照/预发布/RC + Feedback Snapshot section），基岩版默认禁用
- **输出文件**：文件名自动处理非法字符，同名文件自动加序号避免冲突
- **状态重置**：删除 `output/.state.json` 后会重新处理所有新闻
- **磁盘管理**：`output/` 目录下的文件不会被自动清理，需手动管理

## 许可证

本工具以 [GPL-3.0](https://www.gnu.org/licenses/gpl-3.0.zh-cn.html) 协议发布。

AI 翻译作品以 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.zh-hans) 协议发布。
