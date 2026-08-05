# 上游同步策略

## 背景

本仓库 (MCTTK2RSS) 是 [jiubook/MCTTK](https://github.com/jiubook/MCTTK) 的 fork。

**我们的自定义改动**：
- 移除了 BBCode/Markdown 转换功能（`converter.py` 调用）
- 移除了 MCBBS 论坛自动发布功能（`poster.py` 调用）
- 新增了 RSS Feed 生成功能（`rss_generator.py`）
- 新增了通过 GitHub Actions 定时发布 RSS 到 GitHub Pages 的 workflow
- 修改了 Docker 部署配置

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

### 5. 替代方案：cherry-pick 上游的 scraper.py

如果只想更新爬取逻辑，不合并全部：

```bash
git fetch upstream
git checkout upstream/main -- scraper.py glossary.json utils.py log_setup.py
git commit -m "Update scraper, glossary, utils, log_setup from upstream"
git push origin main
```

## .gitattributes 配置

| 文件 | 策略 | 原因 |
|------|------|------|
| `main.py` | `merge=ours` | 编排流程已修改为 RSS，上游版本不兼容 |
| `rss_generator.py` | `merge=ours` | 我们的新文件，上游不存在 |
| `config.json` | `merge=ours` | RSS 配置是自定义的 |
| `docker-compose.yml` | `merge=ours` | 移除了 MCBBS 环境变量 |
| `Dockerfile` | `merge=ours` | 容器配置可能不同 |
| `.env.sample` | `merge=ours` | 移除了 MCBBS 变量 |
| `.github/workflows/rss-publish.yml` | `merge=ours` | 我们的 CI workflow |
| `scraper.py` | 正常合并 | 爬取逻辑由上游维护 |
| `glossary.json` | 正常合并 | 词汇表由上游维护 |
| `utils.py` | 正常合并 | 工具函数由上游维护 |
| `log_setup.py` | 正常合并 | 日志系统由上游维护 |

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
