#!/usr/bin/env python3
"""
poster.py — MCBBS 自动发帖模块（新闻模式 + 图片上传）

支持两种使用方式：
  1. CLI 模式：python poster.py [选项]（与原 post.py 兼容）
  2. 模块导入：from poster import MCBBSPoster（供 main.py 调用）

文件命名规则：
  news_xxx.txt  → 帖子正文（MCBBS BBCode）
  news_xxx.json → 元数据，需包含 "title" 字段
  news_xxx.jpg  → 题图（可选）
"""

import argparse
import contextlib
import glob
import json
import os
import re
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from html import unescape
from urllib.parse import quote, unquote

import requests

from log_setup import current_log_path, log_debug, log_error, log_info, log_warning, setup_logging
from utils import (
    CONFIG_DIR,
    MODULE_TYPE_MAP,
    find_image,
    load_posted,
    load_thread_records,
    save_posted,
    save_thread_records,
    thread_records_path,
)

# ── 配置 ─────────────────────────────────────────────

_CATEGORY_GROUP = {
    'java_snapshot': 'Java资讯',
    'java_prerelease': 'Java资讯',
    'java_rc': 'Java资讯',
    'java_release': 'Java资讯',
    'bedrock_beta': '基岩版资讯',
    'bedrock_release': '基岩版资讯',
    'commentary': '块讯',
    'normal': '块讯',
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

IMG_INSERT_BEFORE = "[align=center][size=5][b]NEWS[/b]"

# 登录重试默认值，config.json 的 retry.login 可以覆盖（见 load_poster_config）
DEFAULT_LOGIN_RETRY = {"max_attempts": 3, "wait_seconds": 10}

# 登录链路的请求必须有超时：站点或网络挂住时，超时才能把它变成可重试的失败。
# 这里是交互式登录，不用抓取链路的 http.timeout（那是有意给长下载留的余量）。
# 列表页、高亮、过审探测这几条短请求也共用这个超时。
LOGIN_TIMEOUT = 30

# 发帖响应的跳转地址有三种写法：静态化地址 thread-123-1-1.html、查询串
# forum.php?mod=viewthread&tid=123，以及弹窗脚本里被转义的 window.location.href =\'...\'。
# 旧实现只认第一种且要求引号不被转义，于是"进审核"的帖子永远拿不到帖子 ID。
_REDIRECT_JS_RE = re.compile(r"window\.location\.href\s*=\s*\\?'([^']+?)\\?'")
_TID_URL_RE = re.compile(r"thread-(\d+)-1-1")
_TID_QUERY_RE = re.compile(r"[?&](?:amp;)?tid=(\d+)")


# Discuz 的标题高亮色板（forum_colorarray）。下标就是要提交的 highlight_color，
# 色值与版规「二、高亮设定」里写的完全一致，所以配置用色值、代码翻译成色号。
_HIGHLIGHT_COLORS = {
    "#EE1B2E": 1,
    "#EE5023": 2,
    "#996600": 3,
    "#3C9D40": 4,
    "#2897C5": 5,
    "#2B65B7": 6,
    "#8F2A90": 7,
    "#EC1282": 8,
}

# 高亮默认值：色号取自幻翼块讯版规「二、高亮设定」，commentary（时评）按版规一般不高亮。
DEFAULT_HIGHLIGHT = {
    "enabled": True,
    "colors": {
        "java_release": "#EE1B2E",
        "java_snapshot": "#3C9D40",
        "java_prerelease": "#3C9D40",
        "java_rc": "#3C9D40",
        "bedrock_release": "#EE1B2E",
        "bedrock_beta": "#3C9D40",
        "normal": "#EC1282",
    },
    "bold_types": ["java_release", "bedrock_release"],
    "max_attempts": 6,
    "max_age_hours": 168,
}


def highlight_color_index(color: str) -> int | None:
    """把配置里的色值翻成 Discuz 的高亮色号；不在色板内返回 None。"""
    return _HIGHLIGHT_COLORS.get((color or "").strip().upper())


def _merge_highlight(raw) -> dict:
    """合并 mcbbs.highlight：colors 逐项覆盖，其余键只认合法类型，非法值保持默认。"""
    merged = deepcopy(DEFAULT_HIGHLIGHT)
    if not isinstance(raw, dict):
        return merged
    if isinstance(raw.get("enabled"), bool):
        merged["enabled"] = raw["enabled"]
    colors = raw.get("colors")
    if isinstance(colors, dict):
        merged["colors"].update({str(k): v for k, v in colors.items() if isinstance(v, str)})
    bold_types = raw.get("bold_types")
    if isinstance(bold_types, list):
        merged["bold_types"] = [str(item) for item in bold_types]
    for key in ("max_attempts", "max_age_hours"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            merged[key] = value
    return merged


@dataclass(frozen=True)
class PostResult:
    """一次发帖的结果。

    帖子 ID 与「要不要审核」必须一起带出来：Discuz 对 displayorder < 0（审核中）
    的帖子拒绝设置高亮，而这类响应往往又不带可用的帖子地址。少了这两个值，
    审核中的帖子就再也补不上高亮了。
    """

    url: str
    tid: int | None = None
    needs_audit: bool = False


def _extract_tid(text: str) -> int | None:
    """从地址或页面片段里取帖子 ID。"""
    match = _TID_URL_RE.search(text or "") or _TID_QUERY_RE.search(text or "")
    return int(match.group(1)) if match else None


def _find_redirect(html: str) -> str:
    """取发帖响应里脚本跳转的目标地址；优先带帖子 ID 的那一条。"""
    targets = [match.group(1) for match in _REDIRECT_JS_RE.finditer(html or "")]
    for target in targets:
        if _extract_tid(target):
            return target
    return targets[0] if targets else ""


def _timestamp() -> str:
    """记录用的本地时间戳（秒级，ISO 风格，便于人眼与排序）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


_MY_THREAD_LINK_RE = re.compile(
    r'href="((?:forum\.php\?mod=viewthread&(?:amp;)?tid=\d+|thread-\d+-1-1\.html)[^"]*)"'
    r"[^>]*>(.*?)</a>",
    re.S,
)


def _normalize_title(text: str) -> str:
    """标题归一：去标签、反转义实体、压空白，用于和记录里的标题比对。"""
    plain = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", unescape(plain)).strip()


def _parse_my_threads(html: str) -> list[tuple[int, str]]:
    """从「我的帖子」列表里取 [(tid, 标题), ...]，同一 tid 只留第一次出现。

    列表页每行都有多个指向同一帖子的链接（标题、回复数、最后回复），所以按 tid
    去重；回复数/时间这类噪声文本也会被收进来，但标题匹配时自然会被忽略。
    """
    threads: list[tuple[int, str]] = []
    seen: set[int] = set()
    for match in _MY_THREAD_LINK_RE.finditer(html or ""):
        tid = _extract_tid(match.group(1))
        title = _normalize_title(match.group(2))
        if not tid or not title or title.isdigit() or tid in seen:
            continue
        seen.add(tid)
        threads.append((tid, title))
    return threads


def thread_is_public(base_url: str, tid: int, timeout: int = LOGIN_TIMEOUT) -> bool | None:
    """以游客身份探测帖子是否对外可见：True 可见 / False 不可见 / None 探测失败。

    必须用不带 cookie 的会话：作者本人和版主都能看到审核中的帖子，拿登录态探测
    会把「审核中」误判成「已过审」。Discuz 对审核中或已删除的帖子回 404 提示页。
    """
    url = f"{base_url}/thread-{tid}-1-1.html"
    try:
        with requests.Session() as session:
            session.headers.update({"User-Agent": UA})
            response = session.get(url, timeout=timeout)
    except requests.RequestException:
        log_debug("    过审探测失败: %s", url, exc_info=True)
        return None
    if response.status_code == 404:
        return False
    if response.status_code >= 400:
        return None
    return 'id="thread_subject"' in (response.text or "")


def _dump_threads_html(html: str) -> str:
    """把「我的帖子」页面落到日志目录，解析失败时用来对站点的自定义模板。"""
    path = os.path.join(
        _login_debug_dir(), f"mcbbs_my-threads_{time.strftime('%Y%m%d-%H%M%S')}.html"
    )
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_scrub_login_html(html or ""))
    except OSError:
        log_debug("    「我的帖子」页面落盘失败", exc_info=True)
        return ""
    return path


_MODERATION_ERROR_RE = re.compile(r"errorhandle_\w+\(\s*'((?:[^'\\]|\\.)*)'")


def _parse_moderation_result(html: str) -> tuple[bool, str]:
    """解析 forum.php?mod=topicadmin 的 ajax 返回，给出 (是否成功, 失败原因)。

    Discuz 只在 inajax=1 且请求带 redirect 时才把成功写成 succeedhandle_mods(...)，
    失败一律走 errorhandle_mods(站点提示语)。两者都不出现，说明站点改版或会话失效；
    旧实现只认 succeedhandle，所以即使高亮成功也会被判成失败。
    """
    text = html or ""
    if "succeedhandle" in text:
        return True, ""
    match = _MODERATION_ERROR_RE.search(text)
    if match:
        return False, match.group(1).replace("\\'", "'")[:200]
    message = re.search(r'id="messagetext"[^>]*>(.*?)</div>', text, re.S)
    if message:
        return False, re.sub(r"<[^>]+>", "", message.group(1)).strip()[:200]
    return False, f"未识别的管理操作响应（{len(text)} 字节）"


# ── 配置加载 ─────────────────────────────────────────

def load_poster_config(config_path: str = None) -> dict:
    """从统一 config.json 中提取 mcbbs 相关配置"""
    if config_path is None:
        config_path = os.path.join(CONFIG_DIR, "config.json")

    cfg = {
        "base_url": "https://www.mcbbs.co",
        "forum_fid": 2,
        "username": "",
        "password": "",
        "captcha_answer": "",
        "sortid_map": {},
        "login_retry": dict(DEFAULT_LOGIN_RETRY),
        "highlight": deepcopy(DEFAULT_HIGHLIGHT),
    }

    if os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                full_config = json.load(f)
            mcbbs = full_config.get("mcbbs", {})
            for key in cfg:
                if key in mcbbs and mcbbs[key]:
                    cfg[key] = mcbbs[key]
            # highlight 是带默认值的嵌套段，整体覆盖会把默认色板冲掉，单独合并
            cfg["highlight"] = _merge_highlight(mcbbs.get("highlight"))
            # 登录重试参数在顶层 retry 段（与 retry.translation / retry.download 并列）
            login_retry = (full_config.get("retry") or {}).get("login") or {}
            for key in DEFAULT_LOGIN_RETRY:
                value = login_retry.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    cfg["login_retry"][key] = value
        except Exception as e:
            log_warning(f"[配置] 读取 mcbbs 配置失败: {e}")

    # 环境变量覆盖
    env_map = {
        "MCBBS_BASE_URL": "base_url",
        "MCBBS_FORUM_FID": "forum_fid",
        "MCBBS_USERNAME": "username",
        "MCBBS_PASSWORD": "password",
        "MCBBS_CAPTCHA_ANSWER": "captcha_answer",
    }
    for env_name, cfg_key in env_map.items():
        val = os.environ.get(env_name)
        if val:
            if cfg_key == "forum_fid":
                with contextlib.suppress(ValueError):
                    val = int(val)
            cfg[cfg_key] = val

    return cfg


# ── 辅助函数 ─────────────────────────────────────────

def detect_module_type(message: str, title: str) -> str | None:
    for tag, category in MODULE_TYPE_MAP.items():
        if f"[{tag}]" in message:
            return category
    from utils import classify_article_type
    return classify_article_type(title, chinese=True, fallback=None)


def insert_image_bbcode(message: str, aid: str) -> str:
    img_tag = f"[align=center][attachimg]{aid}[/attachimg][/align]"
    idx = message.find(IMG_INSERT_BEFORE)
    if idx != -1:
        return message[:idx] + img_tag + "\n\n" + message[idx:]
    hr_idx = message.find("[hr]")
    if hr_idx != -1:
        insert_pos = hr_idx + len("[hr]")
        while insert_pos < len(message) and message[insert_pos] in "\n\r ":
            insert_pos += 1
        return message[:hr_idx + len("[hr]")] + "\n\n" + img_tag + "\n\n" + message[insert_pos:]
    return img_tag + "\n\n" + message


# ── MCBBS 会话与登录 ─────────────────────────────────

# 验证码 idhash 在页面上的写法：标准模板是 updateseccode('hash', 'modid')，
# 部分模板只留 <input name="seccodehash">，或把 idhash 拼在验证码图片地址上。
_SECCODE_HASH_PATTERNS = (
    r"updateseccode\('([A-Za-z0-9]+)'",
    r'name="seccodehash"\s+value="([A-Za-z0-9]+)"',
    r"idhash=([A-Za-z0-9]+)",
)

# 记日志/落盘时关注的页面标记，用来区分登录表单、验证码表单和风控页
_LOGIN_MARKERS = ("formhash", "loginhash", "updateseccode", "seccodehash", "验证码", "messagetext")

# 站点明确拒绝凭证的提示：重试只会加重失败计数，遇到就立即停手。
# 注意不能把「验证码错误」放进来——那是 OCR 没认对，应该换一张继续试。
_PERMANENT_LOGIN_HINTS = (
    "密码错误", "用户名或密码", "用户名不存在", "用户不存在",
    "安全问题", "账号", "冻结", "禁止", "锁定", "尝试",
)


class LoginRetryable(RuntimeError):
    """登录流程里可以换新会话重试的失败。

    验证码页结构不符、风控页、会话没带上登录态都属于这一类；凭证被站点明确
    拒绝（密码错误/安全问题）不算——重试只会让失败计数继续涨，还可能触发验证码。
    """


class LoginRejected(RuntimeError):
    """站点明确拒绝凭证或账号状态（密码错误/安全问题/被限制），重试没有意义。"""


def _is_permanent_login_error(message: str) -> bool:
    """提示语是不是「重试也没用」的那一类（凭证被拒、账号被限制）。"""
    return any(hint in message for hint in _PERMANENT_LOGIN_HINTS)


def extract_formhash(html: str) -> str:
    m = re.search(r'name="formhash"\s+value="([a-f0-9]+)"', html)
    if not m:
        raise ValueError("无法提取 formhash")
    return m.group(1)


def extract_loginhash(html: str) -> str:
    m = re.search(r'loginhash=([A-Za-z0-9]+)', html)
    return m.group(1) if m else ""


def _captcha_page_hash(html: str) -> str:
    """从带验证码的登录页里取 idhash；页面不像登录表单时返回空串。

    只认 seccode 标记不够：风控页、错误页偶尔也带 idhash，但没有 formhash 提交不了。
    """
    if 'name="formhash"' not in html:
        return ""
    for pattern in _SECCODE_HASH_PATTERNS:
        m = re.search(pattern, html)
        if m:
            return m.group(1)
    return ""


def _login_debug_dir() -> str:
    """登录排查页面的落盘目录：跟日志同目录，测试里可用 MCTTK_LOG_DIR 指定。"""
    log_path = current_log_path()
    if log_path:
        return os.path.dirname(log_path)
    return os.environ.get("MCTTK_LOG_DIR") or "logs"


def _scrub_login_html(html: str) -> str:
    """落盘前抹掉登录令牌和密码框内容：日志里不许出现凭据。

    auth 的值按「到分隔符为止」整体抹掉，不假设它只含 base64 字符——站点可能用
    URL-safe 的 `-`/`_` 或带 `=` 填充，漏掉就会把会话令牌留在排查文件里。
    """
    html = re.sub(r"(auth=)[^\s\"'&<>]+", r"\1***", html)
    return re.sub(r'(name="password"[^>]*value=")[^"]*(")', r"\1***\2", html)


def _dump_login_html(tag: str, html: str) -> str:
    """把登录响应写到日志目录，返回路径；写不了返回空串。"""
    directory = _login_debug_dir()
    path = os.path.join(directory, f"mcbbs_login_{tag}_{time.strftime('%Y%m%d-%H%M%S')}.html")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_scrub_login_html(html))
    except OSError:
        log_debug("    登录页面落盘失败", exc_info=True)
        return ""
    return path


def _log_login_evidence(tag: str, label: str, response) -> str:
    """记录一次登录响应的关键特征并把页面落盘，用于定位验证码页/风控页。"""
    html = getattr(response, "text", "") or ""
    status = getattr(response, "status_code", 0)
    marks = " ".join(f"{marker}={html.count(marker)}" for marker in _LOGIN_MARKERS)
    path = _dump_login_html(tag, html)
    log_warning("[登录排查] %s: HTTP %s，%d 字节，%s；页面已存: %s",
                label, status, len(html), marks, path or "（落盘失败）")
    return path


def _verify_login(session: requests.Session, base_url: str) -> str:
    r = session.get(f"{base_url}/forum.php", timeout=LOGIN_TIMEOUT)
    r.raise_for_status()
    uid = re.search(r"discuz_uid\s*=\s*'(\d+)'", r.text)
    if not uid or uid.group(1) == "0":
        raise LoginRetryable("登录验证失败：discuz_uid=0")
    return extract_formhash(r.text)


def _init_ocr():
    """初始化 ddddocr，失败返回 None"""
    try:
        import ddddocr
        return ddddocr.DdddOcr(show_ad=False)
    except Exception:  # noqa: BLE001
        log_debug("ddddocr 初始化失败，将跳过 OCR 识别", exc_info=True)
        return None


def _preprocess_captcha(img_bytes: bytes) -> list:
    """对验证码图片做多种预处理，返回多个候选图片 bytes"""
    candidates = [img_bytes]
    try:
        import io as _io

        from PIL import Image, ImageFilter
        img = Image.open(_io.BytesIO(img_bytes))
        gray = img.convert("L")
        bw = gray.point(lambda x: 255 if x > 128 else 0, "1")
        buf = _io.BytesIO()
        bw.save(buf, format="PNG")
        candidates.append(buf.getvalue())
        big = img.convert("L").resize((img.width * 2, img.height * 2), Image.LANCZOS)
        sharp = big.filter(ImageFilter.SHARPEN)
        buf2 = _io.BytesIO()
        sharp.save(buf2, format="PNG")
        candidates.append(buf2.getvalue())
    except Exception:  # noqa: BLE001
        log_debug("验证码图片预处理失败，使用原始图片", exc_info=True)
    return candidates


def _ocr_recognize(ocr, img_bytes: bytes) -> str:
    """用 OCR 识别验证码，返回识别结果（失败返回空字符串）"""
    if not ocr:
        return ""
    candidates = _preprocess_captcha(img_bytes)
    for idx, img_data in enumerate(candidates):
        try:
            raw = re.sub(r'[^a-zA-Z0-9]', '', ocr.classification(img_data).strip())
            if 3 <= len(raw) <= 8:
                tag = "原始" if idx == 0 else f"预处理#{idx}"
                log_debug(f"    OCR ({tag}): {raw}")
                return raw
        except Exception:  # noqa: BLE001
            log_debug("OCR 识别单张图片失败", exc_info=True)
    return ""


def _submit_captcha_login(session, base_url, r_cap, idhash, auth_token,
                          username, password, answer) -> bool:
    """提交一次验证码登录，成功返回 True，失败返回 False"""
    try:
        fh_cap = extract_formhash(r_cap.text)
    except ValueError as e:
        _log_login_evidence("captcha", "验证码页", r_cap)
        raise LoginRetryable(f"验证码页缺少 formhash（{e}）") from None
    lh_cap = extract_loginhash(r_cap.text)
    seccode_field = "seccodeverify"
    if 'name="seccode"' in r_cap.text and 'name="seccodeverify"' not in r_cap.text:
        seccode_field = "seccode"
    all_modids = re.findall(r"updateseccode\('[^']+',[^,]+,\s*'([^']+)'\)", r_cap.text)
    seccodemodid = all_modids[-1] if all_modids else "member::logging"

    post_data = {
        "formhash": fh_cap,
        "referer": f"{base_url}/",
        "auth": auth_token,
        "username": username,
        "password": password,
        "questionid": "0",
        "answer": "",
        "seccodehash": idhash,
        "seccodemodid": seccodemodid,
        seccode_field: answer,
    }
    referer_url = (
        f"{base_url}/member.php?mod=logging&action=login"
        f"&auth={quote(auth_token, safe='')}&referer={base_url}/&cookietime=1"
    )
    log_debug(f"    提交: {seccode_field}={answer}, password={'(set)' if password else '(EMPTY!)'}")
    session.headers.update({"Referer": referer_url})
    r_post = session.post(
        f"{base_url}/member.php?mod=logging&action=login"
        f"&loginsubmit=yes&loginhash={lh_cap}&inajax=1",
        data=post_data,
        timeout=LOGIN_TIMEOUT,
    )
    session.headers.pop("Referer", None)

    if "欢迎您回来" in r_post.text or "succeedhandle_" in r_post.text:
        return True

    err_match = re.search(r'id="messagetext"[^>]*>(.*?)</div>', r_post.text, re.S)
    if err_match:
        err_msg = re.sub(r'<[^>]+>', '', err_match.group(1)).strip()[:200]
        if "安全问题" not in err_msg:
            log_warning(f"    ✗ 登录被拒: {err_msg}")
        # 凭证被拒和「验证码不对」必须分开：前者再试只会加重失败计数
        if _is_permanent_login_error(err_msg):
            raise LoginRejected(f"登录失败: {err_msg}")
    else:
        log_warning(f"    ✗ 未知登录结果（HTTP {r_post.status_code}，长度 {len(r_post.text)}）")
    return False


def _fetch_captcha_page(session, base_url, auth_token) -> tuple:
    """重新取一次登录页，返回 (响应, idhash, auth_token)；idhash 可能为空。"""
    r_cap = session.get(
        f"{base_url}/member.php?mod=logging&action=login"
        f"&auth={quote(auth_token, safe='')}&referer={base_url}/&cookietime=1",
        timeout=LOGIN_TIMEOUT,
    )
    auth_m = re.search(r"auth=([A-Za-z0-9%/+]+)", r_cap.text)
    if auth_m:
        auth_token = unquote(auth_m.group(1))
    return r_cap, _captcha_page_hash(r_cap.text), auth_token


def _refresh_captcha_page(session, base_url, auth_token, attempts: int = 3) -> tuple:
    """换一张验证码页面：重取登录页并确认里面带 seccode 标记。"""
    r_cap = None
    for attempt in range(1, attempts + 1):
        time.sleep(1)
        r_cap, idhash, auth_token = _fetch_captcha_page(session, base_url, auth_token)
        if idhash:
            return r_cap, idhash, auth_token
        log_debug("    重新获取的登录页里没有 seccode 标记（第 %d/%d 次）", attempt, attempts)
    _log_login_evidence("captcha-page", "重新获取的登录页", r_cap)
    raise LoginRetryable("无法提取验证码 hash（重新获取的登录页里没有 seccode 标记）")


def _select_captcha_page(session, base_url, r, auth_token, attempts: int = 3) -> tuple:
    """定位带验证码表单的页面，返回 (响应, idhash, auth_token)。

    站点要求验证码时，刚才那次失败的 POST 响应本身就是带验证码的登录页，
    优先用它；页面里没有 seccode 标记时才重新取登录页。
    """
    idhash = _captcha_page_hash(r.text)
    if idhash:
        log_debug("    验证码 hash 取自失败的登录响应（无需重取登录页）")
        return r, idhash, auth_token
    log_debug("    失败响应里没有 seccode 标记，改为重新获取登录页")
    try:
        return _refresh_captcha_page(session, base_url, auth_token, attempts)
    except LoginRetryable:
        _log_login_evidence("captcha", "失败响应", r)
        raise


def _login_with_captcha(session, base_url, username, password, captcha_answer, r):
    """处理「需要验证码」的登录，返回登录后的 formhash。

    站点是概率性要求验证码的，所以这里不轻易放弃：取不到验证码页先把响应落盘
    留证据，再抛可重试错误交给外层换会话重试。
    """
    log_warning("    ⚠ 需要验证码")
    auth_m = re.search(r"auth=([A-Za-z0-9%/+]+)", r.text)
    if not auth_m:
        _log_login_evidence("captcha", "失败响应（没有 auth token）", r)
        raise LoginRetryable("无法提取 auth token")
    auth_token = unquote(auth_m.group(1))

    r_cap, idhash, auth_token = _select_captcha_page(session, base_url, r, auth_token)
    ocr = _init_ocr()

    for attempt in range(1, 6):
        referer_url = (
            f"{base_url}/member.php?mod=logging&action=login"
            f"&auth={auth_token}&referer={base_url}/&cookietime=1"
        )
        session.headers.update({"Referer": referer_url})
        update_seed = str(int(time.time() * 1000))[-6:]
        r_img = session.get(f"{base_url}/misc.php?mod=seccode&update={update_seed}&idhash={idhash}",
                            timeout=LOGIN_TIMEOUT)
        session.headers.pop("Referer", None)

        if len(r_img.content) < 100:
            log_warning("    验证码图片为空（第%d次），换一张", attempt)
            r_cap, idhash, auth_token = _refresh_captcha_page(session, base_url, auth_token)
            continue

        with open("captcha.png", "wb") as f:
            f.write(r_img.content)
        log_debug("    验证码已保存: captcha.png（%d 字节）", len(r_img.content))

        answer = _ocr_recognize(ocr, r_img.content)
        if not answer:
            if captcha_answer:
                answer = captcha_answer
                log_warning("    OCR 失败，使用手动验证码: %s", answer)
            else:
                log_warning("    OCR 失败，无手动验证码，跳过第%d次", attempt)
                r_cap, idhash, auth_token = _refresh_captcha_page(session, base_url, auth_token)
                continue

        if _submit_captcha_login(session, base_url, r_cap, idhash, auth_token,
                                 username, password, answer):
            log_info("    ✓ 验证码登录成功！（第%d次）", attempt)
            return _verify_login(session, base_url)

        if attempt < 5:
            log_debug("    重新获取验证码页面（第%d次尝试后）...", attempt)
            r_cap, idhash, auth_token = _refresh_captcha_page(session, base_url, auth_token)

    _log_login_evidence("captcha", "验证码循环结束时的页面", r_cap)
    raise LoginRetryable("验证码识别率过低，请检查 captcha.png 后手动重试")


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return s


# ── MCBBSPoster 类（编程接口）──────────────────────

class MCBBSPoster:
    """MCBBS 发帖器，支持批量发帖时复用登录会话"""

    def __init__(self, config: dict = None):
        if config is None:
            config = load_poster_config()
        self.base_url = config["base_url"]
        self.forum_fid = config["forum_fid"]
        self.username = config["username"]
        self.password = config["password"]
        self.captcha_answer = config.get("captcha_answer", "")
        self.sortid_map = config.get("sortid_map", {})
        self.login_retry = dict(config.get("login_retry") or {})
        self.highlight = _merge_highlight(config.get("highlight"))
        self.session = None
        self.formhash = None

    def _login_retry_policy(self) -> tuple[int, int]:
        """登录重试次数与间隔（秒），默认值来自 DEFAULT_LOGIN_RETRY。"""
        attempts, wait = DEFAULT_LOGIN_RETRY["max_attempts"], DEFAULT_LOGIN_RETRY["wait_seconds"]
        with contextlib.suppress(TypeError, ValueError):
            attempts = int(self.login_retry.get("max_attempts", attempts))
        with contextlib.suppress(TypeError, ValueError):
            wait = int(self.login_retry.get("wait_seconds", wait))
        return max(attempts, 1), max(wait, 0)

    def login(self) -> bool:
        """登录 MCBBS，返回是否成功。

        站点会概率性地要求验证码，验证码页的结构也不一定符合标准模板。一次失败
        就整轮不发布代价太大，所以登录失败（含网络抖动）时换一个新会话重试；
        凭证被站点明确拒绝则立即抛出，不再加重失败计数。重试参数见 retry.login。
        """
        if not self.username or not self.password:
            raise RuntimeError("未配置 MCBBS 账号密码")

        # 补高亮可能已经登录过了（它只在真要动手时才登录），这里别再跑一遍验证码
        if self.session is not None and self.formhash:
            log_info("[*] 复用已有的 MCBBS 会话")
            return True

        max_attempts, wait_seconds = self._login_retry_policy()
        attempt = 0
        while True:
            attempt += 1
            suffix = "" if attempt == 1 else f"（第 {attempt}/{max_attempts} 次）"
            log_info(f"[*] 登录 MCBBS: {self.username}{suffix}")
            try:
                return self._login_once()
            except (LoginRetryable, requests.RequestException) as e:
                if attempt >= max_attempts:
                    raise
                log_warning(f"    ⚠ 登录未完成: {e}；{wait_seconds} 秒后换新会话重试")
                time.sleep(wait_seconds)

    def _login_once(self) -> bool:
        """走一遍完整登录流程（新建会话）；失败时抛可重试/不可重试的登录错误。"""
        self.session = _make_session()

        r = self.session.get(f"{self.base_url}/member.php?mod=logging&action=login",
                             timeout=LOGIN_TIMEOUT)
        r.raise_for_status()
        try:
            formhash = extract_formhash(r.text)
        except ValueError as e:
            _log_login_evidence("login", "登录页", r)
            raise LoginRetryable(f"登录页缺少 formhash（{e}），可能是风控页或站点改版") from None
        loginhash = extract_loginhash(r.text)

        login_url = (
            f"{self.base_url}/member.php?mod=logging&action=login"
            f"&loginsubmit=yes&loginhash={loginhash}"
        )
        data = {
            "formhash": formhash, "referer": "./",
            "username": self.username, "password": self.password,
            "questionid": "0", "answer": "", "cookietime": "2592000",
        }
        r = self.session.post(login_url, data=data, timeout=LOGIN_TIMEOUT)

        if "欢迎您回来" in r.text:
            self.formhash = _verify_login(self.session, self.base_url)
            log_info("    ✓ 登录成功！")
            return True

        if "验证码" in r.text:
            self.formhash = _login_with_captcha(
                self.session, self.base_url,
                self.username, self.password, self.captcha_answer, r
            )
            return True

        err = re.search(r'id="messagetext"[^>]*>(.*?)</div>', r.text, re.S)
        if not err:
            _log_login_evidence("login", "登录结果", r)
            raise LoginRetryable("登录结果无法识别（页面里没有提示信息），可能命中风控页")
        err_msg = re.sub(r"<[^>]+>", "", err.group(1)).strip()[:200]
        # 凭证被拒就停手；其余提示（来路不正确、表单验证串不符等）多是临时状态，值得重试
        if _is_permanent_login_error(err_msg):
            raise LoginRejected(f"登录失败: {err_msg}")
        _log_login_evidence("login", "登录结果", r)
        raise LoginRetryable(f"登录失败（可重试）: {err_msg}")

    def _upload(self, file_path: str, upload_filename: str, mime_type: str,
                extra_data: dict = None) -> str:
        """内部上传方法，封装重试逻辑。extra_data 用于图片上传的 type:image 参数。"""
        upload_url = (
            f"{self.base_url}/misc.php?mod=swfupload&action=swfupload"
            f"&operation=upload&fid={self.forum_fid}"
        )
        referer = f"{self.base_url}/forum.php?mod=post&action=newthread&fid={self.forum_fid}"
        aid = ""
        for attempt in range(1, 4):
            r = self.session.get(referer)
            r.raise_for_status()
            hash_m = re.search(r'"hash"\s*:\s*"([a-f0-9]+)"', r.text)
            uid_m = re.search(r'"uid"\s*:\s*"(\d+)"', r.text)
            if not hash_m or not uid_m:
                raise RuntimeError("无法提取上传参数")

            self.session.headers.update({"Referer": referer})
            data = {"uid": uid_m.group(1), "hash": hash_m.group(1)}
            if extra_data:
                data.update(extra_data)
            with open(file_path, "rb") as f:
                r_up = self.session.post(
                    upload_url,
                    files={"Filedata": (upload_filename, f, mime_type)},
                    data=data,
                    timeout=30,
                )
            self.session.headers.pop("Referer", None)

            aid = r_up.text.strip()
            if aid.isdigit():
                return aid

            if attempt < 3:
                log_warning(f"    ⚠ 第{attempt}次上传失败，2秒后重试...")
                time.sleep(2)

        raise RuntimeError(f"上传失败: {aid[:200]}")

    def upload_file(self, file_path: str, mime_type: str = None) -> str:
        """上传普通文件（非图片附件），返回附件 ID。"""
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")

        filename = os.path.basename(file_path)
        ext = os.path.splitext(file_path)[1].lower()
        if mime_type is None:
            mime_map = {
                ".json": "text/plain",   # Discuz swfupload 拒绝 application/json
                ".txt": "text/plain",
                ".md": "text/plain",     # Discuz 不接受 text/markdown
                ".pdf": "application/pdf",
                ".zip": "application/zip",
            }
            mime_type = mime_map.get(ext, "application/octet-stream")

        log_info(f"    上传文件: {filename}")

        # Discuz swfupload 服务端校验文件扩展名
        # 上传时改扩展名为 .txt 绕过限制（内容不变，用户下载后改名即可）
        allowed_exts = {".chm", ".pdf", ".zip", ".7z", ".7zip", ".rar", ".tar",
                        ".gz", ".bzip2", ".gif", ".jpg", ".jpeg", ".png", ".jar",
                        ".txt", ".webp", ".log", ".conf", ".mcworld", ".mcpack",
                        ".lang", ".bmp"}
        upload_filename = filename
        if ext not in allowed_exts:
            upload_filename = os.path.splitext(filename)[0] + ".txt"
            mime_type = "text/plain"
            log_info(f"    (扩展名 {ext} 不被允许，上传时改用 {upload_filename})")

        aid = self._upload(file_path, upload_filename, mime_type)
        log_info(f"    ✓ 文件上传成功！aid={aid}")
        return aid

    def upload_image(self, image_path: str) -> str:
        """上传图片，返回附件 ID"""
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")

        log_info(f"    上传图片: {os.path.basename(image_path)}")
        ext = os.path.splitext(image_path)[1].lower()
        mime_map = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"
        }
        mime = mime_map.get(ext, "image/jpeg")
        filename = os.path.basename(image_path)
        aid = self._upload(image_path, filename, mime, extra_data={"type": "image"})
        log_info(f"    ✓ 上传成功！aid={aid}")
        return aid

    def post_thread(self, title: str, message: str,
                    attachment_ids: list = None, sortid: int = None) -> PostResult:
        """发帖，返回 PostResult（帖子地址、帖子 ID、是否需审核）"""
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")

        log_info(f"    标题: {title}")
        log_info(f"    正文长度: {len(message)} 字符")

        newthread_url = (
            f"{self.base_url}/forum.php?mod=post&action=newthread&fid={self.forum_fid}"
        )
        r = self.session.get(newthread_url)
        r.raise_for_status()

        if "绑定手机号" in r.text:
            raise RuntimeError("MCBBS 要求绑定手机号，请先在网页端完成绑定")

        uid = re.search(r"discuz_uid\s*=\s*'(\d+)'", r.text)
        if not uid or uid.group(1) == "0":
            raise RuntimeError("发帖前验证失败：未登录")

        post_formhash = extract_formhash(r.text)
        post_url = (
            f"{self.base_url}/forum.php?mod=post&action=newthread"
            f"&fid={self.forum_fid}&extra=&topicsubmit=yes"
        )
        data = {
            "formhash": post_formhash,
            "posttime": str(int(time.time())),
            "wysiwyg": "1",
            "subject": title,
            "message": message,
            "usesig": "1",
            "allownoticeauthor": "1",
        }
        if sortid is not None:
            data["typeid"] = str(sortid)
        if attachment_ids:
            for aid in attachment_ids:
                data[f"attachnew[{aid}][description]"] = ""

        self.session.headers.update({"Referer": newthread_url})
        try:
            r = self.session.post(post_url, data=data, allow_redirects=False)
        finally:
            self.session.headers.pop("Referer", None)
        r.raise_for_status()

        return self._parse_post_response(r)

    def _parse_post_response(self, response) -> PostResult:
        """解析发帖响应：帖子 URL、帖子 ID，以及是否进了审核。"""
        html = getattr(response, "text", "") or ""
        headers = getattr(response, "headers", None) or {}
        needs_audit = "需要审核" in html or "通过审核后才能显示" in html

        sources = [_find_redirect(html), headers.get("Location", ""), getattr(response, "url", "")]
        for source in sources:
            tid = _extract_tid(source)
            if tid:
                url = f"{self.base_url}/thread-{tid}-1-1.html"
                log_info(f"    ✓ 发帖成功{'（需审核）' if needs_audit else ''}: {url}")
                return PostResult(url, tid, needs_audit)

        if needs_audit:
            # 审核中的帖子通常没有可用的跳转地址，帖子 ID 只能过后从「我的帖子」里补
            log_info("    ✓ 发帖成功（需审核，响应里没有帖子 ID，稍后按列表补齐）")
            return PostResult("(需审核)", None, True)

        err = re.search(r'id="messagetext"[^>]*>(.*?)</div>', html, re.S)
        if err:
            raise RuntimeError(f"发帖失败: {re.sub(r'<[^>]+>', '', err.group(1)).strip()[:200]}")

        raise RuntimeError(f"发帖结果不明: {getattr(response, 'url', '')}")

    def _ensure_formhash(self) -> str:
        """取一个可用的 formhash：登录后首页会下发，换会话或久未动手时得重取。"""
        if self.formhash:
            return self.formhash
        r = self.session.get(f"{self.base_url}/forum.php", timeout=LOGIN_TIMEOUT)
        r.raise_for_status()
        self.formhash = extract_formhash(r.text)
        return self.formhash

    def highlight_thread(self, tid: int, color: str, bold: bool = False,
                         fid: int | None = None) -> tuple[bool, str]:
        """给帖子设置标题高亮，返回 (是否成功, 失败原因)。

        Discuz 只允许版块版主操作，且对审核中（displayorder < 0）的帖子直接跳过；
        两种情况都会落到 errorhandle，这里把站点提示原样带出去，由调用方决定重试
        还是放弃。加粗取消也走同一条路：同色 + 样式位 0 = 保留颜色、去掉加粗。
        """
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")
        color_index = highlight_color_index(color)
        if not color_index:
            return False, f"色值不在 Discuz 色板内: {color}"

        forum_id = fid or self.forum_fid
        thread_url = f"{self.base_url}/thread-{tid}-1-1.html"
        payload = {
            "formhash": self._ensure_formhash(),
            "fid": str(forum_id),
            # 没有 redirect 时站点不会回 succeedhandle，成功也会被当成失败
            "redirect": thread_url,
            "handlekey": "mods",
            "moderate[]": str(tid),
            "operations[]": "highlight",
            "highlight_color": str(color_index),
            "highlight_style[1]": "1" if bold else "0",
            "highlight_style[2]": "0",
            "highlight_style[3]": "0",
            "highlight_bgcolor": "",
            "expirationhighlight": "",
        }
        action_url = (
            f"{self.base_url}/forum.php?mod=topicadmin&action=moderate"
            f"&optgroup=1&modsubmit=yes&infloat=yes&handlekey=mods&inajax=1"
        )
        # submitcheck 要求表单提交的 Referer 与站点同源（或为空），这里一并对齐
        self.session.headers.update({"Referer": thread_url})
        try:
            response = self.session.post(action_url, data=payload, timeout=LOGIN_TIMEOUT)
        finally:
            self.session.headers.pop("Referer", None)
        response.raise_for_status()

        ok, reason = _parse_moderation_result(response.text)
        if not ok:
            log_warning(f"    ⚠ 高亮未生效（tid={tid}）: {reason}")
        return ok, reason

    def fetch_my_threads(self, pages: int = 2) -> list[tuple[int, str]]:
        """拉「我的帖子」列表，返回 [(tid, 标题), ...]；待审核的帖子也在里面。

        待审核的帖子按 tid 倒序排在最前，但我们可能又发了几篇，所以留一点余量
        （默认两页）。解析不出内容时把页面落盘，方便对上站点的自定义模板。
        """
        if not self.session:
            raise RuntimeError("未登录，请先调用 login()")
        threads: list[tuple[int, str]] = []
        seen: set[int] = set()
        for page in range(1, max(pages, 1) + 1):
            url = f"{self.base_url}/home.php?mod=space&do=thread&view=me&type=thread"
            if page > 1:
                url += f"&page={page}"
            response = self.session.get(url, timeout=LOGIN_TIMEOUT)
            response.raise_for_status()
            page_threads = _parse_my_threads(response.text)
            if not page_threads:
                if page == 1:
                    path = _dump_threads_html(response.text)
                    log_warning("    ⚠ 「我的帖子」没有解析出任何帖子（页面已存: %s）", path or "落盘失败")
                break
            for tid, title in page_threads:
                if tid not in seen:
                    seen.add(tid)
                    threads.append((tid, title))
        return threads

    def process_pending_highlights(self, records_path: str) -> dict:
        """补上待高亮的帖子：先匿名探测过审，需要时才登录动手。

        站点对审核中的帖子拒绝高亮，所以探测这一步必须先做：不登录就能排除掉
        绝大多数「还没过审」的项，省掉一轮验证码。返回统计供日志汇总。
        """
        stats = {"pending": 0, "highlighted": 0, "unbolded": 0, "gave_up": 0, "failed": 0}
        records = load_thread_records(records_path)
        pending = {stem: rec for stem, rec in records.items() if rec.get("state") == "pending"}
        if not pending:
            return stats
        stats["pending"] = len(pending)

        missing = {stem: rec for stem, rec in pending.items() if not rec.get("tid")}
        if missing:
            self._resolve_tids(records, missing)

        now = time.time()
        ready: list[str] = []
        for stem, rec in pending.items():
            tid = rec.get("tid")
            if not tid:
                if self._count_attempt(rec, "「我的帖子」里还没找到这篇帖子", now):
                    stats["gave_up"] += 1
                continue
            visible = thread_is_public(self.base_url, int(tid))
            if visible is None:
                rec["last_error"] = "过审探测失败（网络异常，下轮重试）"
                rec["updated_at"] = _timestamp()
                continue
            if not visible:
                # 还在审核里不算一次失败：审核是人工的，多久都有可能
                if self._expired(rec, now):
                    rec["state"] = "gave_up"
                    rec["last_error"] = "等待审核超时，已放弃"
                    rec["updated_at"] = _timestamp()
                    stats["gave_up"] += 1
                else:
                    rec["last_error"] = "仍在审核中"
                continue
            ready.append(stem)

        try:
            if ready:
                if not self.session:
                    self.login()
                for stem in ready:
                    rec = records[stem]
                    try:
                        ok, reason = self.highlight_thread(
                            int(rec["tid"]), rec.get("color", ""),
                            bold=bool(rec.get("bold")), fid=rec.get("fid"),
                        )
                    except requests.RequestException as e:
                        ok, reason = False, f"高亮请求失败: {e}"
                    if not ok:
                        if self._count_attempt(rec, reason, now):
                            stats["gave_up"] += 1
                        else:
                            stats["failed"] += 1
                        continue
                    rec["state"] = "done"
                    rec["highlighted_at"] = _timestamp()
                    rec["updated_at"] = _timestamp()
                    rec["last_error"] = ""
                    stats["highlighted"] += 1
                    stats["unbolded"] += self._unbold_predecessors(records, rec)
        finally:
            # 探测/登记结果无论如何都要落盘：登录失败也不该把上一轮的进展丢掉
            self._save_records(records_path, records)

        log_info("[高亮] 待补 %d 篇 | 已设置 %d | 取消加粗 %d | 放弃 %d | 失败 %d",
                 stats["pending"], stats["highlighted"], stats["unbolded"],
                 stats["gave_up"], stats["failed"])
        return stats

    def _resolve_tids(self, records: dict, missing: dict) -> None:
        """从「我的帖子」里补上发帖时没拿到的帖子 ID。

        先按标题精确匹配；匹配不上再认领列表里唯一一条「比所有已知记录都新」的
        帖子（标题被版主改过时靠这条兜底）。都不行就下轮再试。
        """
        try:
            threads = self.fetch_my_threads()
        except (requests.RequestException, RuntimeError) as e:
            log_warning("    ⚠ 拉取「我的帖子」失败: %s", e)
            return
        if not threads:
            log_warning("    ⚠ 「我的帖子」里没有任何可用条目，无法补齐帖子 ID")
            return

        by_title = {_normalize_title(title): tid for tid, title in threads}
        known = {rec.get("tid") for rec in records.values() if rec.get("tid")}
        newest = max(tid for tid, _title in threads)
        for stem, rec in missing.items():
            tid = by_title.get(_normalize_title(rec.get("title", "")))
            source = "标题匹配"
            if not tid and newest not in known:
                tid, source = newest, "列表新增兜底"
            if not tid:
                continue
            rec["tid"] = tid
            rec["url"] = f"{self.base_url}/thread-{tid}-1-1.html"
            rec["tid_source"] = source
            rec["updated_at"] = _timestamp()
            known.add(tid)
            log_info("    → 补齐帖子 ID: %s → tid=%s（%s）", stem, tid, source)

    def _unbold_predecessors(self, records: dict, current: dict) -> int:
        """新帖上色后，把同类型旧帖的加粗取消（保留颜色）。返回处理条数。

        版规要求「版本资讯最新版本加粗」，而 Discuz 的高亮没有「只对最新版生效」
        的机制：只能在新版本上色成功后，用同色 + 样式位 0 把上一版改回不加粗。
        """
        if not current.get("bold"):
            return 0
        max_attempts = int(self.highlight.get("max_attempts") or 0)
        done = 0
        for stem, rec in records.items():
            if rec is current or rec.get("tid") == current.get("tid"):
                continue
            if rec.get("news_type") != current.get("news_type"):
                continue
            if rec.get("state") != "done" or not rec.get("bold") or not rec.get("tid"):
                continue

            try:
                ok, reason = self.highlight_thread(
                    int(rec["tid"]), rec.get("color", ""), bold=False, fid=rec.get("fid"),
                )
            except requests.RequestException as e:
                ok, reason = False, f"取消加粗请求失败: {e}"
            rec["updated_at"] = _timestamp()
            if ok:
                rec["bold"] = False
                rec["last_error"] = ""
                done += 1
                log_info("    → 已取消旧帖加粗: %s（tid=%s）", stem, rec["tid"])
                continue

            rec["unbold_attempts"] = int(rec.get("unbold_attempts") or 0) + 1
            rec["last_error"] = f"取消加粗失败: {reason}"
            if max_attempts and rec["unbold_attempts"] >= max_attempts:
                rec["bold"] = False
                log_warning("    ⚠ 取消旧帖加粗多次失败，已放弃: %s（%s）", stem, reason)
            else:
                log_warning("    ⚠ 取消旧帖加粗失败，下轮重试: %s（%s）", stem, reason)
        return done

    def _count_attempt(self, rec: dict, reason: str, now: float) -> bool:
        """记一次失败尝试，返回是否该放弃（次数或等待时间超限）。"""
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
        rec["last_error"] = reason
        rec["updated_at"] = _timestamp()
        max_attempts = int(self.highlight.get("max_attempts") or 0)
        if (max_attempts and rec["attempts"] >= max_attempts) or self._expired(rec, now):
            rec["state"] = "gave_up"
            return True
        return False

    def _expired(self, rec: dict, now: float) -> bool:
        """记录是否已经超过等待上限（按发帖时间算）。"""
        max_hours = self.highlight.get("max_age_hours") or 0
        try:
            posted = time.mktime(time.strptime(rec.get("posted_at") or "", "%Y-%m-%dT%H:%M:%S"))
        except (TypeError, ValueError):
            return False
        return bool(max_hours) and (now - posted) > max_hours * 3600

    def _save_records(self, records_path: str, records: dict) -> None:
        try:
            save_thread_records(records_path, records)
        except OSError:
            log_warning("    ⚠ 帖子记录写入失败: %s", records_path, exc_info=True)

    def _register_highlight(self, stem: str, news_dir: str, result: PostResult,
                            news_type: str, title: str) -> None:
        """登记这篇帖子的高亮意图，发帖时不动手。

        站点对新主题一律走审核，而审核中的帖子在 Discuz 里设置不了高亮，所以统一
        先落一条 pending 记录，交给每轮的补高亮流程（匿名探测过审 → 登录 → 设置）。
        """
        if not self.highlight.get("enabled"):
            return
        color = (self.highlight.get("colors") or {}).get(news_type, "")
        if not color:
            log_debug("    → 类型 %s 没有配置高亮色，跳过登记", news_type)
            return

        path = thread_records_path(news_dir)
        records = load_thread_records(path)
        previous = records.get(stem) or {}
        now = _timestamp()
        records[stem] = {
            "tid": result.tid or previous.get("tid"),
            "fid": self.forum_fid,
            "url": result.url if result.url.startswith("http") else previous.get("url", ""),
            "title": title,
            "news_type": news_type,
            "color": color,
            "bold": news_type in (self.highlight.get("bold_types") or []),
            "state": "pending",
            "posted_at": previous.get("posted_at") or now,
            "updated_at": now,
            "attempts": previous.get("attempts", 0),
            "last_error": "",
        }
        try:
            save_thread_records(path, records)
        except OSError:
            log_warning("    ⚠ 高亮登记写入失败（不影响发帖）", exc_info=True)
            return

        tid_hint = f"tid={result.tid}" if result.tid else "帖子 ID 待从列表补齐"
        bold_hint = "（加粗）" if records[stem]["bold"] else ""
        log_info(f"    → 已登记待补高亮: {news_type} {color}{bold_hint}，{tid_hint}")

    def post_news_file(self, stem: str, txt_path: str, json_path: str,
                       news_dir: str, no_image: bool = False,
                       attach_json: bool = True) -> str:
        """
        发布单个新闻文件（txt + json + 可选图片 + 可选 JSON 附件）

        Args:
            attach_json: 是否将 JSON 文件作为附件上传并插入下载链接

        Returns:
            帖子 URL
        """
        # 加载元数据
        with open(json_path, encoding="utf-8") as f:
            meta = json.load(f)
        title = meta.get("translated_title", "").strip()
        if not title:
            title = meta.get("title", "").strip()
        if not title:
            raise ValueError(f"{json_path} 中找不到 title 字段")
        title = title + " [AI翻译]"

        with open(txt_path, encoding="utf-8") as f:
            message = f.read().strip()
        if not message:
            raise ValueError(f"{txt_path} 内容为空")

        # 上传图片
        attachment_ids = []
        if not no_image:
            img_path = find_image(news_dir, stem)
            if img_path:
                try:
                    aid = self.upload_image(img_path)
                    message = insert_image_bbcode(message, aid)
                    attachment_ids.append(aid)
                except Exception as e:
                    log_warning(f"    ⚠ 图片上传失败，继续无图发帖: {e}")

        # 上传 JSON 附件并追加到正文末尾
        if attach_json and os.path.exists(json_path):
            try:
                json_aid = self.upload_file(json_path, mime_type="text/plain")  # Discuz 不接受 application/json
                message += f"\n\n[attach]{json_aid}[/attach]"
                attachment_ids.append(json_aid)
            except Exception as e:
                log_warning(f"    ⚠ JSON 附件上传失败，跳过: {e}")

        # 检测分类（同时检测英文和中文标题，避免关键词丢失）
        original_title = meta.get("title", "").strip()
        # 先用英文标题检测
        module_type = detect_module_type(message, original_title) if original_title else None
        # 如果英文标题检测失败，再用中文标题检测
        if not module_type:
            module_type = detect_module_type(message, title)
        # 如果无法检测到类型，使用 "normal" 作为默认分类
        if not module_type:
            module_type = "normal"
        sortid = self.sortid_map.get(module_type)
        if sortid:
            cat_name = _CATEGORY_GROUP.get(module_type, "未知")
            log_info(f"    分类: {cat_name} (sortid={sortid})")
        else:
            log_warning(f"    ⚠ 警告: 未找到分类 {module_type} 的 sortid 配置")

        result = self.post_thread(title, message,
                                  attachment_ids=attachment_ids, sortid=sortid)
        self._register_highlight(stem, news_dir, result, module_type, title)
        return result.url


# ── 状态管理 ─────────────────────────────────────────
# load_posted / save_posted 已上移到 utils（B站 侧也要用），本模块从 utils 导入，
# `from poster import load_posted` 的老调用方与测试仍然有效。


# ── CLI 入口 ─────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="MCBBS 新闻自动发帖（含图片上传）")
    parser.add_argument("files", nargs="*", help="指定要发布的文件（stem）")
    parser.add_argument("--dry-run", action="store_true", help="预览模式")
    parser.add_argument("--dir", default="./output", help="新闻目录")
    parser.add_argument("--no-image", action="store_true", help="跳过图片上传")
    parser.add_argument("--no-json", action="store_true", help="跳过 JSON 附件上传")
    parser.add_argument("--fid", type=int, help="覆盖版块 ID")
    args = parser.parse_args()

    config = load_poster_config()
    if args.fid is not None:
        config["forum_fid"] = args.fid

    news_dir = args.dir
    state_file = os.path.join(news_dir, ".posted.json")

    if not config["username"] or not config["password"]:
        log_error("[!] 未配置 MCBBS 账号密码！请设置 config.json 中的 mcbbs 部分")
        sys.exit(1)

    # 扫描新闻文件
    if not os.path.isdir(news_dir):
        log_error(f"[!] 新闻目录不存在: {news_dir}")
        sys.exit(1)

    all_news = []
    for txt_path in sorted(glob.glob(os.path.join(news_dir, "*.txt"))):
        stem = os.path.splitext(os.path.basename(txt_path))[0]
        json_path = os.path.join(news_dir, stem + ".json")
        if os.path.exists(json_path):
            all_news.append((stem, txt_path, json_path))
        else:
            log_warning(f"    ⚠ 跳过 {stem}: 找不到 .json")

    if not all_news:
        log_info("[!] 没有找到新闻文件")
        sys.exit(0)

    posted = load_posted(state_file)
    if args.files:
        pending = [(s, t, j) for s, t, j in all_news if s in set(args.files)]
    else:
        pending = [(s, t, j) for s, t, j in all_news if s not in posted]

    if not pending:
        log_info("[!] 没有需要发布的新闻")
        sys.exit(0)

    log_info(f"[*] 待发布: {len(pending)} 个")

    if args.dry_run:
        for stem, txt_path, json_path in pending:
            with open(json_path, encoding="utf-8") as f:
                meta = json.load(f)
            title = meta.get("title") or meta.get("translated_title", "")
            with open(txt_path, encoding="utf-8") as f:
                message = f.read().strip()
            img_path = find_image(news_dir, stem)
            module_type = detect_module_type(message, title)
            cat = _CATEGORY_GROUP.get(module_type, "未分类") if module_type else "未分类"
            log_info(f"\n  文件: {stem}")
            log_info(f"  标题: {title}")
            log_info(f"  分类: {cat}")
            log_info(f"  图片: {'✓ ' + os.path.basename(img_path) if img_path else '✗ 无'}")
        log_info(f"\n[Dry Run] 共 {len(pending)} 个待发布")
        sys.exit(0)

    poster = MCBBSPoster(config)
    try:
        poster.login()
    except Exception as e:
        log_error(f"[!] 登录失败: {e}")
        sys.exit(1)

    success = 0
    failed = 0
    for stem, txt_path, json_path in pending:
        try:
            log_info(f"\n[*] 发布: {stem}")
            poster.post_news_file(stem, txt_path, json_path, news_dir,
                                 no_image=args.no_image, attach_json=not args.no_json)
            posted.add(stem)
            save_posted(state_file, posted)
            success += 1
            time.sleep(2)
        except Exception as e:
            log_error(f"    ✗ 发布失败: {e}")
            failed += 1

    log_info(f"\n{'=' * 50}")
    log_info(f"  完成！成功: {success}, 失败: {failed}")
    log_info(f"{'=' * 50}")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    setup_logging()
    main()
