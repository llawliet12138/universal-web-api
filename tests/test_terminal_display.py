import io
import sys

import terminal_display


def test_start_message_classification_keeps_only_key_lines():
    assert terminal_display.start_message_is_permanent("[ERROR] 依赖安装失败")
    assert terminal_display.start_message_is_permanent("   API 地址:     http://127.0.0.1:8199")
    assert not terminal_display.start_message_is_permanent("[STEP] 检查依赖")
    assert not terminal_display.start_message_is_permanent("[OK] 依赖已是最新")


def test_log_record_classification_keeps_warnings_and_noisy_request_completion_temporary():
    assert terminal_display.log_record_is_permanent("WARNING", "BROWSER", "[MONIT] 网络监听错误")
    assert not terminal_display.log_record_is_permanent(
        "INFO",
        "REQUEST",
        "完成 (12.4s, status=completed, tab=gpt_3, reason=-)",
    )
    assert terminal_display.log_record_is_permanent(
        "INFO",
        "REQUEST",
        "完成 (12.4s, status=failed, tab=gpt_3, reason=-)",
    )
    assert terminal_display.log_record_is_permanent("INFO", "CHATGPT", "bridge released")
    assert not terminal_display.log_record_is_permanent("INFO", "BROWSER", "输入内容已经核对通过了")


def test_status_display_writes_transient_status_and_clears_before_permanent():
    stream = io.StringIO()
    display = terminal_display.TerminalDisplay(stream=stream, mode="status", force_tty=True)

    display.write("[STEP] 检查依赖")
    display.write("[ERROR] 依赖安装失败")

    output = stream.getvalue()
    assert "status: [STEP] 检查依赖" in output
    assert "\r\033[2K[ERROR] 依赖安装失败\n" in output


def test_block_display_keeps_section_with_one_dim_live_line_by_default(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERMINAL_LOG_BLOCK_LINES", raising=False)
    stream = io.StringIO()
    display = terminal_display.TerminalDisplay(stream=stream, mode="block", force_tty=True)

    display.start_section("检查依赖")
    display.status("resolving packages")
    display.status("installing packages")
    display.write("[ERROR] 依赖安装失败")

    output = stream.getvalue()
    assert "检查依赖" in output
    assert "\033[2m  resolving packages\033[0m" in output
    assert "\033[2m  installing packages\033[0m" in output
    assert "\033[J" in output
    assert "[ERROR] 依赖安装失败\n" in output
    assert display._block_lines.maxlen == 1


def test_block_display_can_keep_multiple_recent_lines_when_configured(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERMINAL_LOG_BLOCK_LINES", "3")
    stream = io.StringIO()
    display = terminal_display.TerminalDisplay(stream=stream, mode="block", force_tty=True)

    display.start_section("检查依赖")
    display.status("one")
    display.status("two")
    display.status("three")

    output = stream.getvalue()
    assert "\033[2m  one\033[0m" in output
    assert "\033[2m  two\033[0m" in output
    assert "\033[2m  three\033[0m" in output


def test_dynamic_modes_fall_back_to_plain_for_non_tty_stream():
    stream = io.StringIO()
    display = terminal_display.TerminalDisplay(stream=stream, mode="block")

    display.write("[STEP] 检查依赖")

    assert stream.getvalue() == "[STEP] 检查依赖\n"


def test_start_run_suppresses_child_output_to_log(monkeypatch, tmp_path):
    import start

    stream = io.StringIO()
    monkeypatch.setattr(
        terminal_display,
        "_GLOBAL_DISPLAY",
        terminal_display.TerminalDisplay(stream=stream, mode="block", force_tty=True),
    )
    log_file = tmp_path / "startup-subprocess.log"
    monkeypatch.setenv("STARTUP_SUBPROCESS_LOG_FILE", str(log_file))

    completed = start._run(
        [sys.executable, "-c", "print('child detail')"],
        check=False,
    )

    assert completed.returncode == 0
    assert "child detail" in log_file.read_text(encoding="utf-8")
    assert "child detail" in stream.getvalue()


def test_start_debug_logs_cli_forces_plain_mode(monkeypatch):
    import start

    monkeypatch.setenv("TERMINAL_LOG_MODE", "block")

    args = start._parse_args(["--debug-logs"])
    start._apply_cli_overrides(args)

    assert terminal_display.resolve_terminal_log_mode() == "plain"


def test_start_first_cli_only_enables_default_browser_guide(monkeypatch):
    import start

    monkeypatch.delenv("OPEN_DEFAULT_BROWSER_GUIDE", raising=False)
    monkeypatch.delenv("OPEN_STARTUP_GUIDE", raising=False)

    args = start._parse_args(["--first"])
    start._apply_cli_overrides(args)

    assert start.os.environ["OPEN_DEFAULT_BROWSER_GUIDE"] == "true"
    assert "OPEN_STARTUP_GUIDE" not in start.os.environ


def test_start_site_flags_set_controlled_browser_startup_sites_and_login(monkeypatch):
    import start

    monkeypatch.delenv("CONTROLLED_BROWSER_STARTUP_SITES", raising=False)
    monkeypatch.delenv("OPEN_DEFAULT_BROWSER_GUIDE", raising=False)
    monkeypatch.delenv("CONTROLLED_BROWSER_LOGIN_MODE", raising=False)

    args = start._parse_args(["--first", "--login", "--site", "chatgpt", "--site", "grok"])
    start._apply_cli_overrides(args)

    assert start.os.environ["OPEN_DEFAULT_BROWSER_GUIDE"] == "true"
    assert start.os.environ["CONTROLLED_BROWSER_STARTUP_SITES"] == "chatgpt,grok"
    assert start.os.environ["CONTROLLED_BROWSER_LOGIN_MODE"] == "true"


def test_start_rejects_legacy_positional_args():
    import start

    try:
        start._parse_args(["first", "chatgpt"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("legacy positional args should be rejected")


def test_macos_browser_launch_is_background_by_default_and_foreground_for_login(monkeypatch):
    import start

    browser_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    args = [browser_path, "--remote-debugging-port=9222", "about:blank"]
    monkeypatch.setattr(start.sys, "platform", "darwin")
    monkeypatch.delenv("CONTROLLED_BROWSER_LOGIN_MODE", raising=False)

    assert start._browser_launch_command(browser_path, args)[1] == "-gna"

    monkeypatch.setenv("CONTROLLED_BROWSER_LOGIN_MODE", "true")
    assert start._browser_launch_command(browser_path, args)[1] == "-na"


def test_main_resolves_controlled_browser_startup_sites():
    import main

    targets = main._resolve_controlled_browser_startup_targets("chatgpt,grok,gemini.com,missing-site")
    domains = [target["domain"] for target in targets]

    assert domains == ["chatgpt.com", "grok.com", "gemini.google.com"]
    assert [target["url"] for target in targets] == [
        "https://chatgpt.com",
        "https://grok.com",
        "https://gemini.google.com",
    ]


def test_main_controlled_browser_background_mode_tracks_login(monkeypatch):
    import main

    monkeypatch.delenv("CONTROLLED_BROWSER_LOGIN_MODE", raising=False)
    assert main._controlled_browser_background_mode() is True

    monkeypatch.setenv("CONTROLLED_BROWSER_LOGIN_MODE", "true")
    assert main._controlled_browser_background_mode() is False


def test_browser_core_close_does_not_close_tabs_during_service_shutdown():
    from app.core.browser.main import BrowserCore

    class FakeTabPool:
        def __init__(self):
            self.calls = []

        def shutdown(self, close_browser_tabs=True):
            self.calls.append(close_browser_tabs)

    browser = BrowserCore()
    pool = FakeTabPool()
    browser._tab_pool = pool
    browser._watchdog_thread = None

    browser.close()

    assert pool.calls == [False]
