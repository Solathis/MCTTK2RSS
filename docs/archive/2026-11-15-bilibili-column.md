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

## 9. 实测发现（2026-11-15 真机投稿后）

真机发了一篇草稿（aid=374174，分区 26，7 张图重托管成功），暴露三件事。

### 9.1 `tid` 是卡片模板 id，不是分区

创作中心编辑器的真实 payload 是 `tid: store.template.id`、`category: store.category`。
我们此前把分区 id 同时塞进 `tid`，草稿的 `template_id` 会被写成非法值。
探针验证（用后即删）：

| 请求 | 读回 |
| --- | --- |
| `category=26` + `tid=1` | category 26 / template 1 |
| `category=4` + `tid=1` | category 4 / template 1 |

已修：`bili.poster.DEFAULT_TEMPLATE_ID = 1`，`category` 单独传。

### 9.2 B站 会去掉站外超链接

`module_bili_footer` 的四条来源链接在发布态只剩纯文本，因此该模块暂时 `enabled: false`
（内容保留，外链可用了再打开）。

### 9.3 AI 创作声明：只能走新版编辑器接口，暂不实现

- 声明字段是 `opus.attachments.is_aigc`（1=已声明），由新版编辑器（eva3）写入；
  老接口 `x/article/creative/draft/addupdate` 不接受该字段——实测 5 种写法
  （`is_aigc`/`opus_is_aigc`/`attachments`/`is_ai_generated`/`ai_declare`）全部返回 `47015`。
- 新版接口：`POST /x/dynamic/feed/article/draft/add`，需要 WBI 签名
  （`wts` + `w_rid`，mixin-key 由 `nav.wbi_img` 推出），body 为 `{"arg": {...}}`，
  正文是结构化 `opus.content.paragraphs`（`para_type` 1 普通/2 图/4 引用/6 列表/9 标题，
  `node_type` 1 文字/4 链接）。
- 关键约束：新接口**不认 HTML `content`**。实测传入 `content` + 空 `paragraphs` 时，
  正文被清空（content 长度 11 → 0），标题也要靠 `opus.title` 才留得住。
  也就是说老接口（HTML 正文）与新接口（段落正文）**互斥**，不能混着用。
- 编辑器保存会把 HTML 正文一一转成段落（我们那篇 132 个段落 = 54 段 + 58 引用 + 8 标题
  + 7 图 + 5 列表，与 HTML 标签数完全一致），可作为实现段落渲染器的对照样本。
- 编辑器保存还会**用界面上的分区覆盖草稿分区**（实测被写成 15），并且会覆盖 `is_aigc`
  （勾选后曾变回空）。所以「投完草稿再进编辑器点保存」会改动已有字段。

结论：要做自动声明，必须整体切到段落模型（还需先抓到发布/提交环节的新接口）。
本次不做，留作独立特性；当前投稿后 `is_aigc` 需要人工在编辑器勾选。

### 9.4 补抓包：发布走的是 `create/opus`，不是 `draft/add`

维护者用 HTTP Debugger 录了一次「新建图文并发布」（`member.bilibili.com/platform/upload-manager/opus`），
导出的 xlsx 里 42 条请求中只有一条是发布，它推翻了 §9.3 对上一步接口的猜测：

| 观察 | 值 |
| --- | --- |
| 发布请求 | `POST https://api.bilibili.com/x/dynamic/feed/create/opus` |
| 查询串 | `csrf`、`gaia_source=main_web`、`w_rid`、`wts`、`b_wet`、`x-bili-locale-json`、`x-bili-device-req-json`、`dm_img_list`/`dm_img_str`/`dm_cover_img_str`/`dm_img_inter` |
| 请求体 | `application/json`，90623 字节 |
| 响应 | 200，113 字节 |
| 同一会话里的相关请求 | `POST member.bilibili.com/x/material/up/check`（素材上传校验）、`GET x/polymer/web-dynamic/v1/opus/creationlist`（图文列表）、`POST x/v2/dynamic/sunflower/parse`（富文本解析） |

三点结论：

1. 新版图文/专栏是**一次发布**，这条链路里没有「存草稿」请求；老接口 `creative/draft/addupdate`
   才是「先草稿、人工放行」的形态。要不要用新接口，等于要不要放弃草稿环节。
2. `w_rid` + `wts` 确认存在（WBI 签名），另外还带了 `b_wet` 与 `dm_*` 一串风控/指纹参数；
   这些是浏览器端生成的，能否省略、用什么值替代，只有实测才知道。
3. 这次导出的 xlsx **只有摘要表**（Id/URL/方法/状态/大小/类型/耗时），没有请求体与响应体，
   所以 `arg` 里的字段名与写法仍未确认——`create/opus` 是「发布」而非「草稿」，猜错会
   在账号下留下一条公开的坏图文，所以实现前必须先拿到真实请求体。

---

## 10. 段模型渲染器（路线 A，2026-11-15 落地）

§9.3 那篇草稿在编辑器里存盘后，正文被转成了 `opus.content.paragraphs`。用只读的
`draft/view?aid=` 把它读回来，就是渲染器的对照组，字段与取值如下：

| 结构 | 实测取值 | 说明 |
| --- | --- | --- |
| `para_type` | `1` 普通 / `2` 图片 / `4` 引用 / `6` 列表 / `9` 标题 | 没观察到的类型不猜 |
| 段格式 | `format.indent = {}`、`format.heading_type = 1` | 只有一级标题 |
| 引用 / 列表分组 | `format.combine_hash` = `blockquote_<毫秒戳>_<随机数>` / `list_<毫秒戳>_<随机串>` | 相邻同值段落会被并成一个引用框 / 一个列表 |
| 列表层级 | `format.list_format = {level, order, theme: "dot"}` | `level` 从 1 起，`order` 是同层内序号 |
| 文字节点 | `{"node_type": 1, "word": {words, font_size: 17, style, font_level: "regular"}}` | 正文 17 / regular，标题 24 / xxLarge |
| 链接节点 | `{"node_type": 4, "link": {show_text, link, link_type: 16, style, font_level}}` | 不带 `font_size` |
| 图片 | `{"para_type": 2, "pic": {"pics": [{url, width, height, size, comment}]}}` | `size` 单位 KB，`comment` 是 alt |
| 段内换行 | `word.words` 里直接出现 `\n` | 编辑器把 `<br>` 存成换行，模块的多行粗体靠这个保住 |

`bili/opus.py` 的映射：

| blocks / 模块 | 段落 |
| --- | --- |
| `p` | 普通段（译文）+ 引用段（原文） |
| `h1`/`h2` | 标题段（译文）+ 引用段（原文） |
| `h3`/`h4` | 加粗普通段（译文）+ 引用段（原文） |
| `blockquote`/`quote` | 两个引用框（译文、原文各一个） |
| 连续 `li` | 列表段，`meta.indent_level + 1` 作 `level` |
| `img` | 图片段（外链 URL，重托管交给发帖阶段） |
| `pre`/`code` | 原样纯文本段（不解析链接） |
| 头尾模块（BBCode） | 先压成 Markdown，再按段落 / 列表 / 引用 / 加粗解析 |

三处取舍：

1. **列表项的双语放在同一段内换行**，不像正文那样另起引用框。列表项在段模型里只能是一段
   文字，中间插引用段会把一个列表劈成两截；BBCode 版的 `[*]译文\n原文` 本来就是同项两行。
2. **`combine_hash` 用自增编号**（`blockquote_1` / `list_2`）而不是编辑器那种时间戳 + 随机数：
   产物可复现，重跑同一篇不会产生无意义的 diff，测试也能直接断言分组。
3. **图片段只写 `url` + `comment`**：宽高与体积要下载图片才知道，而转换器不碰网络；缺的字段
   留给发帖阶段上传后补。

验收方式（离线）：`bili/opus.py` + `converter.convert_to_bili_opus()` 的单元测试，以及
`tools/preview_bili_html.py --renderer opus` 生成的预览页；与本仓库 HTML 产物的对照口径是
「同一篇的文字内容与段落边界一致」（`tests/test_bili_opus.py::TestRendererParity`）。

未做：发布环节的新接口还没有请求体样本。§9.3 原以为是 `draft/add`，§9.4 抓包显示实机
发布打的是 `POST /x/dynamic/feed/create/opus`（WBI 签名 + 90 KB JSON 正文），但导出文件
没有 body，`arg` 里（分区、标签、卡片、AI 声明）的键名与取值仍未确认。先把渲染器落地并与
`draft/view` 样本对齐，等拿到真实请求体再接接口，避免重演 §9.1 那种字段语义猜错。

---

## 11. 参考

- 专栏表单字段与富文本转换规则：<https://zihengcat.github.io/2018/04/19/bilibili%e4%b8%93%e6%a0%8fAPI%e5%88%86%e6%9E%90/>
- 草稿接口实测（含完整 form）：<https://segmentfault.com/a/1190000023095047>
- 官方开放平台「文章提交」：<https://bilibili.apifox.cn/api-23767554>
- 官方开放平台「专栏稿件图片上传」：<https://bilibili.apifox.cn/api-23906916>
- 同类可工作实现（含外链图片重托管、state 处理）：<https://cdn.jsdelivr.net/npm/@acedatacloud/skills@2026.1019.283001/skills/bilibili/SKILL.md>
