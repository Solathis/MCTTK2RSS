#!/usr/bin/env python3
"""
tools/bili_login.py — 扫码登录 B站，把登录 cookie 写进本地文件

B站 扫码登录走三个公开接口：
  generate 拿 qrcode_key 与扫码链接 → poll 轮询扫码状态 → nav 校验登录态。
扫完把 SESSDATA / bili_jct 等写进 config/bili_cookies.json（.gitignore 已忽略），
bili_poster 会从这里读凭据；也可以用 BILI_SESSDATA / BILI_JCT 环境变量。

这样就不用教用户开 F12 复制 cookie——扫码一次，之后自动续用，直到登录态过期。

用法：
  uv sync --extra login                       # 首次：装二维码渲染依赖（qrcode）
  uv run python tools/bili_login.py           # 打印二维码 → 手机扫码 → 保存 cookie
  uv run python tools/bili_login.py --qr block    # 终端认不出半块字符时用大号图
  uv run python tools/bili_login.py --timeout 300
  uv run python tools/bili_login.py --out D:/tmp/bili_cookies.json
"""

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from bili_poster import (  # noqa: E402
    NAV_URL,
    BiliError,
    build_session,
    decode_json,
    ensure_ok,
    load_bili_config,
)
from log_setup import log_error, log_info, setup_logging  # noqa: E402

PASSPORT_BASE = "https://passport.bilibili.com"
GENERATE_URL = f"{PASSPORT_BASE}/x/passport-login/web/qrcode/generate"
POLL_URL = f"{PASSPORT_BASE}/x/passport-login/web/qrcode/poll"

# 投稿只需要前两个，另外两个是 B站 的账号标识，顺手存下来便于排障
WANTED_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")
REQUIRED_COOKIES = ("SESSDATA", "bili_jct")

DEFAULT_COOKIE_PATH = "config/bili_cookies.json"

# poll 接口在 data.code 里表达扫码进度：这两个是「继续等」，其余都是终止状态
SCAN_WAITING = {
    86101: "等待扫码…",
    86090: "已扫码，请在手机上点「确认登录」",
}
SCAN_FATAL = {
    86038: "二维码已过期，请重新运行本工具",
    86083: "扫码被拒绝，请重新运行本工具",
}


class LoginError(RuntimeError):
    """扫码登录流程失败（二维码过期、超时、拿不到 cookie 等）。"""


# ── 纯函数 ───────────────────────────────────────────

# 半块字符：一个字符表示「上下两个模块」，高度砍半，扫得动还省屏
_HALF_BLOCKS = {
    (False, False): " ",
    (True, False): "▀",
    (False, True): "▄",
    (True, True): "█",
}
_ANSI_RESET = "\x1b[0m"


def _qr_matrix(url: str) -> list[list[bool]]:
    """算出二维码模块矩阵（含 1 模块宽的静区）。

    容错级别取 L（约 7%）：二维码是让手机扫屏幕上的干净图像，不是印在会磨损的纸上，
    L 能让矩阵小一档（同样内容 51 → 43 模块），终端里更紧凑。
    """
    try:
        import qrcode
    except ImportError as e:  # pragma: no cover - 依赖装了就不会走到
        raise LoginError("需要 qrcode 才能打印二维码：先运行 uv sync --extra login") from e

    qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    return [[bool(cell) for cell in row] for row in qr.get_matrix()]


def render_qr_text(url: str, *, style: str = "compact", color: bool = False) -> str:
    """把二维码渲染成控制台可读的文本。

    style="compact"（默认）：一个字符画上下两个模块，宽高各减半，成品约
    「22 行 × 43 字符」。终端字符是竖长条，一行一模块会把码压扁扫不出来，
    所以用半块字符把两行模块压进一行。
    style="block"：每个模块两个字符宽，成品约「44 行 × 86 字符」，留给认不出
    半块字符的终端（老式等宽字体/乱码时用）。

    color=True 时加 ANSI 前景/背景色，强制「黑模块 + 白底」，
    这样深色主题终端上也是正确极性（不加颜色时模块会随主题反色）。
    """
    matrix = _qr_matrix(url)
    if style == "block":
        return "\n".join("".join("██" if cell else "  " for cell in row) for row in matrix)

    lines = []
    for index in range(0, len(matrix), 2):
        top = matrix[index]
        bottom = matrix[index + 1] if index + 1 < len(matrix) else [False] * len(top)
        if not color:
            lines.append("".join(_HALF_BLOCKS[(bool(t), bool(b))] for t, b in zip(top, bottom, strict=True)))
            continue
        parts = []
        for t, b in zip(top, bottom, strict=True):
            # 前景=上半模块，背景=下半模块；固定用 256 色黑白，不吃终端主题
            foreground = 0 if t else 15
            background = 0 if b else 15
            parts.append(f"\x1b[38;5;{foreground};48;5;{background}m▀")
        lines.append("".join(parts) + _ANSI_RESET)
    return "\n".join(lines)


def parse_cookies_from_url(url: str) -> dict:
    """从 crossDomain 回调地址里取 cookie 原值。

    不能用 parse_qs：它会 URL 解码，而 SESSDATA 里的 %2C 这类转义是 cookie 值的一部分，
    解码后写回去就失效了。所以按 & 拆分、只认已知键名，值保持原样。
    """
    query = urlsplit(url).query
    found = {}
    for part in query.split("&"):
        name, sep, value = part.partition("=")
        if sep and name in WANTED_COOKIES and value:
            found[name] = value
    return found


def merge_cookies(session_cookies: dict | None, url_cookies: dict | None) -> dict:
    """合并 cookie：会话 jar 优先（requests 存的就是服务端下发原值），URL 参数只补缺。"""
    merged = {name: value for name, value in (session_cookies or {}).items()
              if name in WANTED_COOKIES and value}
    for name, value in (url_cookies or {}).items():
        merged.setdefault(name, value)
    return merged


def mask_secret(value: str) -> str:
    """日志里只留前 4 位：够确认「换没换」，又不泄露可用的登录态。"""
    if not value:
        return "(空)"
    return value[:4] + "***" if len(value) > 4 else "***"


def _session_cookies(session) -> dict:
    """把 requests 的 CookieJar（或测试里的 dict）摊平成普通 dict。"""
    jar = getattr(session, "cookies", None)
    if jar is None:
        return {}
    if isinstance(jar, dict):
        return dict(jar)
    with contextlib.suppress(TypeError, AttributeError):
        return {cookie.name: cookie.value for cookie in jar}
    return {}


# ── 接口步骤 ─────────────────────────────────────────

def fetch_qr(session, *, request_timeout: int = 30, verify: bool = True) -> tuple[str, str]:
    """生成扫码链接与轮询用的 qrcode_key。"""
    resp = session.get(GENERATE_URL, timeout=request_timeout, verify=verify)
    payload = ensure_ok(decode_json(resp, "生成二维码"), "生成二维码")
    data = payload.get("data") or {}
    url, key = data.get("url"), data.get("qrcode_key")
    if not url or not key:
        raise LoginError("生成二维码失败：接口没有返回扫码链接")
    return url, key


def wait_for_login(session, qrcode_key: str, *, timeout: float = 180.0, interval: float = 2.0,
                   request_timeout: int = 30, verify: bool = True, on_status=None,
                   sleep=time.sleep) -> dict:
    """轮询扫码状态，成功时返回 poll 的 data 负载（含 crossDomain 回调地址）。"""
    started = time.monotonic()
    last_state = None
    while time.monotonic() - started < timeout:
        resp = session.get(
            POLL_URL,
            params={"source": "main-fe-header", "qrcode_key": qrcode_key},
            timeout=request_timeout,
            verify=verify,
        )
        payload = ensure_ok(decode_json(resp, "查询扫码状态"), "查询扫码状态")
        data = payload.get("data") or {}
        state = data.get("code")
        if state == 0:
            return data
        if state not in SCAN_WAITING:
            message = SCAN_FATAL.get(state) or data.get("message") or ""
            raise LoginError(f"扫码失败: code={state} {message}".strip())
        # 同一状态只在变化时提示一次，避免每 2 秒刷屏
        if state != last_state and on_status is not None:
            on_status(SCAN_WAITING[state])
        last_state = state
        sleep(interval)
    raise LoginError(f"等待扫码超时（{timeout:.0f} 秒），请重新运行本工具")


def verify_login(session, *, request_timeout: int = 30, verify: bool = True) -> dict:
    """用刚拿到的 cookie 调 nav，确认真的登录成功。"""
    resp = session.get(NAV_URL, timeout=request_timeout, verify=verify)
    payload = ensure_ok(decode_json(resp, "登录校验"), "登录校验")
    data = payload.get("data") or {}
    if not data.get("isLogin"):
        raise LoginError("扫码成功但校验登录态失败，请重新运行本工具")
    return data


def save_cookies(path: Path, cookies: dict) -> Path:
    """把 cookie 落盘（只写已知键）。文件权限尽量收到 600，仅本人可读。"""
    payload = {name: value for name, value in cookies.items() if name in WANTED_COOKIES and value}
    if not payload:
        raise LoginError("没有取到任何 cookie，未写入文件")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)  # Windows 上基本无效，POSIX 上能挡住同机其它用户
    return path


# ── 流程编排 ─────────────────────────────────────────

def run_login(session, *, out_path: Path, timeout: float = 180.0, interval: float = 2.0,
              request_timeout: int = 30, verify: bool = True, sleep=time.sleep,
              printer=print, on_status=None, qr_style: str = "compact",
              qr_color: bool = False) -> dict:
    """完整流程：取二维码 → 打印 → 轮询 → 校验 → 落盘。返回账号信息与文件路径。"""
    qr_url, qr_key = fetch_qr(session, request_timeout=request_timeout, verify=verify)
    printer(render_qr_text(qr_url, style=qr_style, color=qr_color))
    printer("")
    printer(f"用 B站 App 扫码登录；二维码显示不全时加 --qr block 重跑，"
            f"或直接用浏览器打开：{qr_url}")

    data = wait_for_login(
        session, qr_key, timeout=timeout, interval=interval,
        request_timeout=request_timeout, verify=verify, on_status=on_status, sleep=sleep,
    )
    cookies = merge_cookies(_session_cookies(session), parse_cookies_from_url(data.get("url") or ""))
    missing = [name for name in REQUIRED_COOKIES if not cookies.get(name)]
    if missing:
        raise LoginError(f"扫码成功但没拿到 {', '.join(missing)}，请重试")

    account = verify_login(session, request_timeout=request_timeout, verify=verify)
    saved = save_cookies(Path(out_path), cookies)
    return {
        "out_path": saved,
        "cookies": cookies,
        "mid": account.get("mid"),
        "uname": account.get("uname") or "",
    }


def _default_cookie_path(bili_config: dict) -> Path:
    configured = str(bili_config.get("cookie_file") or "").strip() or DEFAULT_COOKIE_PATH
    path = Path(configured)
    return path if path.is_absolute() else PROJECT_DIR / path


def _force_utf8_stdout() -> None:
    """二维码是方块字符，控制台编码不对会显示成乱码（chcp 936 时尤其明显）。"""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="扫码登录 B站并把登录 cookie 写入本地文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  uv sync --extra login\n"
            "  python tools/bili_login.py\n"
            "  python tools/bili_login.py --timeout 300\n"
        ),
    )
    parser.add_argument("--out", help=f"cookie 输出路径（默认取配置里的 cookie_file，或 {DEFAULT_COOKIE_PATH}）")
    parser.add_argument("--config", help="指定配置文件路径")
    parser.add_argument("--timeout", type=float, default=180.0, help="等待扫码的最长秒数（默认 180）")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数（默认 2）")
    parser.add_argument(
        "--qr", choices=("compact", "block"), default="compact",
        help="二维码样式：compact 半高（默认，约 20 行）；block 逐行（约 40 行，终端认不出半块字符时用）",
    )
    args = parser.parse_args()

    # 二维码直接打到终端，不要进日志文件（否则日志里全是方块字符）
    setup_logging(capture_streams=False)
    _force_utf8_stdout()

    bili_config, http_config = load_bili_config(args.config)
    out_path = Path(args.out) if args.out else _default_cookie_path(bili_config)
    request_timeout = int(http_config.get("timeout") or 30)
    verify = http_config.get("verify_ssl", True)

    log_info("[登录] 目标文件: %s", out_path)
    session = build_session(http_config)
    try:
        result = run_login(
            session,
            out_path=out_path,
            timeout=args.timeout,
            interval=args.interval,
            request_timeout=request_timeout,
            verify=verify,
            on_status=lambda text: log_info("[登录] %s", text),
            qr_style=args.qr,
            # 只在真实终端上上色：管道/重定向时留下转义字符只会污染输出
            qr_color=sys.stdout.isatty(),
        )
    except (LoginError, BiliError) as e:
        log_error("[登录] 失败: %s", e)
        return 1

    log_info("[登录] ✓ 已登录 B站: %s（mid=%s）", result["uname"], result["mid"])
    log_info("[登录] ✓ cookie 已保存: %s（SESSDATA=%s）",
             result["out_path"], mask_secret(result["cookies"]["SESSDATA"]))
    log_info("[登录] 下一步：config.json 里把 bilibili.enabled 设为 true；"
             "先保持 submit=\"draft\"，投一篇草稿看过排版再考虑自动提交")
    return 0


if __name__ == "__main__":
    sys.exit(main())
