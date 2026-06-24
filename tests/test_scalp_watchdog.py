from argparse import Namespace
from pathlib import Path

import scalp_watchdog as sw


def test_watchdog_defaults_allow_paper_sample_collection_through_memory_sleep(monkeypatch):
    monkeypatch.setattr(sw, "default_python", lambda: "python")
    args = Namespace(
        child_args=[],
        allow_live=False,
        interval_seconds=2.0,
        paper_margin_usdt=1.0,
        paper_leverage=20,
        paper_equity=100.0,
        paper_trade_through_memory_sleep=True,
    )

    cmd = sw.build_child_cmd(args)

    assert "--paper-equity" in cmd
    assert "100.0" in cmd
    assert "--paper-trade-through-memory-sleep" in cmd


def test_watchdog_can_disable_paper_sleep_bypass(monkeypatch):
    monkeypatch.setattr(sw, "default_python", lambda: "python")
    args = Namespace(
        child_args=[],
        allow_live=False,
        interval_seconds=2.0,
        paper_margin_usdt=1.0,
        paper_leverage=20,
        paper_equity=100.0,
        paper_trade_through_memory_sleep=False,
    )

    cmd = sw.build_child_cmd(args)

    assert "--paper-trade-through-memory-sleep" not in cmd

def test_watchdog_child_uses_no_window_on_windows(monkeypatch, tmp_path):
    calls = []

    class FakeChild:
        pid = 123
        returncode = 0

        def __init__(self):
            self._polled = False

        def poll(self):
            if self._polled:
                return 0
            self._polled = True
            return None

    monkeypatch.setattr(sw, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sw, "STOP_FILE", tmp_path / "STOP_SCALP_WATCHDOG")
    monkeypatch.setattr(sw, "PID_FILE", tmp_path / "scalp_watchdog.pid")
    monkeypatch.setattr(sw, "CHILD_PID_FILE", tmp_path / "scalp_autotrader.pid")
    monkeypatch.setattr(sw, "WATCHDOG_LOG", tmp_path / "watchdog.jsonl")
    monkeypatch.setattr(sw, "CHILD_OUT", tmp_path / "out.log")
    monkeypatch.setattr(sw, "CHILD_ERR", tmp_path / "err.log")
    monkeypatch.setattr(sw.os, "getpid", lambda: 999)
    monkeypatch.setattr(sw, "is_pid_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(sw.subprocess, "CREATE_NO_WINDOW", 134217728, raising=False)
    monkeypatch.setattr(sw.subprocess, "Popen", lambda *args, **kwargs: calls.append(kwargs) or FakeChild())
    monkeypatch.setattr(sw.time, "sleep", lambda *_: sw.STOP_FILE.write_text("stop", encoding="ascii"))
    args = Namespace(child_args=[], allow_live=False, check_seconds=0.01, restart_delay_seconds=0.01, interval_seconds=2.0, paper_margin_usdt=1.0, paper_leverage=20, paper_equity=100.0, paper_trade_through_memory_sleep=True)

    sw.supervise(args)

    assert calls
    assert calls[0]["creationflags"] == 134217728

def test_watchdog_prefers_pythonw_for_background_child_on_windows(monkeypatch, tmp_path: Path):
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    pythonw = scripts / "pythonw.exe"
    pythonw.write_text("", encoding="ascii")
    (scripts / "python.exe").write_text("", encoding="ascii")

    monkeypatch.setattr(sw, "ROOT", tmp_path)
    monkeypatch.setattr(sw.os, "name", "nt", raising=False)

    assert sw.default_python() == str(pythonw)
