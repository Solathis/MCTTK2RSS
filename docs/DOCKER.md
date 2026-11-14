# Docker 部署说明

## 两种 Dockerfile 的区别

| 文件 | 代码来源 | 适用场景 |
|---|---|---|
| `Dockerfile` | `COPY` 本地文件 | 本地开发调试、打包当前工作目录 |
| `Dockerfile.git` | 构建时从 GitHub `docker` 分支 `git clone` | 服务器部署，无需上传代码 |

两者运行的都是 `scheduler.py`（默认每小时执行一次 `main.py`），功能完全相同。

---

## 方式一：本地构建（Dockerfile）

适合本地开发，或需要将当前代码（含未提交修改）打包部署。

### 使用 docker-compose（推荐）

1. 配置环境变量：

```bash
cp .env.sample .env
# 编辑 .env，填入以下内容：
# OPENAI_API_KEY=你的API密钥
# MCBBS_USERNAME=你的用户名
# MCBBS_PASSWORD=你的密码
```

2. 若还没有本地配置，先 `cp config/config.sample.json config/config.json`（`config/config.json` 不入库），
   确认其中的 API 配置正确，然后启动：

```bash
docker-compose up -d
```

### 手动 docker build

```bash
docker build -t mcttk .
docker run -d \
  --name mcttk-scraper \
  --restart unless-stopped \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/glossary:/app/glossary:ro \
  -e OPENAI_API_KEY=你的密钥 \
  -e MCBBS_USERNAME=用户名 \
  -e MCBBS_PASSWORD=密码 \
  mcttk
```

---

## 方式二：远程拉取构建（Dockerfile.git）

适合在服务器上部署，只需要这一个文件，构建时自动从 GitHub 拉取代码。

```bash
docker build -f Dockerfile.git -t mcttk .
docker run -d \
  --name mcttk-scraper \
  --restart unless-stopped \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/glossary:/app/glossary:ro \
  -e OPENAI_API_KEY=你的密钥 \
  -e MCBBS_USERNAME=用户名 \
  -e MCBBS_PASSWORD=密码 \
  mcttk
```

> 注意：`Dockerfile.git` 拉取的是 `docker` 分支的代码，与 `main` 分支可能存在差异。

---

## 方式三：直接拉取官方发布镜像（推荐）

每推送一个版本 tag，GitHub Actions 会自动构建镜像推到 GHCR（`ghcr.io/jiubook/mcttk`），
服务器上既不用构建、也不用上传代码。

> 本仓库是 public，GHCR 上的包也会继承为 public，`docker pull` 不需要登录（已实测匿名可拉）。
> 万一哪天拉取提示 `unauthorized`：要么先 `docker login ghcr.io`，要么到仓库首页 →
> Packages → 该镜像 → Package settings → Change visibility 改成 Public。
>
> 现成版本看 [Releases](https://github.com/jiubook/MCTTK/releases)。

### 拉取并运行

```bash
docker pull ghcr.io/jiubook/mcttk:1.0.0

docker run -d \
  --name mcttk-scraper \
  --restart unless-stopped \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/glossary:/app/glossary:ro \
  -e OPENAI_API_KEY=你的密钥 \
  -e MCBBS_USERNAME=用户名 \
  -e MCBBS_PASSWORD=密码 \
  ghcr.io/jiubook/mcttk:1.0.0
```

### 使用 docker-compose（推荐）

仓库里的 `docker-compose.release.yml` 已经去掉 `.py` 挂载，代码全部来自镜像：

```bash
# 指定版本部署
MCTTK_VERSION=1.0.0 docker compose -f docker-compose.release.yml up -d

# 跟随 latest
docker compose -f docker-compose.release.yml up -d
```

升级版本：改 `MCTTK_VERSION` 后重新 `up -d` 即可（必要时先 `docker compose pull`）。

可用标签：

| 标签 | 含义 |
|---|---|
| `1.0.0` | 精确版本，生产环境建议用这个 |
| `1.0` | 跟随 `1.0.x` 里的最新一次发布 |
| `latest` | 最新正式版 |

预发布版本（如 `v1.0.0-rc1`）只产出它自己的精确标签，**不会**覆盖 `1.0` 和 `latest`。

> 国内拉取 `ghcr.io` 可能很慢或不通，这种情况直接用下面的离线包方案。

---

## 方式四：离线 tar 包部署

适合无法访问 Docker Hub、GHCR 或 GitHub 的内网环境。

**最省事的方式：直接用 Release 里的离线包**——每次发版都由 CI 自动生成并附上校验和，不用自己构建：

```bash
# 把 1.0.0 换成要的版本，文件名为 mcttk-v<版本>.tar.gz
wget https://github.com/jiubook/MCTTK/releases/download/v1.0.0/mcttk-v1.0.0.tar.gz
wget https://github.com/jiubook/MCTTK/releases/download/v1.0.0/mcttk-v1.0.0.tar.gz.sha256
sha256sum -c mcttk-v1.0.0.tar.gz.sha256
```

下载完直接跳到下面的「传输到目标机器」。后续小节是自己从源码构建并导出的做法。

### 自行导出镜像

在有网络的机器上构建并导出：

```bash
docker build -t mcttk .

# 以下四选一
docker save -o mcttk_v1.tar mcttk:latest  # 导出时同时指定版本标签

docker save -o mcttk.tar mcttk            # 保存为 mcttk.tar

docker save mcttk > mcttk.tar             # 等价写法，用重定向

docker save mcttk | gzip > mcttk.tar.gz   # 保存为 mcttk.tar.gz
```

### 传输到目标机器

```bash
# Release 离线包
scp mcttk-v1.0.0.tar.gz user@server:/path/to/deploy/
# 或自行导出的包
scp mcttk.tar user@server:/path/to/deploy/
```

### 在目标机器上加载并运行

```bash
# Release 下载的包是 .tar.gz
gunzip -c mcttk-v1.0.0.tar.gz | docker load

# 自行导出的包
docker load < mcttk.tar
```

加载后的镜像名取决于包的来源：

| 包来源 | 加载后的镜像名 |
|---|---|
| Release 离线包 | `ghcr.io/jiubook/mcttk:1.0.0` |
| 自行导出 | `mcttk`（即 `docker build -t` 时给的名字） |

```bash
docker run -d \
  --name mcttk-scraper \
  --restart unless-stopped \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/config:/app/config:ro \
  -v $(pwd)/glossary:/app/glossary:ro \
  -e OPENAI_API_KEY=你的密钥 \
  -e MCBBS_USERNAME=用户名 \
  -e MCBBS_PASSWORD=密码 \
  ghcr.io/jiubook/mcttk:1.0.0
```

> 用 Release 离线包时，把最后一行换成上表对应的镜像名即可；`docker-compose.release.yml`
> 里写的就是 `image: ghcr.io/jiubook/mcttk:${MCTTK_VERSION:-latest}`，`docker load` 之后
> 指定同样的 `MCTTK_VERSION` 就能直接 compose 启动。

> 离线部署时，`config/`（`config.json`、`modules_config.json`）与 `glossary/glossary.json`
> 仍需手动放到目标机器上，通过 `-v` 挂载进容器。
> 自动词表同样手动生成后再挂载，见下一节。

---

## 自动词表：手动生成、挂载使用

`glossary/glossary.auto.json`（词条）和 `glossary/glossary.auto.index.json`（溯源索引）由
`tools/sync_glossary.py` 生成，默认取 [misode/mcmeta](https://github.com/misode/mcmeta)
`assets` 分支里对应**最新快照**的游戏语言文件（每次快照当天更新）；
`--source cfpa` 可改用 [CFPAOrg 官方术语表](https://github.com/CFPAOrg/Glossary)（以正式版为基准，较旧）。

**程序和容器在任何时候都不会联网更新它**：镜像构建不生成，`scheduler.py` 每小时起的
`main.py` 只读取挂载进来的文件。网络不通只会影响你手动执行同步的那一次。

首次部署前，在宿主机执行一次：

```bash
python tools/sync_glossary.py
# 数据源: https://github.com/misode/mcmeta (26.4-snapshot-8)
# 输出：glossary/glossary.auto.json（约 100 KB）+ glossary/glossary.auto.index.json（约 150 KB）
```

`glossary/` 整个目录以只读方式挂载进容器（`docker-compose.yml` 与上面的 `docker run` 都已包含）。
之后想更新词表，重新执行上面的命令即可 —— **不用重建镜像、也不用重启容器**：
调度器每次都是拉新进程跑 `main.py`，下一次运行就会读到新文件。

没有这两个文件也能正常跑：程序会退回只用 `glossary/glossary.json`，并在日志里提示一次
（`[词汇表] 未找到 glossary.auto.json，仅使用手写词表`）。
因为挂载的是整个 `glossary/` 目录，不会再出现"Docker 把缺失的挂载文件建成同名空目录"的老问题。

---

## 发布新版本（维护者）

打一个 `v主.次.修订` 形式的 tag 并推送，`.github/workflows/release.yml` 会自动跑完整条链路：

```bash
git tag v1.0.0
git push origin v1.0.0
```

流水线做的事：

1. **测试门禁** — 复用 `.github/workflows/ci.yml`（Ruff + pytest），不通过就不发布
2. **构建推送镜像** — `ghcr.io/jiubook/mcttk`，标签 `1.0.0` / `1.0` / `latest`（预发布不覆盖 `latest`）
3. **生成离线包** — 从 GHCR 拉回镜像 `docker save` 成 `mcttk-v1.0.0.tar.gz` 并附 `.sha256`
4. **建 Release** — 自动生成变更说明，挂上离线包

补充说明：

- 只认 `v1.0.0`、`v1.0.0-rc1` 这类写法；`v1.0` 这种不完整版本号不会触发布
- 发布失败想在同一个 tag 上重来，直接在 Actions 页面点 **Re-run all jobs**；
  要换 tag 内容就 `git push --delete origin v1.0.0` 后重新打
- `pyproject.toml` 的 `version` 与 tag 不一致时只在日志里给一条 warning，不会中断发布
  （镜像版本始终以 tag 为准），但建议顺手同步
- 镜像目前只出 `linux/amd64`；需要 ARM 的话在 `release.yml` 的 `platforms` 里加 `linux/arm64`

---

## 管理命令

```bash
# 查看实时日志（Docker 输出）
docker-compose logs -f
# 或
docker logs -f mcttk-scraper

# 查看持久化日志文件（按日期命名）
ls logs/
cat logs/mcttk_2026-07-22.log

# 停止服务
docker-compose down

# 重启服务
docker-compose restart

# 进入容器调试
docker exec -it mcttk-scraper bash

# 手动触发一次运行（不等待定时器）
docker exec mcttk-scraper python main.py

# 查看容器资源占用
docker stats mcttk-scraper
```

---

## 日志与排查

`logs/` 目录挂载在宿主机上，每轮运行都会写 `logs/mcttk_YYYY-MM-DD.log`，
内容比 `docker logs` 完整得多（`print()` 与 stderr 也会落盘）：

- 每个来源（官方 API / Java 版本清单 / Feedback）拿到多少条、去重与类型过滤结果
- 每篇文章的解析块数、标题原文与译文、翻译成功/失败块数、字符数、耗时
- 保存的 JSON / 头图 / BBCode / Markdown 的路径与大小
- 发帖是否成功、**帖子地址**、失败原因（附异常堆栈）
- 每轮结束的一行汇总：来源条数 / 新增 / 翻译篇数 / 发帖成功失败 / 总用时

常用排查命令：

```bash
# 最近几轮的汇总（有没有新内容、发没发出去）
grep "\[汇总\]" logs/mcttk_*.log | tail -5

# 确认官方 API 是"确实没新内容"还是"请求失败"
grep -E "来源 minecraft_api|获取新闻列表失败" logs/mcttk_*.log | tail -10

# 发帖结果与帖子地址
grep -E "发帖成功|发帖失败|帖子地址" logs/mcttk_*.log

# 所有错误（翻译失败、登录失败、未捕获异常）
grep "\[ERROR\]" logs/mcttk_*.log

# 跟随当天日志
tail -f "logs/mcttk_$(date +%F).log"
```

日志与调度相关环境变量（写进 `.env` 即可，`docker-compose` 已透传）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MCTTK_LOG_LEVEL` | `INFO` | 改成 `DEBUG` 可看到请求头、逐条新闻列表、分批明细 |
| `MCTTK_LOG_KEEP_DAYS` | `90` | 日志保留天数，`0` 表示永久保留；启动时清理超期文件 |
| `MCTTK_LOG_CONSOLE` | `1` | 是否同时输出到 `docker logs` |
| `MCTTK_LOG_CAPTURE` | `1` | 是否把 `print()` / stderr 一并写进日志文件 |
| `MCTTK_INTERVAL` | `3600` | 调度间隔秒数（1 小时） |
| `MCTTK_RUN_TIMEOUT` | `3600` | 单轮 `main.py` 超时秒数（1 小时），超时记 ERROR 后进入下一轮 |

> 如果发现单轮耗时明显偏长（比如一轮跑掉几十分钟），把 `MCTTK_LOG_LEVEL=DEBUG` 打开跑一轮，
> 日志里能看到每个请求的耗时与重试痕迹，用来判断是卡在 Feedback API、头图下载还是翻译调用。

---

## 工作机制

- 容器启动后由 `scheduler.py` 按 `MCTTK_INTERVAL`（默认 3600 秒 = 1 小时）自动运行 `main.py`
- `output/` 目录通过 volume 挂载到宿主机，数据持久化
- `logs/` 目录通过 volume 挂载到宿主机，日志持久化（每天一个文件，默认保留 90 天）
- `config/` 与 `glossary/` 以只读方式挂载，修改宿主机文件后重启容器即可生效
- 自动词表（`glossary/glossary.auto.json` / `glossary/glossary.auto.index.json`）以只读方式挂载，
  **构建和运行期都不会联网更新**；宿主机重新跑 `tools/sync_glossary.py` 后，下一次调度即生效
- 内存限制 512MB，预留 256MB；每次运行后自动垃圾回收
- `restart: unless-stopped`：容器异常退出或系统重启后自动拉起

## 注意事项

- 首次运行会自动将当前所有新闻标记为已处理，下次运行才开始处理真正的新新闻
- 状态文件 `output/.state.json` 和 `output/.posted.json` 持久化保存在宿主机
- 删除 `output/.state.json` 会导致重新处理所有新闻，谨慎操作
- 环境变量优先级高于 `config.json`，敏感信息建议通过环境变量传入
- 日志文件保存在 `logs/` 目录，格式为 `mcttk_YYYY-MM-DD.log`，默认保留 90 天
  （`MCTTK_LOG_KEEP_DAYS` 可调，`0` 表示永久保留）
- 自动词表缺失不影响运行，只是译名覆盖范围退回到手写 `glossary/glossary.json`
