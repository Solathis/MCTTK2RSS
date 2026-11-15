#!/usr/bin/env python3
"""
bili_poster.py — B站专栏发帖模块（创作中心 web 接口 + 登录 cookie）

职责：把已生成的专栏 HTML 与文章元数据投稿到 B站——上传正文图片/封面、存草稿、
按配置决定是否立刻提交审核。只负责单篇投稿，批量节奏（间隔、每次上限）由 main.py 控制。

为什么不用官方开放平台：需要开发者入驻 + 应用审核 + OAuth 授权 + HMAC 签名头，
个人项目基本拿不到白名单；创作中心 web 接口就是专栏编辑器自己在用的那套。

凭据：SESSDATA（登录态）+ bili_jct（csrf）。只放本地 config/bili_cookies.json
（.gitignore 已忽略）或 BILI_SESSDATA / BILI_JCT 环境变量；日志里永不打印值。

已知约束（调研记录见 docs/archive/2026-11-15-bilibili-column.md）：
  - 正文图片必须是 B站 CDN 地址，残留外链会整篇被拒（37130）→ 上传失败就丢图；
  - 投稿成功会进入审核队列（state=-2），审核通过前 URL 打不开；
  - submit 常被风控拦成 HTTP 412，草稿一般已存好，此时不重试、交给人工。
"""

import ipaddress
import json
import os
import re
from urllib.parse import urlsplit

import requests

from log_setup import log_debug, log_info, log_warning
from utils import PROJECT_ROOT

# ── 接口常量 ─────────────────────────────────────────

API_BASE = "https://api.bilibili.com"
MEMBER_BASE = "https://member.bilibili.com"
EDIT_URL_TEMPLATE = MEMBER_BASE + "/article-text/home?aid={aid}"

NAV_URL = f"{API_BASE}/x/web-interface/nav"
DRAFT_URL = f"{API_BASE}/x/article/creative/draft/addupdate"
SUBMIT_URL = f"{API_BASE}/x/article/creative/article/submit"
UPCOVER_URL = f"{API_BASE}/x/article/creative/article/upcover"

# 已经是 B站站内的图，不需要（也不该）再上传
BILI_IMAGE_HOSTS = ("hdslb.com", "bilibili.com", "biliimg.com")
MAX_IMAGE_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT = 120

# 接口返回的 state（投稿管理里看到的状态），只有 0/1 是真的在线
STATE_TEXT = {0: "已发布", 1: "已发布", -1: "未通过", -2: "待审核", -3: "锁定", -4: "已删除"}

# 已知错误码 → 人话。未收录的码原样带 code 打出来，不吞。
ERROR_HINTS = {
    -101: "账号未登录，cookie 已失效",
    -111: "csrf 校验失败，bili_jct 与 SESSDATA 不是同一次登录",
    -400: "请求参数错误",
    -403: "没有权限",
    -404: "接口不存在",
    -17: "当前账号无权投稿到该分区",
    37106: "草稿数已达上限（999），请先清理草稿",
    37130: "正文里还有外链图片，整篇被拒绝",
}

DEFAULT_BILI_CONFIG = {
    "enabled": False,
    # cookie 文件（不入库）；留空则只用环境变量
    "cookie_file": "config/bili_cookies.json",
    # 分区：26=数码；账号无权投稿时会按 candidate_categories 顺序回退
    "category_id": 26,
    "candidate_categories": [26, 4, 15, 6],
    "tags": ["Minecraft", "我的世界", "更新日志"],
    # 译文不是原创，默认不勾原创声明；要标原创自己在配置里改
    "original": False,
    "reprint": True,
    # draft=只存草稿（人工在编辑器放行）| publish=存草稿后立即提交审核
    "submit": "draft",
    "title_suffix": " [AI翻译]",
    "max_title_chars": 80,
    "summary_max_chars": 100,
    "attach_cover": True,
    "rehost_images": True,
    "max_inline_images": 20,
    "ai_notice": "",
    "interval_seconds": 20,
    "max_per_run": 3,
}

_IMG_SRC_RE = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])(https?://[^"\']+)\2', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# 分区不可用时换一个候选分区重试；其它错误换分区也没用
_CATEGORY_HINTS = ("分类", "分区", "-17")


class BiliError(RuntimeError):
    """B站接口返回业务错误或响应不可解析。message 里保留原始 code，便于对照文档排查。"""


# ── 配置与凭据 ───────────────────────────────────────

def load_bili_config(config_path: str = None) -> dict:
    """从统一 config.json 读 bilibili 段，缺字段用默认值补齐。

    返回 (bili_config, http_config)：http 段用于会话的 timeout / 代理 / 证书，
    与其它模块保持同一口径，不在本模块硬编码。
    """
    if config_path is None:
        config_path = os.path.join(PROJECT_ROOT, "config", "config.json")

    bili = dict(DEFAULT_BILI_CONFIG)
    http_cfg = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                full = json.load(f)
            bili.update(full.get("bilibili") or {})
            http_cfg = full.get("http") or {}
        except (OSError, json.JSONDecodeError) as e:
            log_warning("[B站] 读取 bilibili 配置失败，使用默认值: %s", e)
    else:
        log_debug("[B站] 配置文件不存在，使用默认配置: %s", config_path)

    return bili, http_cfg


def load_cookies(bili_config: dict) -> dict:
    """取登录 cookie：环境变量优先于 cookie 文件。

    缺 SESSDATA 或 bili_jct 时不在这里报错——由 check_login 统一给可操作的提示，
    这样纯离线的单元测试不用构造假凭据。
    """
    cookies = {}

    cookie_file = bili_config.get("cookie_file") or ""
    if cookie_file:
        path = cookie_file if os.path.isabs(cookie_file) else os.path.join(PROJECT_ROOT, cookie_file)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    cookies.update({str(k): str(v) for k, v in loaded.items() if v})
            except (OSError, json.JSONDecodeError) as e:
                log_warning("[B站] cookie 文件读取失败: %s", e)
        else:
            log_debug("[B站] cookie 文件不存在: %s", path)

    for env_name, key in (("BILI_SESSDATA", "SESSDATA"), ("BILI_JCT", "bili_jct")):
        value = os.environ.get(env_name)
        if value:
            cookies[key] = value

    return cookies


# ── 小工具 ───────────────────────────────────────────

def _assert_public_url(url: str) -> None:
    """只允许 https 公网地址。

    正文图片 URL 来自抓取内容，是数据不是指令：不能让它把我们指到内网或明文地址。
    这里只挡明显的内网字面量与协议（域名解析交给 HTTP 层），避免单测触发 DNS。
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError(f"只允许 https 图片地址: {url[:80]}")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"图片地址缺少域名: {url[:80]}")
    if host == "localhost" or host.endswith(".local"):
        raise ValueError(f"拒绝内网图片地址: {url[:80]}")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise ValueError(f"拒绝内网图片地址: {url[:80]}")


def _is_bili_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in BILI_IMAGE_HOSTS)


def _sniff_image(data: bytes) -> tuple[str, str]:
    """按文件头判断真实格式。URL 后缀和 CDN 的 Content-Type 都可能骗人。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:2] == b"\xff\xd8":
        return "jpg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "jpg", "image/jpeg"


def _webp_to_png(data: bytes) -> bytes | None:
    """upcover 不收 webp；装了 Pillow（captcha 可选依赖）就转 PNG，没装返回 None。"""
    try:
        import io as _io

        from PIL import Image
    except ImportError:
        log_debug("[B站] 未安装 Pillow，webp 图片将跳过")
        return None
    try:
        with Image.open(_io.BytesIO(data)) as img:
            buf = _io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            return buf.getvalue()
    except Exception:  # noqa: BLE001 - 图片解码失败的方式太多，统一降级为「这张图跳过」
        log_debug("[B站] webp 转 PNG 失败", exc_info=True)
        return None


def _truncate_title(title: str, suffix: str, max_chars: int) -> str:
    """标题加后缀后按上限截断。后缀是有意义的标注，不能被截掉。"""
    limit = max(int(max_chars), len(suffix) + 1)
    body = title.strip()
    if len(body) + len(suffix) <= limit:
        return body + suffix
    return body[: limit - len(suffix)] + suffix


# ── 发帖器 ───────────────────────────────────────────

class BilibiliPoster:
    """单篇专栏投稿器。一个实例复用登录 cookie 与 HTTP 会话。"""

    def __init__(self, bili_config: dict | None = None, http_config: dict | None = None, session=None):
        self.cfg = dict(DEFAULT_BILI_CONFIG)
        self.cfg.update(bili_config or {})
        self.http_cfg = http_config or {}
        self.cookies = load_cookies(self.cfg)
        self.timeout = int(self.http_cfg.get("timeout") or DEFAULT_TIMEOUT)
        self.verify = self.http_cfg.get("verify_ssl", True)
        self.session = session or self._make_session()
        self.mid = None
        self.uname = ""
        self.submit_mode = self.cfg.get("submit") if self.cfg.get("submit") in ("draft", "publish") else "draft"

    # ── 会话 ──

    def _make_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            "User-Agent": self.http_cfg.get("user_agent")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/134.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": MEMBER_BASE,
            "Referer": MEMBER_BASE + "/",
        })
        session.cookies.update(self.cookies)
        proxies = {k: v for k, v in (self.http_cfg.get("proxies") or {}).items() if v}
        if proxies:
            session.proxies.update(proxies)
        return session

    @property
    def csrf_token(self) -> str:
        return self.cookies.get("bili_jct", "")

    def _require_cookies(self) -> None:
        missing = [name for name in ("SESSDATA", "bili_jct") if not self.cookies.get(name)]
        if missing:
            raise BiliError(
                f"缺少 B站登录 cookie: {', '.join(missing)}。"
                f"请把浏览器里的 SESSDATA / bili_jct 写进 config/bili_cookies.json，"
                f"或设置 BILI_SESSDATA / BILI_JCT 环境变量"
            )

    # ── 请求封装 ──

    def _decode(self, resp, action: str) -> dict:
        """校验 HTTP 状态与 JSON 结构；412 单独给一句能操作的话。"""
        if resp.status_code == 412:
            raise BiliError(f"{action} 被风控拦截（HTTP 412），本次不重试")
        if resp.status_code >= 400:
            raise BiliError(f"{action} 失败: HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise BiliError(f"{action} 返回的不是 JSON: {resp.text[:120]!r}") from e
        if not isinstance(payload, dict):
            raise BiliError(f"{action} 返回结构异常: {str(payload)[:120]}")
        return payload

    def _check_code(self, payload: dict, action: str) -> dict:
        code = payload.get("code")
        if code == 0:
            return payload
        hint = ERROR_HINTS.get(code)
        message = payload.get("message") or payload.get("msg") or "未知错误"
        detail = f"code={code} {message}" + (f"（{hint}）" if hint else "")
        raise BiliError(f"{action} 失败: {detail}")

    def _post_form(self, url: str, data: dict, action: str) -> dict:
        log_debug("[B站] POST %s（%d 个字段）", url, len(data))
        resp = self.session.post(url, data=data, timeout=self.timeout, verify=self.verify)
        return self._check_code(self._decode(resp, action), action)

    # ── 登录 ──

    def check_login(self) -> dict:
        """确认 cookie 还有效，返回账号信息（mid / uname）。"""
        self._require_cookies()
        log_info("[B站] 校验登录态…")
        resp = self.session.get(NAV_URL, timeout=self.timeout, verify=self.verify)
        payload = self._check_code(self._decode(resp, "登录校验"), "登录校验")
        data = payload.get("data") or {}
        if not data.get("isLogin"):
            raise BiliError("未登录：cookie 已失效，请重新导出 SESSDATA / bili_jct")
        self.mid = data.get("mid")
        self.uname = data.get("uname") or ""
        log_info("[B站] ✓ 已登录: %s（mid=%s）", self.uname, self.mid)
        return data

    # ── 图片 ──

    def upload_image(self, data: bytes, filename: str = "image") -> str:
        """上传单张图片到 B站图床，返回站内 URL。失败抛 BiliError。"""
        self._require_cookies()
        if len(data) > MAX_IMAGE_BYTES:
            raise BiliError(f"图片过大（{len(data)} 字节 > {MAX_IMAGE_BYTES}），已跳过")
        ext, mime = _sniff_image(data)
        if ext == "webp":
            converted = _webp_to_png(data)
            if converted is None:
                raise BiliError("upcover 不接受 webp，且本机没有 Pillow 可转码，已跳过")
            data, ext, mime = converted, "png", "image/png"

        files = {"binary": (f"{filename}.{ext}", data, mime)}
        resp = self.session.post(
            UPCOVER_URL, files=files, data={"csrf": self.csrf_token},
            timeout=self.timeout, verify=self.verify,
        )
        payload = self._check_code(self._decode(resp, "上传图片"), "上传图片")
        url = (payload.get("data") or {}).get("url")
        if not url:
            raise BiliError("上传图片没有返回站内地址")
        return url

    def upload_image_file(self, path: str) -> str:
        with open(path, "rb") as f:
            return self.upload_image(f.read(), filename=os.path.splitext(os.path.basename(path))[0])

    def download_image(self, url: str) -> bytes:
        """下载正文外链图片。只允许 https 公网地址，且不跟随重定向（防 SSRF）。"""
        _assert_public_url(url)
        resp = self.session.get(
            url, timeout=self.timeout, verify=self.verify, allow_redirects=False,
            headers={"Accept": "image/*"},
        )
        if resp.status_code != 200:
            raise BiliError(f"下载图片失败: HTTP {resp.status_code}（{url[:80]}）")
        data = resp.content or b""
        if not data:
            raise BiliError(f"图片内容为空: {url[:80]}")
        if len(data) > MAX_IMAGE_BYTES:
            raise BiliError(f"图片过大（{len(data)} 字节），已跳过: {url[:80]}")
        return data

    def rehost_images(self, html: str) -> tuple[str, int, int]:
        """把正文里的外链图片重托管到 B站 CDN。

        B站遇到外链图会整篇拒绝（37130），所以上传失败的图必须从正文里删掉，
        宁可少一张图也不能让整篇投稿失败。返回 (新 HTML, 成功数, 丢弃数)。
        """
        if not self.cfg.get("rehost_images", True):
            return html, 0, 0

        limit = int(self.cfg.get("max_inline_images") or 0)
        unlimited = limit <= 0
        uploaded = dropped = 0
        out = []
        cursor = 0
        for match in _IMG_SRC_RE.finditer(html):
            out.append(html[cursor:match.start()])
            cursor = match.end()
            src = match.group(3)
            if _is_bili_host(src):
                out.append(match.group(0))
                continue
            if not unlimited and uploaded >= limit:
                dropped += 1
                log_warning("[B站] 正文图片超过上限 %d 张，已移除: %s", limit, src[:80])
                continue
            try:
                new_url = self.upload_image(self.download_image(src), filename="inline")
            except (BiliError, ValueError, requests.RequestException) as e:
                dropped += 1
                log_warning("[B站] 图片重托管失败，已从正文移除: %s（%s）", src[:80], e)
                continue
            uploaded += 1
            out.append(match.group(1) + match.group(2) + new_url + match.group(2))
            log_debug("[B站] 图片已重托管: %s → %s", src[:60], new_url[:60])
        out.append(html[cursor:])
        return "".join(out), uploaded, dropped

    # ── 投稿 ──

    def _article_payload(self, title: str, content: str, category_id: int,
                         summary: str = "", cover_url: str = "") -> dict:
        tid = str(category_id)
        words = len(_TAG_RE.sub("", content).replace("\n", "").replace(" ", ""))
        payload = {
            "title": title,
            "content": content,
            "csrf": self.csrf_token,
            "list_id": "0",
            "reprint": "1" if self.cfg.get("reprint", True) else "0",
            "original": "1" if self.cfg.get("original", False) else "0",
            "media_id": "0",
            "spoiler": "0",
            "save": "0",
            "pgc_id": "0",
            # category 才是真正生效的分区字段，tid 必须与之一致，否则会静默落到别的分区
            "tid": tid,
            "category": tid,
            "words": str(words),
        }
        if summary:
            payload["summary"] = summary
        tags = [str(tag).strip() for tag in (self.cfg.get("tags") or []) if str(tag).strip()]
        if tags:
            payload["tags"] = ",".join(tags)
        if cover_url:
            payload["image_urls"] = cover_url
            payload["origin_image_urls"] = cover_url
        return payload

    def _candidate_categories(self) -> list[int]:
        ordered = [int(self.cfg.get("category_id") or 0)]
        for cid in self.cfg.get("candidate_categories") or []:
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                continue
            if cid not in ordered:
                ordered.append(cid)
        return [cid for cid in ordered if cid > 0]

    def save_draft(self, title: str, content: str, summary: str = "", cover_url: str = "") -> dict:
        """存草稿，返回 {"aid", "category_id", "payload"}。

        分区无权限时按候选表回退，其余错误直接抛出——换一个分区也修不好参数错误。
        """
        last_error = None
        for category_id in self._candidate_categories():
            payload = self._article_payload(title, content, category_id, summary, cover_url)
            try:
                data = self._post_form(DRAFT_URL, payload, "保存草稿")
            except BiliError as e:
                last_error = e
                text = str(e)
                if not any(hint in text for hint in _CATEGORY_HINTS):
                    raise
                log_warning("[B站] 分区 %s 不可用，尝试下一个: %s", category_id, e)
                continue
            aid = (data.get("data") or {}).get("aid")
            if not aid:
                raise BiliError("保存草稿成功但没有返回 aid")
            log_info("[B站] ✓ 草稿已保存: aid=%s（分区 %s）", aid, category_id)
            return {"aid": int(aid), "category_id": category_id, "payload": payload}
        raise last_error or BiliError("没有可用的投稿分区")

    def submit(self, draft: dict, title: str, content: str, summary: str = "",
               cover_url: str = "") -> dict:
        """把草稿提交投稿，返回 {"cv_id", "state", "url"}。"""
        payload = self._article_payload(title, content, draft["category_id"], summary, cover_url)
        payload["aid"] = str(draft["aid"])
        data = self._post_form(SUBMIT_URL, payload, "提交投稿")
        body = data.get("data") or {}
        cv_id = None
        try:
            candidate = int(body.get("aid") or 0)
            cv_id = candidate if candidate > 0 else None
        except (TypeError, ValueError):
            cv_id = None
        state = body.get("state")
        try:
            state = int(state) if state is not None else None
        except (TypeError, ValueError):
            state = None
        return {
            "cv_id": cv_id,
            "state": state,
            "url": f"https://www.bilibili.com/read/cv{cv_id}" if cv_id else None,
        }

    def publish(self, title: str, content: str, summary: str = "", cover_url: str = "") -> dict:
        """存草稿（+ 按配置提交）。返回结构化结果，不抛业务错误，方便批量流程记账。"""
        result = {
            "ok": False,
            "title": title,
            "draft_aid": None,
            "cv_id": None,
            "url": None,
            "state": None,
            "state_desc": "",
            "pending_review": False,
            "category_id": None,
            "edit_url": "",
            "submit_mode": self.submit_mode,
            "error": None,
        }
        try:
            self._require_cookies()
            draft = self.save_draft(title, content, summary, cover_url)
        except BiliError as e:
            result["error"] = str(e)
            log_warning("[B站] 存草稿失败: %s", e)
            return result

        result["draft_aid"] = draft["aid"]
        result["category_id"] = draft["category_id"]
        result["edit_url"] = EDIT_URL_TEMPLATE.format(aid=draft["aid"])

        if self.submit_mode != "publish":
            result["ok"] = True
            log_info("[B站] 已保存草稿（submit=draft，需人工在编辑器发布）: %s", result["edit_url"])
            return result

        try:
            submitted = self.submit(draft, title, content, summary, cover_url)
        except BiliError as e:
            # 412/请求失败时草稿仍在，这是可交付的结果，不能算「没发出去」
            result["error"] = str(e)
            log_warning("[B站] 提交投稿失败，草稿已保留: %s（%s）", result["edit_url"], e)
            return result

        state = submitted["state"]
        result["cv_id"] = submitted["cv_id"]
        result["state"] = state
        result["state_desc"] = STATE_TEXT.get(state, "未知" if state is None else str(state))
        if state == -2:
            # 待审核：URL 审核通过前打不开，不能当成功也不能当失败
            result["ok"] = True
            result["pending_review"] = True
            log_info("[B站] ✓ 已提交投稿，进入审核队列（state=-2）")
        elif state is None:
            result["ok"] = True
            result["error"] = "提交成功但没返回状态，请到投稿管理确认"
            log_warning("[B站] 提交结果缺少 state，需人工确认: %s", result["edit_url"])
        elif state in (0, 1):
            result["ok"] = True
            result["url"] = submitted["url"]
            log_info("[B站] ✓ 已发布: %s", result["url"])
        else:
            result["error"] = f"投稿未通过: state={state}（{result['state_desc']}）"
            log_warning("[B站] %s", result["error"])
        return result

    # ── 文件级入口（与 poster.post_news_file 对齐） ──

    @staticmethod
    def _build_summary(data: dict, limit: int) -> str:
        """摘要取正文前若干字：B站首页列表会显示，纯文本、不带标签。"""
        if limit <= 0:
            return ""
        parts = []
        total = 0
        for block in data.get("blocks") or []:
            if (block.get("type") or "").lower() in ("img", "pre", "code"):
                continue
            text = (block.get("translated_text") or block.get("source_text") or "").strip()
            if not text:
                continue
            parts.append(text)
            total += len(text)
            if total >= limit:
                break
        summary = re.sub(r"\s+", " ", " ".join(parts)).strip()
        return summary[:limit]

    def post_news_file(self, stem: str, json_path: str, html_path: str,
                       cover_path: str | None = None) -> dict:
        """读 JSON 元数据与专栏 HTML，完成一次投稿。"""
        with open(json_path, encoding="utf-8") as f:
            meta = json.load(f)
        with open(html_path, encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            raise BiliError(f"{html_path} 内容为空")

        base_title = (meta.get("translated_title") or meta.get("title") or "").strip()
        if not base_title:
            raise BiliError(f"{json_path} 中找不到标题")
        title = _truncate_title(
            base_title, str(self.cfg.get("title_suffix") or ""), int(self.cfg.get("max_title_chars") or 80)
        )
        summary = self._build_summary(meta, int(self.cfg.get("summary_max_chars") or 0))

        notice = str(self.cfg.get("ai_notice") or "").strip()
        if notice:
            content += f"\n<p>{notice}</p>"

        content, uploaded, dropped = self.rehost_images(content)
        log_info("[B站] 正文图片: 重托管 %d 张，丢弃 %d 张", uploaded, dropped)

        cover_url = ""
        if cover_path and self.cfg.get("attach_cover", True):
            try:
                cover_url = self.upload_image_file(cover_path)
                log_info("[B站] ✓ 封面已上传: %s", cover_url)
            except (BiliError, OSError, requests.RequestException) as e:
                log_warning("[B站] 封面上传失败，改为无封面投稿: %s", e)

        result = self.publish(title, content, summary=summary, cover_url=cover_url)
        result["stem"] = stem
        result["images"] = {"uploaded": uploaded, "dropped": dropped}
        result["cover_url"] = cover_url
        return result
