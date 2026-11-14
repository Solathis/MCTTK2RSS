# ruffcheck 分支变更汇总

**分支:** `ruffcheck` → `main`  
**提交数:** 5  
**变更规模:** 15 个文件，+1,591 行 / −550 行

---

## 一、概览

本分支以 **代码质量治理** 为核心目标，围绕以下三条主线展开：

1. **引入 Ruff 静态检查 + pytest 测试框架**，建立自动化质量基线
2. **消除全局可变状态**，将模块级常量改为懒加载 / 参数传递
3. **统一重复逻辑**，抽取公共工具函数，减少散落各模块的重复代码

---

## 二、新增文件

| 文件 | 说明 |
|---|---|
| `pyproject.toml` | 项目配置：Ruff 规则集（E/W/F/B/I/UP/S/SIM）、行宽 120、pytest 测试路径 |
| `uv.lock` | uv 依赖锁文件（锁定 ruff ≥0.15.9） |
| `utils.py` | 公共工具模块：`MODULE_TYPE_MAP`、`load_dotenv()`、`classify_article_type()` |
| `tests/test_config_json.py` | config.json 结构与字段验证（16 个测试用例） |
| `tests/test_converter.py` | converter.py 纯逻辑单元测试（~50 个用例，覆盖渲染器、类型检测、日期解析） |
| `tests/test_glossary_json.py` | glossary.json 结构与内容校验（14 个用例） |
| `tests/test_scraper.py` | scraper.py 纯逻辑单元测试（~60 个用例，覆盖合并、分块、术语匹配、配置加载） |
| `tests/test_utils.py` | utils.py 单元测试（6 个用例，覆盖 dotenv 加载行为） |

---

## 三、变更详情

### 3.1 Ruff 静态检查修复（全量 lint 修正）

涉及文件：`converter.py`、`main.py`、`poster.py`、`scraper.py`、`init_state.py`、`scheduler.py`

- **import 排序**：按 `stdlib → 第三方 → 本地` 顺序重排，消除 isort 告警
- **类型注解现代化**：`Optional[X]` → `X | None`，`List[Dict]` → `list[dict]`，`Dict` → `dict`（UP 规则）
- **`open(path, 'r')` 简化**：移除冗余的 `'r'` 参数，直接 `open(path)`（UP015）
- **异常处理细化**：
  - `except Exception` 添加 `# noqa: BLE001` 注释，明确放行意图
  - `IOError` → `OSError`（IOError 是 OSError 的别名，UP024）
  - 部分 bare `except` 改为 `contextlib.suppress()`（SIM105）
- **f-string 修正**：移除无变量插值的 f-string，如 `f"静态文本"` → `"静态文本"`（F541）
- **安全规则标记**：
  - `hashlib.sha1` 添加 `# noqa: S324`（SHA-1 用于内容去重，非安全用途）
  - pyproject.toml 忽略 `S101`（assert in tests）和 `S501`（verify=False 有意为之）
- **日志替代静默吞异常**：`except` 分支从 `pass` 改为 `logging.debug(..., exc_info=True)`，保留调试信息
- **删除冗余编码声明**：移除 `# -*- coding: utf-8 -*-`（Python 3 默认 UTF-8）

### 3.2 消除全局可变状态（`scraper.py` 为核心改动）

**问题**：模块加载时即执行 `load_config()` / `load_glossary()` 产生全局变量 `CFG`、`PROXIES`、`HEADERS_HTML`、`GLOSSARY`，导致：
- 测试无法注入 mock 配置
- `python -c "import scraper"` 会触发网络 / 文件 I/O
- 多处函数隐式依赖全局状态

**改动**：
- 移除模块级 `CFG`、`PROXIES`、`HEADERS_HTML`、`GLOSSARY` 常量
- 新增 `_get_cfg()`、`_get_glossary()` 懒加载函数，首次调用时才加载
- 新增 `_make_proxies(cfg)`、`_make_headers(cfg)` 工厂函数，按需构建
- 所有公开函数（`translate_text`、`get_latest_news_list`、`parse_article_page`、`download_header_image`、`save_article_json`、`process_article`、`translate_blocks` 等）签名新增 `config=None` / `glossary=None` 参数，支持外部注入
- `_parse_pattern()` 内部的 `import re as regex_module` 改为直接使用顶层 `import re`

### 3.3 提取公共工具函数 `utils.py`

**`MODULE_TYPE_MAP`** — 原先在 `converter.py` 和 `poster.py` 中各有一份完全相同的字典，现统一到 `utils.py`，两处改为 `from utils import MODULE_TYPE_MAP`。

**`classify_article_type()`** — 原先 `converter._detect_article_type()`、`scraper.classify_news_type()`、`poster.detect_module_type()` 各自维护一套分类逻辑（略有差异）。统一为：
```python
classify_article_type(title, *, chinese=False, commentary=False, fallback="other")
```
通过参数控制中文关键词检测、时评类型检测、默认回退值，三处调用点各自传入所需参数。

**`load_dotenv()`** — 原先 `main.py` 和 `init_state.py` 各有内联的 `.env` 加载代码（逐行解析），统一为 `load_dotenv(project_dir)` 函数。

### 3.4 `main.py` — 流程拆分重构

`run_scrape()` 原为 ~120 行的单体函数，拆分为三个职责清晰的子函数：

| 函数 | 职责 |
|---|---|
| `_fetch_all_news(config)` | 聚合 API + Feedback 两个来源的新闻 |
| `_filter_and_check_state(all_news, config, state_file)` | 类型过滤 + 状态管理 + 首次运行检测 |
| `_process_single_article(news, config, save_dir, modules_cfg)` | 单篇文章的完整处理链路（解析→翻译→保存→头图下载→格式转换） |

`run_scrape()` 本身变为编排器，可读性显著提升。

**头图下载职责迁移**：原先 `scraper.save_article_json()` 在保存 JSON 后顺带下载头图，现移至 `main._process_single_article()`，使 `save_article_json()` 成为纯 I/O 函数。

### 3.5 `poster.py` — 上传逻辑去重 + 验证码 OCR 拆分

**上传逻辑统一**：`upload_file()` 和 `upload_image()` 原有大段重复的上传重试逻辑，提取为私有方法：
```python
def _upload(self, file_path, upload_filename, mime_type, extra_data=None) -> str
```
两个公开方法简化为调用 `_upload()` 并传入差异参数。

**验证码登录拆分**：`_login_with_captcha()` 从 ~120 行缩减至 ~50 行，拆出：
- `_init_ocr()` — OCR 引擎初始化
- `_preprocess_captcha(img_bytes)` — 图片预处理（二值化 / 放大锐化）
- `_ocr_recognize(ocr, img_bytes)` — 单次 OCR 识别
- `_submit_captcha_login(...)` — 单次提交登录请求

### 3.6 `scheduler.py` — 超时保护 + 日志优化

- `subprocess.run()` 新增 `timeout=600` 参数，防止子进程无限挂起
- 新增 `subprocess.TimeoutExpired` 异常捕获，输出明确的超时提示
- 非零退出码时输出警告日志

### 3.7 `.gitignore` 更新

- 新增 `output/*.json` 忽略规则
- 将 `debug*.*`、`test*.*`、`glossary_*.json` 限制为根目录匹配
- 新增 `!tests/test_*.*` 反向规则，确保测试文件不被忽略
- 补充注释说明 `.posted.json` 会被跟踪、`.state.json` 不会被跟踪

---

## 四、破坏性变更

> **无**。所有改动均为内部重构，外部接口（CLI 参数、配置文件格式、输出文件格式）保持完全兼容。

---

## 五、测试

新增 **~140 个单元测试**，覆盖以下模块的纯逻辑函数：

- `converter.py`：BBCode / Markdown 渲染、日期解析、文章类型检测、J2MMConverter 核心流程
- `scraper.py`：deep_merge、空白符规范化、新闻分类、术语模式匹配、词汇表搜索、翻译分块、纯文本转换
- `utils.py`：dotenv 加载行为
- `config.json` / `glossary.json`：结构完整性校验

运行方式：
```bash
uv run pytest tests/ -v
```

---

## 六、升级指引

1. **开发依赖**：首次需 `uv sync` 安装 ruff + pytest
2. **无配置迁移**：`config.json`、`glossary.json` 格式未变，无需修改
3. **CI 建议**：在 CI 中添加 `uv run ruff check .` 和 `uv run pytest` 步骤
