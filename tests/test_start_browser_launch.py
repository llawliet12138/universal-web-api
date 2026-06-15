from pathlib import Path

import start


def test_fork_disables_upstream_auto_update_by_default():
    assert start.ENV_DEFAULTS["AUTO_UPDATE_ENABLED"] == "false"


def test_browser_launch_command_for_macos_uses_new_app_instance(monkeypatch):
    monkeypatch.setattr(start.sys, "platform", "darwin")
    browser_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    browser_args = [browser_path, "--remote-debugging-port=9222", "about:blank"]

    command = start._browser_launch_command(browser_path, browser_args)

    assert command == [
        "open",
        "-na",
        "/Applications/Google Chrome.app",
        "--args",
        "--remote-debugging-port=9222",
        "about:blank",
    ]


def test_browser_launch_command_for_other_platforms_uses_binary(monkeypatch):
    monkeypatch.setattr(start.sys, "platform", "linux")
    browser_path = "/usr/bin/chromium"
    browser_args = [browser_path, "--remote-debugging-port=9222", "about:blank"]

    assert start._browser_launch_command(browser_path, browser_args) == browser_args


def test_macos_app_bundle_lookup_rejects_non_app_path():
    assert start._find_macos_app_bundle(Path("/usr/bin/chromium")) is None
