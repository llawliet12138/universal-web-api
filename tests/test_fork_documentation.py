from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_chinese_readme_separates_upstream_and_fork_features():
    content = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "# Universal Web API（ChatGPT Thread Bridge Fork）" in content
    assert "## 本 Fork 与上游项目的关系" in content
    assert "### 上游原项目保留能力" in content
    assert "### 本 Fork 新增功能" in content
    assert "lumingya/universal-web-api" in content
    assert "ChatGPT 网页线程桥接" in content
    assert "终端 1：启动服务和受控浏览器" in content
    assert "终端 2：启动聊天客户端" in content


def test_english_readme_separates_upstream_and_fork_features():
    content = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "# Universal Web API (ChatGPT Thread Bridge Fork)" in content
    assert "## This Fork and the Upstream Project" in content
    assert "### Retained Upstream Capabilities" in content
    assert "### Features Added by This Fork" in content
    assert "lumingya/universal-web-api" in content
    assert "ChatGPT Web Thread Bridge" in content
    assert "Terminal 1: start the service and controlled browser" in content
    assert "Terminal 2: start the chat client" in content


def test_changelog_has_a_distinct_fork_entry():
    content = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "## 2026-06-15 - 本 Fork 新增" in content
    assert "上游原项目不包含" in content
