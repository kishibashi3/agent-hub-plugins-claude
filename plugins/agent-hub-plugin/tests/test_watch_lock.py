"""Regression tests for watch.sh single-instance lock (issue #50).

テスト対象:
  - 本プロセスを kill -9 しても子がロックを持ち続けず、再起動した watch.sh がロックを取得できる
  - ハブ接続ループだけが死んだら watch.sh 自体も終了し、ロックを解放する

到達できない hub (127.0.0.1:1) に trust モードで接続させ、接続リトライ中のプロセスを操作する。
flock / pgrep が無い環境 (例: macOS) では skip する。

実行:
  python3 -m pytest plugins/agent-hub-plugin/tests/test_watch_lock.py -v
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
import unittest
import uuid
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent
    / "skills"
    / "agent-hub"
    / "scripts"
    / "watch.sh"
)

LOCK_MSG = "single-instance lock acquired"
TIMEOUT = 15.0


def _children(pid: int) -> list[int]:
    out = subprocess.run(
        ["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False
    ).stdout
    return sorted(int(p) for p in out.split())


def _hub_loops(pid: int) -> list[int]:
    """親の直接の子のうちハブ接続ループの PID を返す (PID の並び順には依存しない).

    watchdog は stdout を /dev/null にリダイレクトしているので、stdout の向き先で見分ける。
    """
    hubs = []
    for child in _children(pid):
        try:
            stdout = os.readlink(f"/proc/{child}/fd/1")
        except OSError:
            continue
        if stdout != os.devnull:
            hubs.append(child)
    return hubs


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@unittest.skipUnless(
    shutil.which("flock") and shutil.which("pgrep") and os.path.isdir("/proc"),
    "flock / pgrep / /proc が必要",
)
class WatchLockTest(unittest.TestCase):
    def setUp(self) -> None:
        # テストごとに固有の handle / tenant にして、実運用のロックファイルと衝突させない
        suffix = uuid.uuid4().hex[:8]
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "AGENT_HUB_PARTICIPANT": f"test-watch-{suffix}",
            "AGENT_HUB_TENANT": f"test-{suffix}",
            "AGENT_HUB_URL": "http://127.0.0.1:1/mcp",
        }
        self.lockfile = Path(
            f"/tmp/agent-hub-watch-{self.env['AGENT_HUB_PARTICIPANT']}"
            f"-{self.env['AGENT_HUB_TENANT']}.lock"
        )
        self.procs: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for proc in self.procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            if proc.stdout:
                proc.stdout.close()
        self.lockfile.unlink(missing_ok=True)

    def _start(self) -> subprocess.Popen[str]:
        # 独立したセッションで起動し、tearDown で子孫ごと片付けられるようにする
        proc = subprocess.Popen(
            ["bash", str(SCRIPT)],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        self.procs.append(proc)
        return proc

    def _wait_lock_line(self, proc: subprocess.Popen[str]) -> str:
        """boot ログを読み、ロック取得 / 退避のどちらかの行を返す."""
        assert proc.stdout is not None
        for line in proc.stdout:
            if LOCK_MSG in line or "already holds the lock" in line:
                return line
        self.fail("watch.sh exited without a lock message")

    def _wait_until(self, cond, what: str) -> None:
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if cond():
                return
            time.sleep(0.2)
        self.fail(f"timed out waiting for: {what}")

    def test_restart_acquires_lock_after_parent_killed(self) -> None:
        """親を kill -9 しても、再起動した watch.sh がロックを取得できる."""
        first = self._start()
        self.assertIn(LOCK_MSG, self._wait_lock_line(first))
        self._wait_until(lambda: len(_children(first.pid)) >= 2, "hub loop + watchdog")

        os.kill(first.pid, signal.SIGKILL)
        first.wait()

        second = self._start()
        self.assertIn(LOCK_MSG, self._wait_lock_line(second))

    def test_script_exits_when_hub_loop_dies(self) -> None:
        """ハブ接続ループが死んだら watch.sh も終了し、ロックを解放する."""
        proc = self._start()
        self.assertIn(LOCK_MSG, self._wait_lock_line(proc))
        # watchdog の >/dev/null は fork 後に子側で張られるので、直後は fd/1 がまだ PIPE を指す.
        # hub が 1 個に見分けられるまで待つ.
        self._wait_until(
            lambda: len(_children(proc.pid)) >= 2 and len(_hub_loops(proc.pid)) == 1,
            "hub loop + watchdog (stdout redirected)",
        )

        # 親の直接の子はハブ接続ループ (hub は 1 個) と watchdog
        hubs = _hub_loops(proc.pid)
        self.assertEqual(len(hubs), 1)
        hub_pid = hubs[0]
        os.kill(hub_pid, signal.SIGKILL)

        self._wait_until(lambda: proc.poll() is not None, "watch.sh to exit")
        self.assertFalse(_alive(hub_pid))

        second = self._start()
        self.assertIn(LOCK_MSG, self._wait_lock_line(second))


if __name__ == "__main__":
    unittest.main()
