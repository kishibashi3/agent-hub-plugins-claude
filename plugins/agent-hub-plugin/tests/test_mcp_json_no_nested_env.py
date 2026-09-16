"""Regression test for issue #41: .mcp.json must not contain nested env syntax.

背景 (再発バグ):
  Claude Code の MCP 設定の変数展開は **ネスト `${VAR:-${VAR2:-}}` を展開できない**。
  展開できないとリテラル `${...}` がそのまま server に送られ、handle regex 検証で
  弾かれて MCP 接続が HTTP 500 で死ぬ。X-User-Id → X-Participant-Id 移行のたびに
  「USER fallback を残そう」とネストを足して再発してきた。

恒久対策:
  `.mcp.json` の AGENT_HUB_* ヘッダ参照は **単純 `${VAR}` または単一 `${VAR:-}`** のみ。
  ネスト `${...:-${...}}` を一切入れない。USER fallback の後方互換は .mcp.json でなく
  起動 env 側で AGENT_HUB_PARTICIPANT を保証して担保する。

このテストは 2 つの artifact を守る:
  - commit 済みテンプレ `.mcp.json`
  - `setup-hubs.sh` が生成する `.mcp.json`

実行:
  python3 -m pytest plugins/agent-hub-plugin/tests/test_mcp_json_no_nested_env.py -v
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent.parent
COMMITTED_MCP_JSON = PLUGIN_DIR / ".mcp.json"
SETUP_HUBS = PLUGIN_DIR / "skills" / "agent-hub" / "scripts" / "setup-hubs.sh"

# 完了条件 (issue #41) の grep と等価:
#   grep -rn ':-\${' plugins/
# 変数の default 値にさらに ${...} がネストしている形を検出する (AGENT_HUB_* に
# 限らず GITHUB_PAT_N 等の全変数が対象)。
# 単一 `${AGENT_HUB_TENANT:-}` (default が空) は許容、ネストのみ禁止。
_NESTED_AGENT_HUB = re.compile(r":-\$\{")


def _find_nested(text: str) -> list[str]:
    """ネスト AGENT_HUB env 構文を含む行を返す。"""
    return [
        line
        for line in text.splitlines()
        if _NESTED_AGENT_HUB.search(line)
    ]


class TestCommittedMcpJson(unittest.TestCase):
    def test_is_valid_json(self) -> None:
        """commit 済み .mcp.json は valid JSON である。"""
        json.loads(COMMITTED_MCP_JSON.read_text())

    def test_no_nested_agent_hub_env(self) -> None:
        """commit 済み .mcp.json に AGENT_HUB ネスト構文が無い (issue #41)。"""
        offenders = _find_nested(COMMITTED_MCP_JSON.read_text())
        assert not offenders, (
            "Nested ${AGENT_HUB_*:-${...}} found in committed .mcp.json "
            f"(issue #41 — Claude Code can't expand nesting):\n" + "\n".join(offenders)
        )

    def test_primary_participant_id_is_simple(self) -> None:
        """primary hub の X-Participant-Id は単純 ${AGENT_HUB_PARTICIPANT} である。"""
        config = json.loads(COMMITTED_MCP_JSON.read_text())
        value = config["agent-hub"]["headers"]["X-Participant-Id"]
        assert value == "${AGENT_HUB_PARTICIPANT}", (
            f"Primary X-Participant-Id must be simple form, got: {value!r}"
        )

    def test_all_participant_ids_simple_form(self) -> None:
        """全 hub の X-Participant-Id が単純形 (ネスト無し) である。"""
        config = json.loads(COMMITTED_MCP_JSON.read_text())
        for name, server in config.items():
            value = server.get("headers", {}).get("X-Participant-Id", "")
            assert ":-${" not in value, (
                f"{name}: X-Participant-Id contains nested env syntax: {value!r}"
            )

    def test_secondary_pat_is_single_default(self) -> None:
        """agent-hub-2 の Authorization は単一 :- のみ (issue #41)。

        ${GITHUB_PAT_2:-${GITHUB_PAT}} は Claude Code が最初の } で default を切り、
        GITHUB_PAT_2 設定時に token 末尾へ "}" が付いて 401 になる。
        """
        config = json.loads(COMMITTED_MCP_JSON.read_text())
        assert (
            config["agent-hub-2"]["headers"]["Authorization"]
            == "Bearer ${GITHUB_PAT_2:-}"
        )


class TestGeneratedMcpJson(unittest.TestCase):
    """setup-hubs.sh の生成物にネスト構文が混入しないことを検証する。

    setup-hubs.sh は出力先を PLUGIN_DIR/.mcp.json に hardcode しているため、
    commit 済みファイルを上書きしないよう temp に script 構造を複製して実行する。
    """

    def _generate(self, tmp_root: Path, urls: str) -> str:
        scripts_dir = tmp_root / "skills" / "agent-hub" / "scripts"
        scripts_dir.mkdir(parents=True)
        shutil.copy(SETUP_HUBS, scripts_dir / "setup-hubs.sh")

        result = subprocess.run(
            ["bash", str(scripts_dir / "setup-hubs.sh")],
            env={"AGENT_HUB_URLS": urls, "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"setup-hubs.sh failed: {result.stderr}"
        return (tmp_root / ".mcp.json").read_text()

    def test_single_hub_no_nested_env(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            generated = self._generate(Path(td), "http://hub1:3000/mcp")
            json.loads(generated)  # valid JSON
            offenders = _find_nested(generated)
            assert not offenders, (
                "Nested ${AGENT_HUB_*:-${...}} in single-hub generated .mcp.json "
                f"(issue #41):\n" + "\n".join(offenders)
            )

    def test_multi_hub_no_nested_env(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            generated = self._generate(
                Path(td),
                "http://hub1:3000/mcp http://hub2:3000/mcp http://hub3:3000/mcp",
            )
            config = json.loads(generated)  # valid JSON
            offenders = _find_nested(generated)
            assert not offenders, (
                "Nested ${AGENT_HUB_*:-${...}} in multi-hub generated .mcp.json "
                f"(issue #41):\n" + "\n".join(offenders)
            )
            # primary は単純形、hub-N は単一 :- まで
            assert (
                config["agent-hub"]["headers"]["X-Participant-Id"]
                == "${AGENT_HUB_PARTICIPANT}"
            )
            assert (
                config["agent-hub-2"]["headers"]["X-Participant-Id"]
                == "${AGENT_HUB_PARTICIPANT_2:-}"
            )
            assert (
                config["agent-hub-3"]["headers"]["Authorization"]
                == "Bearer ${GITHUB_PAT_3:-}"
            )


if __name__ == "__main__":
    unittest.main()
