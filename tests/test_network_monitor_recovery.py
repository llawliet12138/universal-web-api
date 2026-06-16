import pytest

from app.core.network_monitor import NetworkMonitor, NetworkMonitorError


class DeadDriver:
    is_running = False


class DeadTab:
    driver = DeadDriver()

    class Listen:
        _reuse_driver = False

        def start(self, pattern):
            raise AssertionError("dead target must be rejected before listener.start")

    listen = Listen()


def test_network_monitor_rejects_dead_tab_driver_before_registering_callbacks():
    monitor = object.__new__(NetworkMonitor)
    monitor.tab = DeadTab()
    monitor._listen_pattern = "backend-api/f/conversation"

    with pytest.raises(NetworkMonitorError, match="target 已失效"):
        monitor._start_listen()


def test_set_callback_none_error_is_restartable():
    assert NetworkMonitor._is_restartable_listen_error(
        "'NoneType' object has no attribute 'set_callback'"
    )
