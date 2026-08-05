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

**我们希望持续接收上游的更新**：
- `scraper.py`（爬取逻辑、翻译、Feedback 爬虫）
- `glossary.json`（专业术语词汇表）
- `utils.py`（公共工具，如 `classify_article_type`）
- `log_setup.py`（日志系统）

## 分支结构

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

### 3. 只需爬取逻辑更新的场景

如果上游仅更新了 `scraper.py`（最常见的情况），合并过程会非常顺利：

```bash
git fetch upstream
git merge upstream/main
git push origin main
```

由于 `main.py`、`rss_generator.py`、`config.json` 等已标记 `merge=ours`，上游对它们的任何更改都不会覆盖我们的版本。

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
| `scraper.py` | 正常合并 | 爬取逻辑由上游维护 |
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

1. `scraper.py` 冲突：优先采纳入上游版本（爬取逻辑更新）
2. `main.py` 冲突：保留我们的版本（`git checkout --ours main.py`）
3. 其他文件冲突：根据 `merge=ours` 策略自动保留我们的版本
