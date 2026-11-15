"""test_dockerignore.py — 镜像构建上下文不能带本地凭据

`config/config.json`（API Key）与 `config/bili_cookies.json`（SESSDATA / bili_jct）都不入库，
运行期靠只读挂载提供。它们要是漏进构建上下文，本地 `docker build` 就会把凭据烤进镜像层，
push 或把离线 tar 交给别人等于直接泄露 —— 这条不变式用测试钉住。

路径从配置里的默认值取，不用手写字面量：默认路径改了，测试会跟着变，不会悄悄失效。
"""
from pathlib import Path

from bili.poster import DEFAULT_BILI_CONFIG
from utils import PROJECT_ROOT

DOCKERIGNORE = Path(PROJECT_ROOT) / ".dockerignore"


def _ignored_paths() -> set:
    lines = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")}


class TestSensitiveFilesIgnored:
    def test_cookie_file_ignored(self):
        cookie_file = str(DEFAULT_BILI_CONFIG["cookie_file"]).replace("\\", "/")
        assert cookie_file in _ignored_paths()

    def test_local_config_ignored(self):
        assert "config/config.json" in _ignored_paths()

    def test_dotenv_ignored(self):
        assert ".env" in _ignored_paths()
