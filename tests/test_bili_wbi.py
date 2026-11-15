"""test_bili_wbi.py — WBI 签名（w_rid / wts）

签名算法是纯字符串运算，这里用固定键与固定时间戳钉住：mixin key 的取法、参数值里
`!'()*` 的剔除、按键名排序、以及 wts 参与签名。真实接口能否通过校验只能靠实机，
但算法一改这里必然红。
"""
import hashlib
from urllib.parse import urlencode

from bili.wbi import _MIXIN_KEY_TAB, image_keys, mixin_key, sign_params

IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"
TS = 1700000000


class TestImageKeys:
    def test_extracts_file_names(self):
        wbi = {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        }
        assert image_keys(wbi) == (IMG_KEY, SUB_KEY)

    def test_query_string_ignored(self):
        wbi = {"img_url": "https://i0.hdslb.com/bfs/wbi/abc.png?v=2", "sub_url": "https://x/def.jpg"}
        assert image_keys(wbi) == ("abc", "def")

    def test_missing_fields(self):
        assert image_keys(None) == ("", "")
        assert image_keys({}) == ("", "")


class TestMixinKey:
    def test_length_is_32(self):
        assert len(mixin_key(IMG_KEY, SUB_KEY)) == 32

    def test_is_tab_permutation_of_raw(self):
        raw = IMG_KEY + SUB_KEY
        expected = "".join(raw[index] for index in _MIXIN_KEY_TAB[:32])
        assert mixin_key(IMG_KEY, SUB_KEY) == expected

    def test_short_keys_do_not_raise(self):
        """键长度不足时宁可变短也不要抛异常（反正签名会失败，但有日志可看）。"""
        assert mixin_key("ab", "") == "ab"


class TestSignParams:
    def _expected_w_rid(self, params: dict, timestamp: int) -> str:
        """独立重算一遍签名（不调用被测代码），避免自证。"""
        merged = dict(params)
        merged["wts"] = timestamp
        cleaned = {k: str(v).replace("!", "").replace("'", "").replace("(", "").replace(")", "")
                   .replace("*", "") for k, v in merged.items()}
        query = urlencode(sorted(cleaned.items()))
        raw = IMG_KEY + SUB_KEY
        key = "".join(raw[index] for index in _MIXIN_KEY_TAB)[:32]
        return hashlib.md5((query + key).encode()).hexdigest()  # noqa: S324 - 与 B站 前端算法一致

    def test_adds_wts_and_w_rid(self):
        signed = sign_params({"foo": "bar"}, IMG_KEY, SUB_KEY, timestamp=TS)
        assert signed["wts"] == str(TS)
        assert signed["w_rid"] == self._expected_w_rid({"foo": "bar"}, TS)
        assert len(signed["w_rid"]) == 32

    def test_input_not_mutated(self):
        params = {"foo": "bar"}
        sign_params(params, IMG_KEY, SUB_KEY, timestamp=TS)
        assert params == {"foo": "bar"}

    def test_values_are_filtered_before_signing(self):
        signed = sign_params({"q": "a!b'c(d)e*f"}, IMG_KEY, SUB_KEY, timestamp=TS)
        assert signed["q"] == "a!b'c(d)e*f"  # 返回值保持原样，只有签名时剔除
        assert signed["w_rid"] == self._expected_w_rid({"q": "a!b'c(d)e*f"}, TS)

    def test_order_independent(self):
        first = sign_params({"a": "1", "b": "2"}, IMG_KEY, SUB_KEY, timestamp=TS)
        second = sign_params({"b": "2", "a": "1"}, IMG_KEY, SUB_KEY, timestamp=TS)
        assert first["w_rid"] == second["w_rid"]

    def test_timestamp_changes_signature(self):
        first = sign_params({"a": "1"}, IMG_KEY, SUB_KEY, timestamp=TS)
        second = sign_params({"a": "1"}, IMG_KEY, SUB_KEY, timestamp=TS + 1)
        assert first["w_rid"] != second["w_rid"]

    def test_numeric_values_stringified(self):
        signed = sign_params({"category": 26}, IMG_KEY, SUB_KEY, timestamp=TS)
        assert signed["category"] == "26"
