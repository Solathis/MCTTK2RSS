# 使用手册

> README 只留最短上手路径。这里是完整用法：安装、全部配置项、CLI 参数、日志、
> 新闻来源、词汇表与注意事项。贡献与提交流程见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

**目录**：[快速开始](#快速开始) · [日志](#日志) · [新闻来源](#新闻来源) ·
[新闻类型过滤](#新闻类型过滤) · [模块系统](#模块系统) · [智能词汇表](#智能词汇表) ·
[智能去重机制](#智能去重机制) · [配置参考](#配置参考) · [注意事项](#注意事项)

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

帖子高亮默认开启（`mcbbs.highlight`）。站点对新主题一律进审核，审核中的帖子在
Discuz 里设置不了高亮，所以发帖时只登记一条待补记录：每轮运行先匿名探测帖子页
（404 = 还没过审），确认过审后再登录设置高亮/加粗。色值按版规「二、高亮设定」，
不在 Discuz 色板内的色值会被跳过（记录里会写原因）。`commentary`（时评）按版规
一般不高亮，默认不配颜色即可：

```json
{
  "mcbbs": {
    "highlight": {
      "enabled": true,
      "colors": { "java_release": "#EE1B2E", "java_snapshot": "#3C9D40" },
      "bold_types": [
        "java_release", "java_snapshot", "java_prerelease",
        "java_rc", "bedrock_release", "bedrock_beta"
      ],
      "exclude_forums": ["块讯回收区"],
      "max_attempts": 6,
      "max_age_hours": 168,
      "max_age_days": 30
    }
  }
}
```

`colors` 按新闻类型给色值，缺省（或没有这个类型）就不高亮；`bold_types` 列出的类型
会加粗，默认是全部「版本资讯」（Java 版资讯 / 基岩版资讯两个分类），块讯不加粗；
`exclude_forums` 是永不回填高亮的版块，写名字（按"包含"匹配）或 `fid`（按精确匹配）；
`max_attempts` / `max_age_hours` 是补高亮的放弃条件（**等审核**超时就放弃，不再每轮探测，
计时从登记/提升那一刻算，不是发帖时间）；`max_age_days` 是「多久以前的帖子根本不用管」，
见下面的记录生命周期。`0` 在三个上限里都表示「不限」。

**记录生命周期（`max_age_days`，默认 30 天）**：新闻有时效，一个月前的帖子再补高亮也没有
意义，所以程序不会为它们做任何事：

- 过期判断以**帖子自己的发帖日期**为准（版块列表每行都带），取不到才退回产物的
  `release_date` —— 旧文章被重新发一遍时帖子是新的，照样要上色；
- `--index-history` 跳过过期帖子，不建记录；已经登记过、后来老掉的记录一并清掉
  （标题带删除线的帖子连记录一起作废）；
- 补高亮遇到过期的 `pending` 直接判 `gave_up`，**连探测都不发**；等审核超过
  `max_age_hours` 的同样先判放弃，不再每轮为一个死帖花一次请求；
- 每次落盘前清理：窗口外的 `legacy` / `done` / `gave_up` / `dropped` 删掉。
  **`pending`、加粗中的、以及站点上仍显示为加粗的记录永不删除** —— 待办不能丢，
  加粗的要留着才能随时取消加粗；
- 记录里没有时间戳的老记录会补一次 `registered_at`，从补上那一刻开始算年龄；
- 这一轮什么都没改就不重写文件（每小时一轮，避免无意义的写盘）。

所以 `output/.mcbbs_threads.json` 只留「待办 + 最近一个月 + 所有加粗帖」，不会无限增长。
实测把装了 14 个月的历史账本从 208 条 / 106 KB 收到 44 条 / 25 KB。

**加粗按「分类 + 版本线」收敛，一共 4 个「最新」**：版规是「最新版本加粗」，而 Discuz
的高亮没有「只对最新版生效」的机制。而且「最新」不是一个：Java 版资讯和基岩版资讯各算各的，
每个分类里正式版和测试版也各算各的，所以同时有 4 篇加粗帖才是对的（两红两绿）：

| 分组 | 加粗的那篇 | 颜色 | 现在的例子 |
| --- | --- | --- | --- |
| Java版资讯／正式版 | 最新的正式版 | `#EE1B2E` | `Minecraft Java版 26.3` |
| Java版资讯／测试版 | 最新的快照/预发布/RC | `#3C9D40` | `Minecraft 26.4 快照 9` |
| 基岩版资讯／正式版 | 最新的更新日志/热修复 | `#EE1B2E` | `Minecraft: 基岩版 26.62 热修复更新日志` |
| 基岩版资讯／测试版 | 最新的预览版/测试版 | `#3C9D40` | `Minecraft 预览版 26.70.26 更新日志` |

所以每给一篇新版本帖加粗后，程序会读版块列表（那是唯一能看到高亮样式的地方）把**同一分组里
其它仍然加粗的帖子**改回不加粗 —— 判断依据是站点上真实的 `font-weight: bold`，不是本地记录，
这样历史帖、手工加过粗的帖子也能一起收敛；跨分组的不动（给 Java 测试版加粗不会碰 Java 正式版）。

收敛是**整张列表一次算完再动手**（不是逐篇「谁刚加粗谁就是赢家」）：否则同一批里后处理的
旧版会把先处理的新版取消掉，最后留下的反而是旧版。补加粗失败时整组保持原样，不会把一条版本线
弄成「一篇都不加粗」；认不出颜色的帖子只保留原状，不会用兜底色顶替。

「这篇属于正式版还是测试版」优先看记录的 `news_type`，站点列表里没有记录的帖子按标题认
（快照/预览/测试/候选/预发布/prerelease/preview/beta/snapshot 算测试版，Java版/基岩版 xx
更新日志、热修复算正式版）。

历史遗留或人工改过的加粗用 `--reconcile-bold` 整体对一次账（可以反复跑）：

```bash
python poster.py --reconcile-bold --dry-run   # 只列出 4 组各自的最新和要做的动作
python poster.py --reconcile-bold             # 真的改论坛
```

历史登记（`--index-history`）会直接跳过两类帖子：

- **标题带删除线**：站点对回收站帖子用 `class="recy"` 加删除线，说明它已删除、
  是测试稿、或者人工翻译后撤掉了 AI 翻译版；
- **落在 `exclude_forums` 里的版块**：例如块讯回收区。

补高亮阶段还会用帖子页面的面包屑复核一次版块（帖子过审后被移动/删除的情况），
命中排除名单的直接标记为 `dropped`，不会再去提交高亮。

**怎么验证高亮是否真的生效**：高亮样式只渲染在版块列表里，所以直接对照列表看：

```bash
# 记录 vs 站点，逐条打「一致/不一致」（只读，不登录；读不到版块列表时退出码非 0）
python poster.py --highlight-status

# 单篇试色 + 试加粗：先 --dry-run 看色号，再真跑，然后重新跑一次 --highlight-status
# 只处理指定的这一篇，不会顺手提交队列里别的 pending
python poster.py --highlight-stem news_xxx --bold --dry-run
python poster.py --highlight-stem news_xxx --bold
python poster.py --highlight-status
```

输出形如 `一致 | tid=6808 | 记录 #3C9D40 加粗 | 站点 #3C9D40 加粗 | 标题`；
不一致就是没生效或被人改过。也可以直接打开版块列表看标题链接的
`style="font-weight: bold;color: #3C9D40;"`。

默认色值对应幻翼块讯版规「二、高亮设定」：

| 新闻类型 | 版规分类 | 色值 |
| --- | --- | --- |
| `java_release` / `bedrock_release` | 版本资讯（正式版） | `#EE1B2E` |
| `java_snapshot` / `java_prerelease` / `java_rc` / `bedrock_beta` | 版本资讯（快照/测试版） | `#3C9D40` |
| `normal`（兜底，块讯） | 块讯 / 基岩块讯 | `#EC1282` |
| `commentary` | 时评（一般不高亮） | 不配颜色 |

加粗只发生在版本资讯（`bold_types` 默认全量），且每个分组里只保留最新一篇加粗
（见上面的 4 个「最新」）。

高亮状态记在 `output/.mcbbs_threads.json`（`state` 取 `pending` / `done` / `gave_up` /
`dropped` / `legacy`），与「发没发」的 `output/.posted.json` 分开：

- 发帖成功只写一条 `pending`，帖子 ID 拿不到时靠「我的帖子」按标题补齐
- 每轮运行先游客探测帖子页：仍在审核不算重试，超过 `max_attempts`/`max_age_hours` 才放弃；
  超过 `max_age_days` 的连探测都不发，直接判放弃
- 只有确实过审了才会登录，避免多跑验证码
- 历史帖用 `poster.py --index-history` 只登记成 `legacy`（不动论坛），
  颜色与是否加粗取自版块列表的现状，再用 `--highlight-legacy N` 限量挑最近的几篇真正上色
- 窗口外的旧记录在落盘前自动清理，`pending` 与加粗中的记录永久保留

如果需要发布到 B站专栏，先用扫码工具拿登录 cookie（不用手动开 F12 复制）：

```bash
uv sync --extra login            # 首次：装二维码渲染依赖（qrcode）
uv run python tools/bili_login.py
```

终端会打印二维码，用 B站 App 扫码确认后，`SESSDATA` / `bili_jct` 会写进
`config/bili_cookies.json`（已 gitignore，不入库；也可改用环境变量）。重复运行会覆盖旧值，
cookie 过期（投稿报 `-101` / `-111`）时重跑一次即可。然后在 `config.json` 里打开：

```json
{
  "bilibili": {
    "enabled": true,
    "renderer": "opus",
    "submit": "draft",
    "publish_types": ["java_snapshot", "java_release", "bedrock_release"],
    "exclude_sources": ["feedback"]
  }
}
```

`renderer` 选正文模型：`"html"`（默认，老接口收 HTML 字符串，只存草稿/提交审核两态）或
`"opus"`（新版编辑器的段模型，能写封面、摘要与「AI 辅助创作」声明）。`submit: "draft"`
只存草稿，需要你到创作中心手动发布；确认排版与标题没问题后再改成 `"publish"` 自动发布
（opus 模式下会先存草稿再按草稿号发布，发布失败草稿仍在）。

**凭据来源**（按优先级）：环境变量 `BILI_SESSDATA` / `BILI_JCT` → `bilibili.cookie_file`
（默认 `config/bili_cookies.json`）。环境变量是空串时按「没设置」处理、回落到 cookie 文件，
所以 Docker Compose 里留空 `BILI_SESSDATA=` 不会把文件里的值覆盖掉。两处都没有 cookie 时
专栏整段跳过（日志一条 WARNING，不计入失败），其余流程照常跑。

**首次启用会先做基线**：`output/.posted_bili.json` 不存在（或内容为空）时，第一次运行把
`output` 里已有的 `news_*.json` 全部记为「已投稿」，**一篇都不投**，之后只投新翻译的文章。
升级到带专栏的版本时不用手动清理历史。要补投历史：删掉（或清空成 `[]`）
`output/.posted_bili.json`，再用 `--post-only --publish bilibili --bili-backfill` 跑
（`--bili-backfill` 就是「这次明确要补投，别做基线」的开关，同样受 `max_per_run` 限制，
一次投不完就多跑几次）。

**投放范围**（`publish_types` / `exclude_sources`）在投稿前按文章元数据过滤：

- `publish_types`：允许投专栏的文章类型，取值同抓取阶段的分类
  （`java_snapshot` / `java_prerelease` / `java_rc` / `java_release` / `bedrock_beta` / `bedrock_release`）。
  模板默认是**版本发布类全放**，于是官方 API 的「快讯」（类型落到 `other`）和时评不会进专栏。
- `exclude_sources`：不投专栏的来源，默认排除 `feedback`（论坛式的更新日志搬运）。
  目前能区分的是 `minecraft_api` 与 `feedback`：版本清单（Piston manifest）补出来的文章
  抓取阶段同样记成 `minecraft_api`，要按版本类型排除请用 `publish_types`。
- 两项**留空或删掉即不过滤**（老配置行为不变）；被跳过的文章会在日志里逐条写明原因。

`bilibili.max_per_run`（默认 3）对全流程和 `--post-only` 都生效：单次运行最多投这么多篇，
剩下的下次运行再投（全流程只投当批新文章，补投用 `--post-only`）。

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
# 全流程：爬取 → 翻译 → 转换 → 发布（各平台是否发布由 enabled 决定）
python main.py

# 仅检测新新闻（不实际处理）
python main.py --dry-run

# 只爬取翻译，不发布
python main.py --scrape-only

# 只发布 output 目录中已翻译但未发布的文件
python main.py --post-only
# （全流程默认也会补发，见 retry.backfill_unposted；这个开关关掉后才需要手动跑）

# 只投 B站专栏，不碰 MCBBS
python main.py --publish bilibili

# 把 B站专栏里积压的历史文章补投出去（跳过首次基线，配合 --post-only）
python main.py --post-only --publish bilibili --bili-backfill

# 只投 MCBBS / 完全不发布
python main.py --publish mcbbs
python main.py --publish none

# 发布时跳过图片上传 / JSON 附件（只对 MCBBS 生效）
python main.py --no-image
python main.py --no-json

# 只补帖子高亮（审核已通过的帖子），不发帖
python poster.py --highlight-only

# 把「我的帖子」里对得上 output 产物的历史帖登记为 legacy（只写本地记录，不动论坛）
# 删除线帖子、落在 exclude_forums 里的版块会被跳过；加 --dry-run 只打印不写文件
python poster.py --index-history --dry-run
python poster.py --index-history

# 把最近 3 条历史登记提升为待高亮并执行（会真的改论坛，先用 --dry-run 看清单）
python poster.py --highlight-legacy 3 --dry-run
python poster.py --highlight-legacy 3

# 对照站点现有颜色/加粗检查本地记录（只读，不改论坛）
python poster.py --highlight-status

# 加粗整体对账：4 个分组（Java/基岩 × 正式版/测试版）各只留最新一篇加粗
python poster.py --reconcile-bold --dry-run
python poster.py --reconcile-bold

# 单篇验证：只补这一篇，可强制样式；先 --dry-run 看要提交的色号与加粗位
python poster.py --highlight-stem news_xxx --bold --dry-run
python poster.py --highlight-stem news_xxx --bold
python poster.py --highlight-stem news_xxx --no-bold

# 不依赖 output 产物，直接指定一篇帖子（色值必须给，取 Discuz 色板）
# 不加 --bold/--no-bold 时沿用站上现状，不会把人工加粗的帖子取消加粗
python poster.py --highlight-tid 6816 --color "#EC1282" --dry-run
python poster.py --highlight-tid 6816 --color "#EC1282"
python poster.py --highlight-tid 6772 --color "#2B65B7" --bold
```

## 日志

日志按天写入 `logs/mcttk_YYYY-MM-DD.log`，同时打印到控制台（容器里就是 `docker logs`）。
除 `logging` 记录的消息外，`print()` 与标准错误也会同步进同一份文件（标记为
`[STDOUT]` / `[STDERR]`），所以容器 stdout 里出现过的内容不会只留在 `docker logs` 里。

每次运行都会记录下面这些内容：

| 阶段 | 记录内容 |
|---|---|
| 运行 | 模式、输出目录、状态文件、翻译模型、启用类型、Feedback 栏目、MCBBS 开关、B站专栏开关（含 cookie 是否就位）、日志文件路径 |
| 采集 | 每个来源（官方 API / Java 版本清单 / Feedback）拿到多少条、去重与类型过滤结果、耗时 |
| 状态 | 候选 / 已处理 / 本次待处理条数，首次运行的标记情况 |
| 解析 | 文章标题、内容块数量与类型分布、发布日期、耗时 |
| 翻译 | 标题原文与译文、正文可译块数、成功/失败块数、原文字符 → 译文字符、批次数、每次 API 调用的耗时与 token 用量 |
| 保存 | JSON / 头图 / BBCode / Markdown 的完整路径与文件大小 |
| 发布 | 登录结果、每篇发帖结果、**帖子地址**、耗时；失败时附异常堆栈 |
| 发布排查 | 登录页/验证码页解析失败时记 `[登录排查]`，并把响应写进日志目录 `mcbbs_login_*.html`（`auth` 令牌与密码框已打码） |
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
- `platform`：模块适用的发布平台，`"all"`（两边共用）/ `"mcbbs"` / `"bilibili"`。**缺省按 `"mcbbs"` 处理**，避免 MCBBS 版面的表格模板漏到专栏

每个模块支持 `content`（BBCode）、`bbcode_content`、`markdown_content` 字段，转换时自动选择对应格式。

### 平台差异

- 当前共用（`"platform": "all"`）：开头署名、各类型开头说明、结尾开源协议。
- MCBBS 独占：结尾的「实用链接 / 如何游玩 / 想了解更多资讯」等模块，里面全是 MCBBS 站内链接。
- 专栏独占：`module_bili_footer`（外部来源与更新条目追踪），B站 读者不关心 MCBBS 版块，所以单独写了一份官方站点链接。
  该模块**已默认开启**：超链接降级成 `文字（URL）` 之后，这组来源在正文里既看得到名字也看得到地址
  （原先因为 B站 剥链接、只剩名字而暂时关掉，见 [`archive/2026-11-15-bilibili-column.md`](archive/2026-11-15-bilibili-column.md) §9.2、§9.7）。
  内容里因此不再写 `[url]`，直接用「名称：地址」的纯文本，两种正文模型渲染出来一致。

渲染专栏正文时，模块里的 BBCode 会被降级：粗体、列表保留（`[b]`→`<strong>`、`[list]`→`<ul>`），
链接只有项目署名还保留成 `<a>` / link 节点，其余一律降级成 `文字（URL）`；
表格/彩色/浮动/图标和 `[size=0px]` 隐藏文本会被丢弃——专栏没有这些等价物，留着只会显示成脏标签或
把「本帖来自www.mcbbs.co」这类隐藏文字曝光出来。

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
    "verify_ssl": true,
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
    "sortid_map": {},
    "highlight": {
      "enabled": true,
      "colors": { "java_release": "#EE1B2E", "normal": "#EC1282" },
      "bold_types": ["java_release", "bedrock_release"],
      "exclude_forums": ["块讯回收区"],
      "max_attempts": 6,
      "max_age_hours": 168,
      "max_age_days": 30
    }
  },
  "bilibili": {
    "enabled": false,
    "cookie_file": "config/bili_cookies.json",
    "category_id": 26,
    "candidate_categories": [26, 4, 15, 6],
    "tags": ["Minecraft", "我的世界", "更新日志"],
    "original": false,
    "reprint": true,
    "renderer": "html",
    "template_id": 1,
    "ai_declare": true,
    "publish_types": [
      "java_snapshot",
      "java_prerelease",
      "java_rc",
      "java_release",
      "bedrock_beta",
      "bedrock_release"
    ],
    "exclude_sources": ["feedback"],
    "submit": "draft",
    "title_suffix": " [AI翻译]",
    "max_title_chars": 80,
    "summary_max_chars": 100,
    "attach_cover": true,
    "rehost_images": true,
    "max_inline_images": 20,
    "ai_notice": "",
    "interval_seconds": 20,
    "max_per_run": 3
  },
  "output": {
    "save_dir": "output"
  },
  "retry": {
    "translation": { "max_retries": 3 },
    "download": { "max_retries": 3 },
    "login": { "max_attempts": 3, "wait_seconds": 10 },
    "backfill_unposted": true,
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
- **B站专栏**：`bilibili.enabled` 默认关闭。凭据是浏览器登录后的 cookie（`SESSDATA` + `bili_jct`），
  放 `.env` 或 `config/bili_cookies.json`（两者都不入库），日志里不会打印。`submit: "draft"` 只存草稿、
  需人工在编辑器发布，改成 `"publish"` 才会自动提交审核（提交后进入审核队列，通过前链接打不开）。
  正文图片会先重托管到 B站 CDN——残留外链会被整篇拒绝，所以上传失败的图会被直接丢弃
- **专栏正文模型**：`bilibili.renderer` 决定用哪套正文。`"html"`（默认）是老接口的 HTML 正文
  + cv 专栏草稿；`"opus"` 是新版编辑器的段模型，能写封面、摘要与「AI 辅助创作」声明
  （`bilibili.ai_declare`），发布走 `POST /x/dynamic/feed/create/opus`。两种模型的接口都已在
  真机验证过，字段与踩过的坑见
  [`archive/2026-11-15-bilibili-column.md`](archive/2026-11-15-bilibili-column.md) §9.5、§9.6、§10。
  想先看排版再决定：`uv run python tools/preview_bili_html.py --renderer opus`。
  注意 opus 模型没有标签字段（`bilibili.tags` 只在 html 模型下生效），分区与封面在发布时会
  被编辑器界面上的值覆盖——自动投稿不打开编辑器就不会有这个问题
- **专栏投放范围**：`bilibili.publish_types`（默认版本发布类）与 `bilibili.exclude_sources`
  （默认排除 `feedback`）在投稿前过滤，官方 API 的快讯、时评与论坛式更新日志不会进专栏。
  两项留空即不过滤，被跳过的文章会在日志里逐条说明原因
- **专栏超链接**：只保留项目署名 `github.com/jiubook/MCTTK`，其余外链降级为纯文本
  `文字（URL）`（B站 点外链会先进「离开 B站」拦截页，实测比不给链接更糟）。
  改策略见 `bili/__init__.py` 的 `SIGNATURE_HOST` / `SIGNATURE_PATH` 与 `degrade_link()`
- **验证码**：登录时若遇到验证码，优先从这次失败的登录响应里取验证码表单（站点要求验证码时，
  那张页面本身就是带验证码的登录页），取不到再重取登录页；识别仍用 `ddddocr`，单次流程最多试 5 次，
  识别失败时用 `captcha_answer` 备用答案。验证码页解析不出来时会把响应写进日志目录
  （`mcbbs_login_*.html`，`auth` 令牌与密码框已打码）并在日志里记 `[登录排查]`，便于对照站点改版
- **登录重试**：整段登录失败（验证码页结构不符、风控页、网络抖动）时换一个新会话重试，
  默认 3 次、间隔 10 秒，由 `retry.login.max_attempts` / `retry.login.wait_seconds` 控制。
  凭证被站点明确拒绝（密码错误、安全问题）不重试，避免把失败计数刷上去又触发验证码
- **输出文件**：文件名自动处理非法字符，同名文件自动加序号避免冲突
- **失败重试**：处理失败的文章**不会**被标记为已处理，下次运行会自动重试；
  连续失败达到 `retry.max_article_attempts`（默认 3）次后才放弃，避免个别坏链接被反复重试
- **失败补发**：登录失败会让整轮 MCBBS 发布跳过，文章留在 `output/` 里但没进
  `.posted.json`。`retry.backfill_unposted`（默认 `true`）让**下一次运行**把这些「已生成但
  未发布」的文章一起发掉，每篇在日志里单独打一条 `[发布] 补发: <文件名>`；没有待补发文章时
  不会多跑一轮登录。想只让全流程发新文章就设为 `false`，改完用 `--post-only` 手动补
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
  `curl: (60) unable to get local issuer certificate`。正确做法是用 `http.ca_bundle`
  指向中间人软件的 CA 证书（`.pem`），校验照旧保留；`http.verify_ssl: false` 只是最后手段，
  它同时关掉 `requests` 与 `curl_cffi` 两边的校验 —— 而翻译请求带着 bearer API key，
  关掉校验等于把密钥和文章正文交给中间人。日志会给出这两种处理建议
- **配置模板里的占位符**：`openai_compat.host` 默认是 `api.example.com`、`model` 是
  `gpt-4o`，`main.py` 见到 `example` 会当成「翻译 API 未配置」直接退出（`--post-only`
  除外）。复制模板后必须把 host / model 换成自己的服务商
- **Feedback 被 Cloudflare 拦截**：页面与 RSS 会返回挑战页（`403` + `cf-mitigated: challenge`），
  这是站点的机器人防护，硬碰没有意义。抓取默认走 Zendesk Help Center 公开 API
  （见上文 [Feedback 网站](#feedback-网站)）：列表接口一次带回标题、链接与正文，
  失败时才回退页面解析。若机房 IP 连 API 都被挑战，请降低频率或改用代理，而不是加大重试
- **配置文件不入库**：仓库只保留 `config/config.sample.json` 模板，`config/config.json` 已被 `.gitignore` 忽略；
  首次使用请复制模板。API Key、MCBBS 密码等敏感信息建议放在 `.env`（同样被忽略）
- **磁盘管理**：`output/` 目录下的文件不会被自动清理，需手动管理
- **日志**：每轮运行的来源条数、翻译量、保存的文件、发帖地址都会写进 `logs/mcttk_YYYY-MM-DD.log`
  （`print()` 与 stderr 也一并落盘）。容器重建后 `docker logs` 会清空，排查历史问题请以该目录为准
- **状态重置**：删除 `output/.state.json` 后会重新处理所有新闻；删除 `output/.posted.json`
  后会重新发布所有已翻译文件（`retry.backfill_unposted` 开着时下一次全流程就会补发）；删除
  `output/.posted_bili.json` 后会先把现有文章重新记为「已投稿」做基线（一篇都不投），
  要真补投就用 `--post-only --bili-backfill`。删除 `output/.mcbbs_threads.json` 只是丢掉
  高亮的追踪记录（已发出去的帖子不受影响），但发帖后还没补上高亮的帖子就找不回来了
