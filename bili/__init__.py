"""bili — B站专栏相关的模块（投稿器 + 两套正文渲染器 + WBI 签名）

专栏有两套互斥的正文形态，各有自己的渲染器，投稿时二选一：
  html.py    老接口 `creative/draft/addupdate` 收的 HTML 正文（已真机验证）
  opus.py    新版编辑器 `dynamic/feed/article/draft/add` 收的 eva3 段落正文

放在一个包里是为了让「专栏」这件事的边界看得见：根目录只留通用模块，
投稿链路的改动范围一眼可数。共用工具仍在根目录的 utils.py。
"""

from urllib.parse import urlsplit

# 唯一保留超链接的地址：项目署名（主机 + 路径前缀）。
# 实测（2026-11-15 真机发布）B站 图文里点其余外链会跳到「离开 B站」提示页，读者看到的是
# 一个拦截页而不是内容，所以除署名外一律降级成纯文本；两种正文模型共用这一份策略。
#
# 用 host + path 判定而不是子串匹配：正文里的链接来自抓取内容，`?x=github.com/jiubook/MCTTK`
# 这种写法也能命中的话，等于给「看起来像本项目的钓鱼地址」留了一个可点的口子。
SIGNATURE_HOST = "github.com"
SIGNATURE_PATH = "/jiubook/mcttk"


def is_signature_link(url: str) -> bool:
    """是否是必须保留超链接的署名地址（其余外链统一降级成纯文本）。"""
    parts = urlsplit(url or "")
    if (parts.hostname or "").lower() != SIGNATURE_HOST:
        return False
    return parts.path.lower().startswith(SIGNATURE_PATH)


def degrade_link(label: str, url: str) -> str:
    """非署名链接的纯文本形态：`文字（URL）`。

    地址要留在正文里——读者至少能看见、能复制（下载地址、漏洞追踪器这类信息丢了比
    不可点更糟）；链接文字本身就是地址时不重复写成「URL（URL）」。
    """
    label = (label or "").strip()
    url = (url or "").strip()
    if not url or not label or label == url:
        return url or label
    return f"{label}（{url}）"
