# B站专栏自动发布 — 调研与决策记录（2026-11-15）

本文件是「给 MCTTK 增加 B站专栏发帖能力」的调研结论与决策留痕，供后续实现与 PR review 对照。
**代码尚未改动**；本文只记录事实、取舍和待实测项。

---

## 1. 结论

可行，但它和 MCBBS 发帖**不是同一类问题**，不能复用 `poster.py`：

1. 专栏正文只吃**富文本 HTML**（`<p>/<h1>/<strong>/<figure><img>`），BBCode 的
   `[attachimg]/[quote]/[color]/[table]` 没有等价物；
2. 正文图片**必须是 B站 CDN 地址**，留着外链会被整篇拒绝（社区实现反馈 `code 37130`）；
3. 认证是**登录 cookie**（`SESSDATA` + `bili_jct`），不是账号密码；
4. 投稿成功后进入**审核队列**（`state=-2`），URL 在审核通过前 404。

因此需要新增：HTML 渲染器 + 独立发帖模块 + 编排器多平台化。估算 3~4 个工作日，**不需要新增硬依赖**。

---

## 2. 现状链路（改动前）

| 环节 | 位置 | 现状 |
| --- | --- | --- |
| 抓取→翻译→落盘 | `main.run_scrape` → `_process_single_article` | 产 `.json` + `.txt`(BBCode) + `.md`(Markdown)，返回 `(stem, bbcode_path, json_path)` |
| 转换 | `converter.convert_json_file` | 返回 `(bbcode_path, md_path)`，被上面解包后再传给 `run_post` |
| 发布 | `main.run_post` / `run_post_only` | 只认 `config["mcbbs"]["enabled"]`，`result` 形状被 `tests/test_logging.py` 精确断言 |
| 发帖 | `poster.MCBBSPoster.post_news_file` | 读 `.txt` + `.json` + 同名图片，上传附件后发帖 |
| 状态 | `.state.json`（URL 级）、`.posted.json`（stem 级） | 单平台语义 |

改动边界（最小改动原则）：**不改** `convert_json_file` 返回签名、**不改** `run_post` 返回形状、
**不改** `.posted.json` 语义（B站另开状态文件）。

---

## 3. 三条路线

| 路线 | 做法 | 前提 | 工作量 | 主要风险 | 结论 |
| --- | --- | --- | --- | --- | --- |
| **A. 创作中心 web API + cookie** | `api.bilibili.com/x/article/creative/*`，草稿→提交两步；图片走 `upcover` | 自己的 B站账号 cookie | 3~4 天 | `412` 风控、cookie 过期、`37130` 外链图、审核不通过 | **采用** |
| B. 官方开放平台 | `member.bilibili.com/arcopen/fn/article/add`，HMAC-SHA256 签名 + OAuth access_token | 开发者入驻 + 应用 + 权限白名单 | A 的 2~3 倍 | 个人项目基本拿不到白名单 | 备选，不实现 |
| C. 浏览器自动化 | Playwright/Selenium 驱动编辑器 | Chromium 二进制 | 中 | 镜像暴涨、选择器频繁失效 | 否决 |

---

## 4. 接口事实

### 4.1 已核实（社区可工作实现 + 官方开放平台文档交叉印证）

| 步骤 | 请求 | 关键字段 / 返回 |
| --- | --- | --- |
| 登录校验 | `GET https://api.bilibili.com/x/web-interface/nav` | `data.isLogin`、`data.mid`、`data.uname` |
| 存草稿 | `POST https://api.bilibili.com/x/article/creative/draft/addupdate` | form：`title, content(HTML), csrf, tid, category, list_id=0, reprint, original, media_id=0, spoiler=0, save=0, pgc_id=0`；返回 `data.aid`（草稿 id） |
| 上传图片 | `POST https://api.bilibili.com/x/article/creative/article/upcover` | multipart：`binary`(文件) + `csrf`；返回 `data.url`（B站 CDN） |
| 提交投稿 | `POST https://api.bilibili.com/x/article/creative/article/submit` | 同草稿字段 + `aid`；返回 `data.aid`（**cv id，与草稿 aid 不同**）+ `data.state` |

要点：
- 写操作必须带 `csrf`，值等于 `bili_jct` cookie；请求头需 `Referer/Origin: https://member.bilibili.com/`。
- `category` 才是真正决定分区的字段，`tid` 必须与之一致，否则会静默落到别的分区。
- 提交返回的 `aid` 才是 cv 号；拿草稿 aid 拼 `read/cv<aid>` 是 404 的常见原因。

### 4.2 `state` 语义

| state | 含义 | 处理 |
| --- | --- | --- |
| `0` / `1` | 已发布 | 记成功 |
| `-2` | 待审核 | **既不是成功也不是失败**：记入已发（防重投），日志写「已投稿·待审核」 |
| `-1` | 未通过（审核拒绝） | 记失败 |
| `-3` / `-4` | 锁定 / 已删除 | 记失败 |

### 4.3 已知错误码 / 现象

| 现象 | 含义 | 处理策略 |
| --- | --- | --- |
| HTTP `412` | 风控拦截（submit 常见，草稿往往已存成功） | **不重试**，保留草稿并给出编辑器链接 |
| `-111` | csrf 校验失败 = cookie 失效 | 提示更新 `SESSDATA`/`bili_jct` |
| `-17` | 该账号无权投稿到该分区 | 按候选分区表回退 |
| `37106` | 草稿数达上限（999） | 报错并提示手动清理草稿 |
| `37130` | 正文残留外链图片，整篇被拒 | 上传失败的图**丢弃**，绝不保留外链 |

> `37130` / `37106` / `-17` 的语义来自社区实现的经验记录，需在实测中确认；确认前代码按「保守处理 + 明确日志」实现。

### 4.4 专栏 HTML 白名单（据社区分析）

| 富文本 | HTML | 备注 |
| --- | --- | --- |
| 标题 | `<h1>` | **只支持一级标题** |
| 段落 | `<p>` | |
| 粗体 / 斜体 | `<strong>` / `<em>` | |
| 引用 | `<blockquote><p>` | 可嵌套 |
| 列表 | `<ul>/<ol><li>` | |
| 图片 | `<figure><img src>` | 必须是站内 CDN |
| 链接 | `<a>` | **仅站内链接**，外链可能被过滤（待实测） |
| 字号 / 颜色 / 对齐 | `<span class="font-size-x">` 等 | 编辑器样式类，内联 `style` 会被剥离 |
| 分割线 | `<figure><img>` | 专栏没有真正的 `<hr>` |

---

## 5. 内容映射（MCTTK blocks → 专栏 HTML）

| 来源 | 输出 | 说明 |
| --- | --- | --- |
| `p` | `<p>译文</p>` + `<blockquote><p>原文</p></blockquote>` | 双语策略，见 §6 |
| `h1`,`h2` | `<h1>` | 专栏只有一级标题 |
| `h3`,`h4` | `<p><strong>` | 避免满屏大标题 |
| `li`（含嵌套） | `<li>` 嵌套在 `<ul>` 内 | |
| `blockquote/quote` | `<blockquote><p>` | 原文另起一个引用块 |
| `pre/code` | `<pre><code>` | 白名单待实测，退路 `<p><code>` |
| `img` | `<figure><img src="B站URL">` | 先重托管 |
| 内联 `[b]/[i]/[url]` | `<strong>/<em>/<a>` | `[size]/[color]/[align]/[font]` 丢弃 |
| `[hr]` / `---` | 丢弃 | 专栏无真分割线 |
| 模块（`modules_config.json`） | 纯文本段落 | 模块是表格+彩色排版，专栏无等价物，先降级为文字 |

所有文本先 `html.escape`：抓回来的内容是**数据**，不转义会把页面结构写坏。

图片重托管流程（发帖阶段）：
`blocks[].meta.src` → 只允许 `https` 且公网地址、不跟随重定向（SSRF 防护）→ 按 `config.http`
的 `proxies/verify_ssl/timeout` 下载 → webp 用 Pillow 转 PNG（未安装 Pillow 则跳过该图）→
`upcover` → 替换 URL。单图失败丢图不丢文，逐张记日志。

---

## 6. 决策记录（2026-11-15 维护者拍板）

| 决策 | 选择 | 理由 |
| --- | --- | --- |
| 发布方式 | **配置项**，`submit: "draft" \| "publish"`，默认 `draft` | 风控与审核都不可控，草稿留了人工兜底；要全自动时改一行配置 |
| 正文双语 | **译文 + 原文放引用块** | 保留现有「双语对照」产品特征；专栏没有灰色小字样式，引用块是最接近的视觉降级 |
| 实施顺序 | 分阶段，先「调研归档 + HTML 渲染器」（离线可验收） | 渲染器是纯函数，可先评审；网络与发帖部分等实测后再动 |

其余默认值（后续实现时写入配置模板）：分区 `26 数码`（候选 `[26, 4, 15, 6]`）、
篇间隔 20 秒、单次最多 3 篇、`original=true`、`reprint=true`、`summary` 取译文首 100 字。

---

## 7. 待实测清单（需维护者用自己账号执行）

用一篇**已存草稿**即可验证，全部在 `member.bilibili.com` 编辑器里肉眼确认：

1. `<pre><code>` 是否被保留（退路：降级 `<p><code>`）。
2. `<a href>` 外链是否被过滤/降级为纯文本（退路：外链输出成「文字（URL）」）。
3. 标题与正文的字数上限（决定截断策略）。
4. `category_id` 权限：目标分区是否可投（`-17`）。
5. `submit` 的 `412` 概率与触发频率（决定是否需要更长间隔 / 只发草稿）。

> 这 5 项确认前，代码按「保守实现 + 明确日志」落地，不猜测白名单行为。

---

## 8. 安全红线

- `SESSDATA` 等价于账号密码：只放本地 `config/bili_cookies.json`（`.gitignore`）或
  `BILI_SESSDATA` / `BILI_JCT` 环境变量；日志只打 `abcd***`；绝不入库、绝不进 PR。
- 真实投稿只在维护者明确授权、且在草稿确认排版后执行；自动化验证一律走 fake session 或 `draft`。

---

## 9. 参考

- 专栏表单字段与富文本转换规则：<https://zihengcat.github.io/2018/04/19/bilibili%e4%b8%93%e6%a0%8fAPI%e5%88%86%e6%9E%90/>
- 草稿接口实测（含完整 form）：<https://segmentfault.com/a/1190000023095047>
- 官方开放平台「文章提交」：<https://bilibili.apifox.cn/api-23767554>
- 官方开放平台「专栏稿件图片上传」：<https://bilibili.apifox.cn/api-23906916>
- 同类可工作实现（含外链图片重托管、state 处理）：<https://cdn.jsdelivr.net/npm/@acedatacloud/skills@2026.1019.283001/skills/bilibili/SKILL.md>
