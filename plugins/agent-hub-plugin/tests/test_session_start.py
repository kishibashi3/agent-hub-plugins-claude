"""Unit tests for session-start.sh (issue #44).

テスト対象:
  - AGENT_HUB_BRIDGE が設定された bridge 配下セッションでは additionalContext を出力しない
  - 未設定 (人間 operator セッション) では従来どおり additionalContext を出力する
  - 接続情報 (AGENT_HUB_URL / GITHUB_PAT) が無い場合は従来どおり静かに skip する

実行:
  python3 -m pytest plugins/agent-hub-plugin/tests/test_session_start.py -v
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent
    / "skills"
    / "agent-hub"
    / "scripts"
    / "session-start.sh"
)

# 接続情報が揃った「人間 operator セッション」相当の最小 env
BASE_ENV = {
    "PATH": "/usr/bin:/bin",
    "AGENT_HUB_URL": "https://hub.example.com/mcp",
    "GITHUB_PAT": "ghp_dummy",
}


def run_hook(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {**BASE_ENV, **extra_env}
    return subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class SessionStartHookTest(unittest.TestCase):
    def test_operator_session_injects_opening(self) -> None:
        """識別 env なし (人間 operator) では従来どおりオープニング指示を注入する."""
        result = run_hook({})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("get_messages", ctx)

    def test_bridge_session_skips_opening(self) -> None:
        """AGENT_HUB_BRIDGE=1 (bridge 配下) では何も出力せず exit 0."""
        result = run_hook({"AGENT_HUB_BRIDGE": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_bridge_flag_any_truthy_value_skips(self) -> None:
        """値は `1` に限らず、非空かつ `0` 以外なら bridge 配下とみなす."""
        for value in ("true", "bridge-claude2"):
            with self.subTest(value=value):
                result = run_hook({"AGENT_HUB_BRIDGE": value})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_bridge_flag_zero_or_empty_keeps_opening(self) -> None:
        """AGENT_HUB_BRIDGE=0 / 空文字は「未設定」と同じ扱い (従来挙動)."""
        for value in ("0", ""):
            with self.subTest(value=value):
                result = run_hook({"AGENT_HUB_BRIDGE": value})
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = json.loads(result.stdout)
                self.assertIn(
                    "get_messages", payload["hookSpecificOutput"]["additionalContext"]
                )

    def test_missing_connection_env_skips(self) -> None:
        """接続情報が無ければ bridge 判定に関係なく静かに skip する (既存挙動の回帰確認)."""
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
