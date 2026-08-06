# 上游同步策略

## 背景

本仓库 (MCTTK2RSS) 是 [jiubook/MCTTK](https://github.com/jiubook/MCTTK) 的 fork。

**我们的自定义改动**：
- 移除了 BBCode/Markdown 转换功能（`converter.py` 调用）
- 移除了 MCBBS 论坛自动发布功能（`poster.py` 调用）
- 新增了 RSS Feed 生成功能（`rss_generator.py`，用 feedgen 库）
- 新增了通过 GitHub Actions 定时发布 RSS 到 GitHub Pages 的 workflow
- 修改了 Docker 部署配置（内置 HTTP 服务器）
- 移除了环境变量配置，改为纯 config.json 驱动
- 新增 JSON Schema 结构化输出支持
- 新增 server.py（容器内置 HTTP 服务器）
- 新增 scheduler.py cron 调度
- **新增 Piston 版本清单发现**：通过 Mojang `version_manifest_v2.json` 获取最新 Java 版本更新日志，替代不可靠的搜索 API `sortType=Recent` 排序

**我们希望持续接收上游的更新**：
- `scraper.py`（包含本项目的配置驱动、Java-only 新闻过滤、Piston manifest 发现、API 候选窗口、翻译和 Feedback 节流逻辑；上游更新需人工检查后合并）
- `glossary.json`（专业术语词汇表）
- `utils.py`（公共工具，如 `classify_article_type`）
- `log_setup.py`（日志系统）

**保护原则**：`main.py`、`config.json`、`scraper.py` 和 workflow 都已标记为 `merge=ours`。普通 `git merge upstream/main` 不会覆盖这些文件；上游的 `scraper.py` 爬取更新需要通过 diff 人工挑选，避免覆盖 Java-only 过滤、Piston manifest 发现、pageSize 和配置驱动逻辑。

```
upstream/main (jiubook/MCTTK)      origin/main (Solathis/MCTTK2RSS)
       │                                     │
       │  git fetch upstream                  │
       │─────────────────────────────────────▶│
       │                                     │
       │  git merge upstream/main            │
       │  (自动保留我们的自定义文件)            │
       │                                     │
       │  git push origin main               │
       │─────────────────────────────────────▶│
```

## 使用方法

### 1. 首次设置（已完成）

```bash
# 添加上游远程
git remote add upstream https://github.com/jiubook/MCTTK.git

# 配置 merge=ours 策略（需要运行一次）
git config merge.ours.name "Always prefer our version"
git config merge.ours.driver "true"
```

### 2. 定期同步上游更新

```bash
# 拉取上游最新代码
git fetch upstream

# 合并上游 main 到本地 main
# .gitattributes 中标记了 merge=ours 的文件会自动保留我们的版本
git merge upstream/main -m "Merge upstream scraper updates"

# 解决剩余冲突（如果有的话）
# - 如果 scraper.py 有冲突，优先采纳上游版本（最新爬取逻辑）
# - 如果 main.py 有冲突，保留我们的版本

# 推送到我们的远程仓库
git push origin main
```

### 3. 只需查看上游爬取逻辑更新的场景

由于 `scraper.py` 包含本项目的 Java-only 过滤、API 候选窗口、配置驱动和 Feedback 节流逻辑，不能直接用上游版本覆盖。建议先查看差异：

```bash
git fetch upstream
git diff HEAD upstream/main -- scraper.py
```

确认某个上游修复与本项目逻辑兼容后，再手动移植对应代码，并运行完整测试。

由于 `main.py`、`config.json`、`rss-publish.yml` 等已标记 `merge=ours`，普通合并不会覆盖这些自定义文件。

### 4. 如果上游也修改了 main.py

万一上游修改了 `main.py` 但我们仍想保留自己的：合并时 `merge=ours` 会自动生效，代码不动。

如果想看看上游做了什么改动再决定：

```bash
git fetch upstream
git diff HEAD upstream/main -- main.py  # 查看差异
git merge upstream/main                  # merge=ours 自动保留我们的版本
```

### 6. 如果上游也修改了 load_config 或 translate_blocks

我们移除了 `scraper.py` `load_config()` 中读取环境变量的逻辑。上游可能恢复该逻辑。

合并后检查 `scraper.py` 的 `load_config` 函数，确保没有 `os.getenv` 或 `api_key_env` 相关代码。

如果上游恢复了环境变量逻辑，可手动删除：

```python
# 删除这段（如果上游恢复了它）：
env_var = config.get("openai_compat", {}).get("api_key_env", "OPENAI_API_KEY")
if env_var:
    env_key = os.getenv(env_var)
    if env_key:
        config["openai_compat"]["api_key"] = env_key
```

同样检查 `translate_blocks` 函数的解析逻辑：
- 应兼容 `translated_text` 和 `text` 两种字段名
- 应兼容 `{"translations": [...]}` 和直接 `[...]` 两种返回格式
- 应支持 `response_schema` 参数传入 `translate_text`

同时检查 `get_java_news_from_manifest` 函数是否存在：
- 应从 `version_manifest_v2.json` 获取版本列表
- 应拼接为 `https://www.minecraft.net/zh-hans/article/minecraft-{version_id}` URL
- 应使用 `_classify_version_type` 分类版本类型
- `main.py` 中应优先调用此函数获取 Java 更新（`version_manifest.enabled` 为 true 时）

如果上游覆盖了这些增强，需手动恢复。

### 5. 替代方案：cherry-pick 上游的 scraper.py

如果只想更新爬取逻辑，不合并全部：

```bash
git fetch upstream
git checkout upstream/main -- scraper.py glossary.json utils.py log_setup.py
git commit -m "Update scraper, glossary, utils, log_setup from upstream"
git push origin main
```

**注意**：cherry-pick `scraper.py` 后需检查 `load_config` 函数是否被上游恢复了环境变量逻辑，如果是则手动删除。

## .gitattributes 配置

| 文件 | 策略 | 原因 |
|------|------|------|
| `main.py` | `merge=ours` | 编排流程已修改为 RSS，上游版本不兼容 |
| `rss_generator.py` | `merge=ours` | 我们的新文件，上游不存在 |
| `scheduler.py` | `merge=ours` | 重写为从 config.json 读取 cron + 内置 HTTP |
| `server.py` | `merge=ours` | 我们的新文件，上游不存在 |
| `init_state.py` | `merge=ours` | 移除了 load_dotenv 调用 |
| `config.json` | `merge=ours` | RSS/scheduler/server 配置是自定义的 |
| `docker-compose.yml` | `merge=ours` | 移除了 MCBBS 环境变量 + 内置 nginx 替代 |
| `Dockerfile` | `merge=ours` | 添加了 curl_cffi 系统依赖 + EXPOSE 8080 |
| `Dockerfile.git` | `merge=ours` | 上游的独立 Dockerfile，不使用 |
| `.env.sample` | `merge=ours` | 移除了 MCBBS/API_KEY 变量 |
| `.github/workflows/rss-publish.yml` | `merge=ours` | 我们的 RSS 发布 workflow |
| `.github/workflows/docker-build.yml` | `merge=ours` | 我们的 Docker 构建 workflow |
| `UPSTREAM_SYNC.md` | `merge=ours` | 本文档 |
| `scraper.py` | `merge=ours` | 包含 Java-only 过滤、Piston manifest 发现、API 候选窗口、配置驱动、结构化翻译和 Feedback 节流逻辑；上游更新需人工移植 |
| `glossary.json` | 正常合并 | 词汇表由上游维护 |
| `utils.py` | 正常合并 | 工具函数由上游维护 |
| `log_setup.py` | 正常合并 | 日志系统由上游维护 |
| `converter.py` | 正常合并 | 上游模块，保留但不调用 |
| `poster.py` | 正常合并 | 上游模块，保留但不调用 |
| `modules_config.json` | 正常合并 | 上游模块配置，保留但不使用 |

## 故障排除

### `merge=ours` 不生效

`merge=ours` 策略需要 git 本地配置：

```bash
git config merge.ours.name "Always prefer our version"
git config merge.ours.driver "true"
```

只需在仓库中运行一次即可生效。

### 想强制采纳上游的某次修改

如果上游修改了某个标记为 `merge=ours` 的文件且你想采纳上游版本：

```bash
git merge upstream/main
git checkout --theirs main.py   # 临时采纳上游版本
# 修复后重新提交
```

### 合并冲突如何解决

1. `scraper.py`：普通合并保留我们的版本；先用 `git diff HEAD upstream/main -- scraper.py` 检查，再人工移植上游爬取修复
2. `main.py`：保留我们的版本（`git checkout --ours main.py`）
3. 其他文件冲突：根据 `merge=ours` 策略自动保留我们的版本
