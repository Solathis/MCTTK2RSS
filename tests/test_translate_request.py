"""translate_text 请求构造与结构化输出降级测试。

背景：DeepSeek 等 OpenAI 兼容网关不支持 ``response_format={"type": "json_schema"}``，
会直接返回 400（"This response_format type is unavailable now"），
旧实现只打印 ``400 Client Error`` 并重试，导致整批译文全部丢失。
"""

import json

import pytest
import requests

import scraper


def _cfg(**overrides):
    cfg = {
        "openai_compat": {
            "host": "api.fake-host.test",
            "endpoint": "/v1/chat/completions",
            "api_key": "sk-test",
            "model": "test-model",
            "json_schema": True,
            "max_tokens": 100,
            "timeout": 10,
        },
        "http": {"verify_ssl": False, "proxies": {}, "user_agent": "ua", "accept": "*/*"},
        "retry": {"translation": {"max_retries": 2}},
        "prompts": {"translate_text_default": "把输入翻译成简体中文"},
    }
    for key, value in overrides.items():
        cfg[key] = {**cfg.get(key, {}), **value}
    return cfg


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = json.dumps(self._payload, ensure_ascii=False)
        self.headers = {"content-type": "application/json"}
        self.url = "https://api.fake-host.test/v1/chat/completions"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)


def _ok(content):
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def _bad_request(message):
    return FakeResponse(400, {"error": {"message": message, "type": "invalid_request_error"}})


JSON_SCHEMA_400 = "This response_format type is unavailable now (request_id: 123)"
JSON_OBJECT_400 = (
    "Prompt must contain the word 'json' in some form to use "
    "'response_format' of type 'json_object'."
)

SCHEMA = {
    "type": "object",
    "properties": {"translations": {"type": "array"}},
    "required": ["translations"],
}


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setattr(scraper, "_REJECTED_RESPONSE_FORMATS", set())
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)


def _install_responses(monkeypatch, responses):
    """按顺序返回预设响应，并记录每次请求的 payload。"""
    payloads = []

    def fake_post(url, json=None, headers=None, **kwargs):
        payloads.append(json)
        return responses.pop(0)

    monkeypatch.setattr(scraper.requests, "post", fake_post)
    return payloads


class TestResponseFormatFallback:
    def test_falls_back_to_json_object(self, monkeypatch):
        payloads = _install_responses(
            monkeypatch, [_bad_request(JSON_SCHEMA_400), _ok('{"translations": []}')]
        )

        result = scraper.translate_text(
            "hello", config=_cfg(), glossary={}, use_glossary=False,
            response_schema=SCHEMA,
        )

        assert result == '{"translations": []}'
        assert payloads[0]["response_format"]["type"] == "json_schema"
        assert payloads[1]["response_format"] == {"type": "json_object"}

    def test_json_object_prompt_mentions_json(self, monkeypatch):
        payloads = _install_responses(
            monkeypatch, [_bad_request(JSON_SCHEMA_400), _ok("{}")]
        )

        scraper.translate_text(
            "hello", config=_cfg(), glossary={}, use_glossary=False,
            response_schema=SCHEMA,
        )

        # DeepSeek 要求 json_object 模式下提示词出现 json 字样
        assert "JSON" in payloads[1]["messages"][0]["content"]

    def test_falls_back_to_plain_prompt_when_all_formats_rejected(self, monkeypatch):
        payloads = _install_responses(
            monkeypatch,
            [_bad_request(JSON_SCHEMA_400), _bad_request(JSON_OBJECT_400), _ok("{}")],
        )

        result = scraper.translate_text(
            "hello", config=_cfg(), glossary={}, use_glossary=False,
            response_schema=SCHEMA,
        )

        assert result == "{}"
        assert "response_format" not in payloads[2]

    def test_rejection_is_remembered_for_next_call(self, monkeypatch):
        _install_responses(monkeypatch, [_bad_request(JSON_SCHEMA_400), _ok("{}")])
        cfg = _cfg()
        scraper.translate_text(
            "hello", config=cfg, glossary={}, use_glossary=False, response_schema=SCHEMA,
        )

        payloads = _install_responses(monkeypatch, [_ok("{}")])
        scraper.translate_text(
            "hello", config=cfg, glossary={}, use_glossary=False, response_schema=SCHEMA,
        )

        # 第二次调用不再白白撞一次 json_schema 的 400
        assert payloads[0]["response_format"] == {"type": "json_object"}

    def test_other_400_is_not_treated_as_format_rejection(self, monkeypatch, capsys):
        error = "Authentication Fails, Your api key is invalid"
        payloads = _install_responses(
            monkeypatch, [_bad_request(error) for _ in range(10)]
        )

        result = scraper.translate_text(
            "hello", config=_cfg(), glossary={}, use_glossary=False,
            response_schema=SCHEMA,
        )

        assert result is None
        # 3 次尝试（max_retries=2），全程保持 json_schema，不降级
        assert len(payloads) == 3
        assert all(p["response_format"]["type"] == "json_schema" for p in payloads)
        out = capsys.readouterr().out
        assert "HTTP 错误" in out
        # 错误原因必须被打印出来，否则只剩一个 400 无从排查
        assert "api key is invalid" in out

    def test_no_schema_means_no_response_format(self, monkeypatch):
        payloads = _install_responses(monkeypatch, [_ok("你好")])

        result = scraper.translate_text(
            "hello", config=_cfg(), glossary={}, use_glossary=False,
        )

        assert result == "你好"
        assert "response_format" not in payloads[0]

    def test_json_schema_disabled_in_config(self, monkeypatch):
        payloads = _install_responses(monkeypatch, [_ok("{}")])
        cfg = _cfg()
        cfg["openai_compat"]["json_schema"] = False

        scraper.translate_text(
            "hello", config=cfg, glossary={}, use_glossary=False, response_schema=SCHEMA,
        )

        assert "response_format" not in payloads[0]

    def test_requests_gzip_so_error_body_is_readable(self, monkeypatch):
        captured = {}

        def fake_post(url, json=None, headers=None, **kwargs):
            captured["headers"] = headers
            return _ok("{}")

        monkeypatch.setattr(scraper.requests, "post", fake_post)
        scraper.translate_text(
            "hello", config=_cfg(), glossary={}, use_glossary=False,
        )

        # 显式只要 gzip：避免网关返回 zstd 时 requests 解不开错误体
        assert captured["headers"]["Accept-Encoding"] == "gzip"
