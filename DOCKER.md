# Docker 部署说明

## 两种 Dockerfile 的区别

| 文件 | 代码来源 | 适用场景 |
|---|---|---|
| `Dockerfile` | `COPY` 本地文件 | 本地开发调试、打包当前工作目录 |
| `Dockerfile.git` | 构建时从 GitHub `docker` 分支 `git clone` | 服务器部署，无需上传代码 |

两者运行的都是 `scheduler.py`（每 10 分钟执行一次 `main.py`），功能完全相同。

---

## 方式一：本地构建（Dockerfile）

适合本地开发，或需要将当前代码（含未提交修改）打包部署。

### 使用 docker-compose（推荐）

1. 配置环境变量：

```bash
cp .env.example .env
# 编辑 .env，填入以下内容：
# OPENAI_API_KEY=你的API密钥
# MCBBS_USERNAME=你的用户名
# MCBBS_PASSWORD=你的密码
```

2. 确认 `config.json` 中的 API 配置正确，然后启动：

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
  -v $(pwd)/config.json:/app/config.json:ro \
  -v $(pwd)/modules_config.json:/app/modules_config.json:ro \
  -v $(pwd)/glossary.json:/app/glossary.json:ro \
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
  -v $(pwd)/config.json:/app/config.json:ro \
  -v $(pwd)/modules_config.json:/app/modules_config.json:ro \
  -v $(pwd)/glossary.json:/app/glossary.json:ro \
  -e OPENAI_API_KEY=你的密钥 \
  -e MCBBS_USERNAME=用户名 \
  -e MCBBS_PASSWORD=密码 \
  mcttk
```

> 注意：`Dockerfile.git` 拉取的是 `docker` 分支的代码，与 `main` 分支可能存在差异。

---

## 方式三：离线 tar 包部署

适合无法访问 Docker Hub 或 GitHub 的内网环境。

### 导出镜像

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
scp mcttk.tar user@server:/path/to/deploy/
```

### 在目标机器上加载并运行

```bash
docker load < mcttk.tar
docker run -d \
  --name mcttk-scraper \
  --restart unless-stopped \
  -v $(pwd)/output:/app/output \
  -v $(pwd)/logs:/app/logs \
  -v $(pwd)/config.json:/app/config.json:ro \
  -v $(pwd)/modules_config.json:/app/modules_config.json:ro \
  -v $(pwd)/glossary.json:/app/glossary.json:ro \
  -e OPENAI_API_KEY=你的密钥 \
  -e MCBBS_USERNAME=用户名 \
  -e MCBBS_PASSWORD=密码 \
  mcttk
```

> 离线部署时，`config.json`、`modules_config.json`、`glossary.json` 仍需手动放到目标机器上，通过 `-v` 挂载进容器。

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

## 工作机制

- 容器启动后由 `scheduler.py` 每 10 分钟自动运行一次 `main.py`
- `output/` 目录通过 volume 挂载到宿主机，数据持久化
- `logs/` 目录通过 volume 挂载到宿主机，日志持久化（按日期轮转，单文件最大 5MB，保留 5 个备份）
- `config.json` 等配置文件以只读方式挂载，修改宿主机文件后重启容器即可生效
- 内存限制 512MB，预留 256MB；每次运行后自动垃圾回收
- `restart: unless-stopped`：容器异常退出或系统重启后自动拉起

## 注意事项

- 首次运行会自动将当前所有新闻标记为已处理，下次运行才开始处理真正的新新闻
- 状态文件 `output/.state.json` 和 `output/.posted.json` 持久化保存在宿主机
- 删除 `output/.state.json` 会导致重新处理所有新闻，谨慎操作
- 环境变量优先级高于 `config.json`，敏感信息建议通过环境变量传入
- 日志文件保存在 `logs/` 目录，格式为 `mcttk_YYYY-MM-DD.log`，可配合 `logrotate` 等工具管理宿主机日志
