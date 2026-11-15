"""test_bili_links.py — 专栏的超链接策略（只留署名，其余降级成「文字（URL）」）

策略本身是两行判断，但它决定成稿长什么样：外链在 B站 里点开是拦截页，所以必须降级；
地址又要留在正文里（下载地址、漏洞追踪器这类信息丢了比不可点更糟）。这两条用测试钉住。
"""
from bili import SIGNATURE_LINKS, degrade_link, is_signature_link

REPO = "https://github.com/jiubook/MCTTK"


class TestIsSignatureLink:
    def test_repo_link_kept(self):
        assert is_signature_link(REPO)
        assert is_signature_link(REPO + "/tree/main")

    def test_case_insensitive(self):
        assert is_signature_link("HTTPS://GitHub.com/JiuBook/MCTTK")

    def test_other_links_dropped(self):
        assert not is_signature_link("https://www.minecraft.net/zh-hans")
        assert not is_signature_link("https://github.com/other/repo")
        assert not is_signature_link("")

    def test_policy_lists_repo(self):
        assert any("jiubook/mcttk" in pattern.lower() for pattern in SIGNATURE_LINKS)


class TestDegradeLink:
    def test_label_and_url(self):
        assert degrade_link("服务器 jar", "https://x.com/a") == "服务器 jar（https://x.com/a）"

    def test_url_label_not_duplicated(self):
        """元信息的「原文：URL」文字本身就是地址，不要写成 URL（URL）。"""
        assert degrade_link(REPO, REPO) == REPO

    def test_empty_label_falls_back_to_url(self):
        assert degrade_link("", "https://x.com/a") == "https://x.com/a"

    def test_surrounding_whitespace_trimmed(self):
        assert degrade_link(" 文字 ", " https://x.com/a ") == "文字（https://x.com/a）"

    def test_nothing_left(self):
        assert degrade_link("", "") == ""
