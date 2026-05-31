"""Unit tests for emit_artifact_span.py (issue #28).

テスト対象:
  - parse_artifact: Write / Edit / Bash (git commit / gh pr / その他) / エラーレスポンス
  - emit_artifact_span: span 属性確認
  - main: stdin → span emit 統合フロー / msg_id 引き継ぎ / skip パス

実行:
  python3 -m pytest plugins/agent-hub-plugin/tests/test_emit_artifact_span.py -v

opentelemetry 未インストール環境でも skip guard テストは動作する。
emit_artifact_span テストは opentelemetry がインストール済みの場合のみ実行する。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

# scripts/ ディレクトリを sys.path に追加
SCRIPTS_DIR = (
    Path(__file__).parent.parent / "skills" / "agent-hub" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import emit_artifact_span as target
import emit_span  # save_msg_id / load_msg_id のテストにも使用


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _bash_payload(
    command: str,
    output: str = "",
    is_error: bool = False,
) -> dict[str, Any]:
    """Bash ツールの PostToolUse hook ペイロードを生成する。"""
    return {
        "session_id": "test-session",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "test"},
        "tool_response": {
            "content": [{"type": "text", "text": output}],
            "isError": is_error,
        },
    }


def _write_payload(file_path: str, is_error: bool = False) -> dict[str, Any]:
    """Write ツールの PostToolUse hook ペイロードを生成する。"""
    return {
        "session_id": "test-session",
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": "..."},
        "tool_response": {
            "content": [{"type": "text", "text": "File written."}],
            "isError": is_error,
        },
    }


def _edit_payload(file_path: str, is_error: bool = False) -> dict[str, Any]:
    """Edit ツールの PostToolUse hook ペイロードを生成する。"""
    return {
        "session_id": "test-session",
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "old_string": "a", "new_string": "b"},
        "tool_response": {
            "content": [{"type": "text", "text": "File edited."}],
            "isError": is_error,
        },
    }


# ---------------------------------------------------------------------------
# _extract_text_output
# ---------------------------------------------------------------------------


class TestExtractTextOutput(unittest.TestCase):
    def test_mcp_content_array(self) -> None:
        resp = {"content": [{"type": "text", "text": "hello"}], "isError": False}
        assert target._extract_text_output(resp) == "hello"

    def test_string_response(self) -> None:
        assert target._extract_text_output("raw text") == "raw text"

    def test_non_text_content_skipped(self) -> None:
        resp = {
            "content": [
                {"type": "image", "url": "http://x.com/img.png"},
                {"type": "text", "text": "ok"},
            ]
        }
        assert target._extract_text_output(resp) == "ok"

    def test_empty_content(self) -> None:
        assert target._extract_text_output({"content": []}) == ""

    def test_non_dict_returns_empty(self) -> None:
        assert target._extract_text_output(42) == ""
        assert target._extract_text_output(None) == ""


# ---------------------------------------------------------------------------
# parse_artifact — Write
# ---------------------------------------------------------------------------


class TestParseArtifactWrite(unittest.TestCase):
    def test_write_returns_file_write_span(self) -> None:
        payload = _write_payload("/path/to/file.py")
        result = target.parse_artifact(payload)
        assert result is not None
        assert result["span_name"] == "plugin.artifact.file_write"
        assert result["attributes"]["artifact.type"] == "file_write"
        assert result["attributes"]["artifact.path"] == "/path/to/file.py"

    def test_write_error_response_returns_none(self) -> None:
        payload = _write_payload("/path/to/file.py", is_error=True)
        assert target.parse_artifact(payload) is None

    def test_write_missing_file_path_returns_none(self) -> None:
        payload = {
            "tool_name": "Write",
            "tool_input": {},
            "tool_response": {"content": [], "isError": False},
        }
        assert target.parse_artifact(payload) is None

    def test_write_path_field_fallback(self) -> None:
        """file_path がなく path フィールドがある場合のフォールバック。"""
        payload = {
            "tool_name": "Write",
            "tool_input": {"path": "/alt/path.txt"},
            "tool_response": {"content": [], "isError": False},
        }
        result = target.parse_artifact(payload)
        assert result is not None
        assert result["attributes"]["artifact.path"] == "/alt/path.txt"


# ---------------------------------------------------------------------------
# parse_artifact — Edit
# ---------------------------------------------------------------------------


class TestParseArtifactEdit(unittest.TestCase):
    def test_edit_returns_file_edit_span(self) -> None:
        payload = _edit_payload("/src/main.py")
        result = target.parse_artifact(payload)
        assert result is not None
        assert result["span_name"] == "plugin.artifact.file_edit"
        assert result["attributes"]["artifact.type"] == "file_edit"
        assert result["attributes"]["artifact.path"] == "/src/main.py"

    def test_edit_error_response_returns_none(self) -> None:
        assert target.parse_artifact(_edit_payload("/f.py", is_error=True)) is None

    def test_edit_missing_file_path_returns_none(self) -> None:
        payload = {
            "tool_name": "Edit",
            "tool_input": {},
            "tool_response": {"content": [], "isError": False},
        }
        assert target.parse_artifact(payload) is None


# ---------------------------------------------------------------------------
# parse_artifact — Bash (git commit)
# ---------------------------------------------------------------------------


class TestParseArtifactBashGitCommit(unittest.TestCase):
    def test_git_commit_extracts_hash(self) -> None:
        output = "[main abc1234] feat: add something\n 1 file changed"
        payload = _bash_payload("git commit -m 'feat: add something'", output)
        result = target.parse_artifact(payload)
        assert result is not None
        assert result["span_name"] == "plugin.artifact.git_commit"
        assert result["attributes"]["artifact.type"] == "git_commit"
        assert result["attributes"]["artifact.commit_hash"] == "abc1234"

    def test_git_commit_long_hash(self) -> None:
        output = "[feat/branch 1a2b3c4d5e6f7890] message"
        payload = _bash_payload("git commit -m 'message'", output)
        result = target.parse_artifact(payload)
        assert result is not None
        assert result["attributes"]["artifact.commit_hash"] == "1a2b3c4d5e6f7890"

    def test_git_commit_no_hash_in_output(self) -> None:
        """hash が見つからない場合は空文字 (span は emit する)。"""
        payload = _bash_payload("git commit -m 'message'", "nothing useful here")
        result = target.parse_artifact(payload)
        assert result is not None
        assert result["attributes"]["artifact.commit_hash"] == ""

    def test_git_commit_command_truncated_at_256(self) -> None:
        long_cmd = "git commit -m '" + "x" * 300 + "'"
        payload = _bash_payload(long_cmd, "[main abc1234] x")
        result = target.parse_artifact(payload)
        assert result is not None
        assert len(result["attributes"]["artifact.command"]) <= 256

    def test_git_commit_error_response_returns_none(self) -> None:
        payload = _bash_payload("git commit -m 'x'", "", is_error=True)
        assert target.parse_artifact(payload) is None

    def test_non_git_commit_bash_returns_none(self) -> None:
        """git commit でない Bash はスキップ。"""
        payload = _bash_payload("ls -la", "file.txt")
        assert target.parse_artifact(payload) is None


# ---------------------------------------------------------------------------
# parse_artifact — Bash (gh pr create)
# ---------------------------------------------------------------------------


class TestParseArtifactBashGhPr(unittest.TestCase):
    def test_gh_pr_create_extracts_url(self) -> None:
        output = "https://github.com/owner/repo/pull/42\n"
        payload = _bash_payload("gh pr create --title 'feat' --body '...'", output)
        result = target.parse_artifact(payload)
        assert result is not None
        assert result["span_name"] == "plugin.artifact.pr_create"
        assert result["attributes"]["artifact.type"] == "pr_create"
        assert result["attributes"]["artifact.pr_url"] == (
            "https://github.com/owner/repo/pull/42"
        )

    def test_gh_pr_create_no_url_in_output(self) -> None:
        """PR URL が見つからない場合は空文字 (span は emit する)。"""
        payload = _bash_payload("gh pr create --title 'x'", "Warning: something")
        result = target.parse_artifact(payload)
        assert result is not None
        assert result["attributes"]["artifact.pr_url"] == ""

    def test_gh_pr_merge_detected(self) -> None:
        output = "https://github.com/owner/repo/pull/99\n"
        payload = _bash_payload("gh pr merge 99 --squash", output)
        result = target.parse_artifact(payload)
        assert result is not None
        assert result["span_name"] == "plugin.artifact.pr_create"

    def test_gh_pr_error_response_returns_none(self) -> None:
        payload = _bash_payload("gh pr create --title 'x'", "", is_error=True)
        assert target.parse_artifact(payload) is None


# ---------------------------------------------------------------------------
# parse_artifact — 対象外
# ---------------------------------------------------------------------------


class TestParseArtifactSkipped(unittest.TestCase):
    def test_read_tool_returns_none(self) -> None:
        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "/f.py"},
            "tool_response": {"content": [], "isError": False},
        }
        assert target.parse_artifact(payload) is None

    def test_unknown_tool_returns_none(self) -> None:
        payload = {"tool_name": "UnknownTool", "tool_input": {}, "tool_response": {}}
        assert target.parse_artifact(payload) is None

    def test_empty_payload_returns_none(self) -> None:
        assert target.parse_artifact({}) is None


# ---------------------------------------------------------------------------
# emit_span.save_msg_id / load_msg_id (state file — issue #28 追加分)
# ---------------------------------------------------------------------------


class TestStatefile(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.mkdtemp()

    def test_save_and_load_roundtrip(self) -> None:
        with patch.dict(os.environ, {"TMPDIR": self._tmpdir}):
            emit_span.save_msg_id("sess-abc", "msg-123")
            assert emit_span.load_msg_id("sess-abc") == "msg-123"

    def test_load_returns_empty_when_not_saved(self) -> None:
        with patch.dict(os.environ, {"TMPDIR": self._tmpdir}):
            assert emit_span.load_msg_id("no-such-session") == ""

    def test_save_overwrites_previous(self) -> None:
        with patch.dict(os.environ, {"TMPDIR": self._tmpdir}):
            emit_span.save_msg_id("sess", "old-id")
            emit_span.save_msg_id("sess", "new-id")
            assert emit_span.load_msg_id("sess") == "new-id"

    def test_sessions_are_isolated(self) -> None:
        with patch.dict(os.environ, {"TMPDIR": self._tmpdir}):
            emit_span.save_msg_id("sess-1", "id-for-sess1")
            emit_span.save_msg_id("sess-2", "id-for-sess2")
            assert emit_span.load_msg_id("sess-1") == "id-for-sess1"
            assert emit_span.load_msg_id("sess-2") == "id-for-sess2"

    def test_path_traversal_sanitized(self) -> None:
        """session_id に '../' が含まれても安全なパスになる。"""
        with patch.dict(os.environ, {"TMPDIR": self._tmpdir}):
            # '../etc/passwd' の特殊文字が除去されることを確認
            path = emit_span._state_path("../etc/passwd")
            # パスが tmpdir 内に収まること
            assert path.parent == Path(self._tmpdir)
            # 危険な文字が含まれないこと
            assert ".." not in path.name
            assert "/" not in path.name


# ---------------------------------------------------------------------------
# emit_artifact_span (opentelemetry 必要)
# ---------------------------------------------------------------------------


def _has_opentelemetry() -> bool:
    try:
        import opentelemetry  # noqa: F401
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_has_opentelemetry(), "opentelemetry not installed")
class TestEmitArtifactSpan(unittest.TestCase):
    def test_span_attributes_file_write(self) -> None:
        """emit_artifact_span() が正しい属性で span を emit することを確認する。"""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        in_memory_exporter = InMemorySpanExporter()
        test_provider = TracerProvider()

        with patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            return_value=in_memory_exporter,
        ), patch(
            "opentelemetry.sdk.trace.TracerProvider",
            return_value=test_provider,
        ):
            target.emit_artifact_span(
                span_name="plugin.artifact.file_write",
                attributes={"artifact.type": "file_write", "artifact.path": "/src/x.py"},
                msg_id="test-msg-id",
                model="claude-sonnet-4-5",
                telemetry_url="http://otel:4318",
            )

        spans = in_memory_exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})

        assert spans[0].name == "plugin.artifact.file_write"
        assert attrs["msg_id"] == "test-msg-id"
        assert attrs["gen_ai.request.model"] == "claude-sonnet-4-5"
        assert attrs["artifact.type"] == "file_write"
        assert attrs["artifact.path"] == "/src/x.py"

    def test_span_name_reflects_artifact_type(self) -> None:
        """span 名が artifact 種別ごとに異なることを確認する。"""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        in_memory_exporter = InMemorySpanExporter()
        test_provider = TracerProvider()

        with patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            return_value=in_memory_exporter,
        ), patch(
            "opentelemetry.sdk.trace.TracerProvider",
            return_value=test_provider,
        ):
            target.emit_artifact_span(
                span_name="plugin.artifact.git_commit",
                attributes={"artifact.type": "git_commit", "artifact.commit_hash": "abc1234", "artifact.command": "git commit"},
                msg_id="",
                model="claude-sonnet-4-5",
                telemetry_url="http://otel:4318",
            )

        spans = in_memory_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "plugin.artifact.git_commit"
        attrs = dict(spans[0].attributes or {})
        assert attrs["artifact.commit_hash"] == "abc1234"


# ---------------------------------------------------------------------------
# main (統合テスト)
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def _run_main(
        self,
        payload: dict[str, Any],
        env: dict[str, str],
        tmpdir: str | None = None,
    ) -> int:
        stdin_data = json.dumps(payload)
        env_patch = dict(env)
        if tmpdir:
            env_patch["TMPDIR"] = tmpdir
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch.dict(os.environ, env_patch, clear=False):
            return target.main()

    def test_skip_when_url_not_set(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "AGENT_HUB_TELEMETRY_URL"}
        payload = _write_payload("/f.py")
        stdin_data = json.dumps(payload)
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch.dict(os.environ, env, clear=True):
            assert target.main() == 0

    def test_skip_for_non_target_tool(self) -> None:
        payload = {"tool_name": "Read", "tool_input": {"file_path": "/f.py"}, "tool_response": {}}
        result = self._run_main(payload, {"AGENT_HUB_TELEMETRY_URL": "http://otel:4318"})
        assert result == 0

    def test_skip_on_invalid_stdin(self) -> None:
        with patch("sys.stdin", io.StringIO("not-json")), \
             patch.dict(os.environ, {"AGENT_HUB_TELEMETRY_URL": "http://otel:4318"}):
            assert target.main() == 0

    def test_skip_when_opentelemetry_not_installed(self) -> None:
        payload = _write_payload("/f.py")
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.dict(os.environ, {"AGENT_HUB_TELEMETRY_URL": "http://otel:4318"}), \
             patch.object(target, "emit_artifact_span", side_effect=ImportError):
            assert target.main() == 0

    def test_skip_on_emit_exception(self) -> None:
        payload = _write_payload("/f.py")
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.dict(os.environ, {"AGENT_HUB_TELEMETRY_URL": "http://otel:4318"}), \
             patch.object(target, "emit_artifact_span", side_effect=RuntimeError("fail")):
            assert target.main() == 0

    @unittest.skipUnless(_has_opentelemetry(), "opentelemetry not installed")
    def test_msg_id_passed_from_state_file(self) -> None:
        """emit_span.save_msg_id() で保存した msg_id が emit_artifact_span() に渡される。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # send_message hook が msg_id を保存
            emit_span.save_msg_id.__module__  # ensure imported
            with patch.dict(os.environ, {"TMPDIR": tmpdir}):
                emit_span.save_msg_id("sess-xyz", "linked-msg-id")

            payload = _write_payload("/f.py")
            payload["session_id"] = "sess-xyz"

            with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                 patch.dict(os.environ, {
                     "AGENT_HUB_TELEMETRY_URL": "http://otel:4318",
                     "TMPDIR": tmpdir,
                 }), \
                 patch.object(target, "emit_artifact_span") as mock_emit:
                target.main()

            call_kwargs = mock_emit.call_args[1]
            assert call_kwargs["msg_id"] == "linked-msg-id"

    @unittest.skipUnless(_has_opentelemetry(), "opentelemetry not installed")
    def test_msg_id_empty_when_no_state_file(self) -> None:
        """状態ファイルが存在しない場合 msg_id は空文字。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = _write_payload("/f.py")
            payload["session_id"] = "never-saved-session"

            with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
                 patch.dict(os.environ, {
                     "AGENT_HUB_TELEMETRY_URL": "http://otel:4318",
                     "TMPDIR": tmpdir,
                 }), \
                 patch.object(target, "emit_artifact_span") as mock_emit:
                target.main()

            call_kwargs = mock_emit.call_args[1]
            assert call_kwargs["msg_id"] == ""

    @unittest.skipUnless(_has_opentelemetry(), "opentelemetry not installed")
    def test_emit_called_with_correct_span_name_for_git_commit(self) -> None:
        """git commit Bash コマンドで plugin.artifact.git_commit span が emit される。"""
        output = "[main abc1234] message"
        payload = _bash_payload("git commit -m 'message'", output)
        payload["session_id"] = "sess-git"

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.dict(os.environ, {
                 "AGENT_HUB_TELEMETRY_URL": "http://otel:4318",
                 "TMPDIR": tmpdir,
             }), \
             patch.object(target, "emit_artifact_span") as mock_emit:
            target.main()

        mock_emit.assert_called_once()
        assert mock_emit.call_args[1]["span_name"] == "plugin.artifact.git_commit"
        assert mock_emit.call_args[1]["attributes"]["artifact.commit_hash"] == "abc1234"

    @unittest.skipUnless(_has_opentelemetry(), "opentelemetry not installed")
    def test_emit_called_with_correct_span_name_for_pr_create(self) -> None:
        """gh pr create で plugin.artifact.pr_create span が emit される。"""
        output = "https://github.com/owner/repo/pull/55\n"
        payload = _bash_payload("gh pr create --title 'x'", output)
        payload["session_id"] = "sess-pr"

        with tempfile.TemporaryDirectory() as tmpdir, \
             patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.dict(os.environ, {
                 "AGENT_HUB_TELEMETRY_URL": "http://otel:4318",
                 "TMPDIR": tmpdir,
             }), \
             patch.object(target, "emit_artifact_span") as mock_emit:
            target.main()

        mock_emit.assert_called_once()
        assert mock_emit.call_args[1]["span_name"] == "plugin.artifact.pr_create"
        assert mock_emit.call_args[1]["attributes"]["artifact.pr_url"] == (
            "https://github.com/owner/repo/pull/55"
        )

    def test_model_defaults_to_unknown(self) -> None:
        payload = _write_payload("/f.py")
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_MODEL"}
        env["AGENT_HUB_TELEMETRY_URL"] = "http://otel:4318"
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.dict(os.environ, env, clear=True), \
             patch.object(target, "emit_artifact_span") as mock_emit:
            target.main()

        assert mock_emit.call_args[1]["model"] == "unknown"


if __name__ == "__main__":
    unittest.main()
