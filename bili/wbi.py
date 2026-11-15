#!/usr/bin/env python3
"""bili/wbi.py — WBI 签名（B站 web 接口的 w_rid / wts 风控参数）

新版创作中心接口（`x/dynamic/feed/*`）在查询串上带 `wts`（秒级时间戳）与 `w_rid`
（签名），校验不过会被风控挡成 412 / -352。签名算法来自浏览器端 `encWbi`：

  1. 用 `nav` 返回的 `data.wbi_img.img_url` / `sub_url` 取两个文件名；
  2. 拼成 64 字符基础串，按固定的混淆表重排后取前 32 位，得到 mixin key；
  3. 查询参数剔除值里的 `!'()*`，按键名排序后 urlencode，接上 mixin key 再 md5。

混淆表是 2023-07 起固定的一张下标表，官方换表时整体签名会失效，届时只需更新
`_MIXIN_KEY_TAB` 这一处；为此这里把「取键」与「签名」拆成两个纯函数，方便用固定
向量做单元测试（不联网）。
"""

import hashlib
import re
import time
from urllib.parse import urlencode

# 官方混淆表：64 个下标，重排后取前 32 位作为 mixin key
_MIXIN_KEY_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

# 官方前端会把这些字符从参数值里剔掉再参与签名（历史兼容），我们照做，
# 否则带括号/感叹号的模块文案会让签名与浏览器不一致
_UNSAFE_CHARS = re.compile(r"[!'()*]")


def _file_stem(url: str) -> str:
    """`https://i0.hdslb.com/bfs/wbi/7cd0...c.png?v=1` → `7cd0...c`。"""
    path = str(url or "").split("?", 1)[0]
    return path.rsplit("/", 1)[-1].split(".", 1)[0]


def image_keys(wbi_img: dict | None) -> tuple[str, str]:
    """从 `nav` 的 `data.wbi_img` 取（img_key, sub_key）。缺字段时返回空串。"""
    if not isinstance(wbi_img, dict):
        return "", ""
    return _file_stem(wbi_img.get("img_url") or ""), _file_stem(wbi_img.get("sub_url") or "")


def mixin_key(img_key: str, sub_key: str) -> str:
    """两个文件名 → 32 位 mixin key。表里下标有可能越界（键长不足），跳过而不是抛错。"""
    raw = f"{img_key}{sub_key}"
    picked = [raw[index] for index in _MIXIN_KEY_TAB if index < len(raw)]
    return "".join(picked)[:32]


def sign_params(params: dict, img_key: str, sub_key: str, *, timestamp: int | None = None) -> dict:
    """给查询参数补 `wts` / `w_rid`，返回新字典（不改入参）。

    时间戳可注入，测试里就不用跟着真实时钟跑；`wts` 本身也参与签名，
    所以必须在算 w_rid 之前塞进去。
    """
    signed = {name: str(value) for name, value in params.items()}
    signed["wts"] = str(int(timestamp if timestamp is not None else time.time()))
    query = urlencode(
        [(name, _UNSAFE_CHARS.sub("", value)) for name, value in sorted(signed.items())],
    )
    # 这里的 md5 不是我们挑的哈希：B站 前端签名算法就是 md5(query + mixin key)，
    # 换成更强的摘要反而通不过校验（S324 关掉的正是「别用 md5」这条通用建议）
    signed["w_rid"] = hashlib.md5((query + mixin_key(img_key, sub_key)).encode()).hexdigest()  # noqa: S324
    return signed
