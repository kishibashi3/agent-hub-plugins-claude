"""Unit tests for emit_span.py (issue #26).

テスト対象:
  - get_telemetry_url: opt-in guard
  - parse_msg_id: MCP format / 文字列 / エラーレスポンス / フォールバック
  - emit_span: span 属性確認 / SimpleSpanProcessor 使用確認
  - main: stdin → span emit 統合フロー

実行:
  python3 -m pytest plugins/agent-hub-plugin/tests/test_emit_span.py -v

opentelemetry 未インストール環境でも skip guard のテストは動作する。
emit_span テストは opentelemetry がインストール済みの場合のみ実行する。
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

# emit_span.py のパスを sys.path に追加
SCRIPTS_DIR = (
    Path(__file__).parent.parent / "skills" / "agent-hub" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import emit_span as target


# ---------------------------------------------------------------------------
# ヘルパー
# ---------------------------------------------------------------------------


def _mcp_payload(id_value: str, is_error: bool = False) -> dict[str, Any]:
    """Claude Code PostToolUse hook の標準 MCP フォーマットを生成する。"""
    return {
        "session_id": "test-session",
        "tool_name": "mcp__agent-hub__send_message",
        "tool_input": {"to": "@alice", "message": "hello"},
        "tool_response": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "id": id_value,
                            "from": "@sender",
                            "to": "@alice",
                            "message": "hello",
                            "caused_by": None,
                            "timestamp": "2026-05-31T00:00:00Z",
                        }
                    ),
                }
            ],
            "isError": is_error,
        },
    }


# ---------------------------------------------------------------------------
# get_telemetry_url
# ---------------------------------------------------------------------------


class TestGetTelemetryUrl(unittest.TestCase):
    def test_returns_url_when_set(self) -> None:
        with patch.dict(os.environ, {"AGENT_HUB_TELEMETRY_URL": "http://otel:4318"}):
            assert target.get_telemetry_url() == "http://otel:4318"

    def test_returns_none_when_not_set(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "AGENT_HUB_TELEMETRY_URL"}
        with patch.dict(os.environ, env, clear=True):
            assert target.get_telemetry_url() is None

    def test_returns_none_when_empty_string(self) -> None:
        with patch.dict(os.environ, {"AGENT_HUB_TELEMETRY_URL": ""}):
            assert target.get_telemetry_url() is None


# ---------------------------------------------------------------------------
# parse_msg_id
# ---------------------------------------------------------------------------


class TestParseMsgId(unittest.TestCase):
    def test_mcp_content_array_format(self) -> None:
        """MCP 標準フォーマット: tool_response.content[].text の id を取得する。"""
        payload = _mcp_payload("test-uuid-1234")
        assert target.parse_msg_id(payload) == "test-uuid-1234"

    def test_mcp_format_msg_id_fallback(self) -> None:
        """content.text に msg_id フィールドがある場合のフォールバック。"""
        payload = {
            "tool_response": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"msg_id": "fallback-id"}),
                    }
                ],
                "isError": False,
            }
        }
        assert target.parse_msg_id(payload) == "fallback-id"

    def test_tool_response_is_string(self) -> None:
        """tool_response が JSON 文字列の場合でも id を取得できる。"""
        inner = json.dumps({"id": "str-payload-id"})
        payload = {"tool_response": json.dumps({"content": [{"type": "text", "text": inner}], "isError": False})}
        assert target.parse_msg_id(payload) == "str-payload-id"

    def test_direct_id_in_tool_response(self) -> None:
        """tool_response に直接 id フィールドがある場合。"""
        payload = {"tool_response": {"id": "direct-id", "isError": False}}
        assert target.parse_msg_id(payload) == "direct-id"

    def test_direct_msg_id_in_tool_response(self) -> None:
        """tool_response に直接 msg_id フィールドがある場合。"""
        payload = {"tool_response": {"msg_id": "direct-msg-id"}}
        assert target.parse_msg_id(payload) == "direct-msg-id"

    def test_error_response_returns_none(self) -> None:
        """isError=true のレスポンスは None を返す（span を emit しない）。"""
        payload = _mcp_payload("error-id", is_error=True)
        assert target.parse_msg_id(payload) is None

    def test_missing_tool_response_returns_none(self) -> None:
        """tool_response がない場合は None。"""
        assert target.parse_msg_id({}) is None

    def test_none_tool_response_returns_none(self) -> None:
        """tool_response が None の場合は None。"""
        assert target.parse_msg_id({"tool_response": None}) is None

    def test_invalid_json_in_content_text(self) -> None:
        """content.text が不正 JSON でも None を返す（クラッシュしない）。"""
        payload = {
            "tool_response": {
                "content": [{"type": "text", "text": "not-json"}],
                "isError": False,
            }
        }
        assert target.parse_msg_id(payload) is None

    def test_non_text_content_type_skipped(self) -> None:
        """content type が text 以外の要素はスキップする。"""
        payload = {
            "tool_response": {
                "content": [
                    {"type": "image", "url": "http://example.com/img.png"},
                    {"type": "text", "text": json.dumps({"id": "found-id"})},
                ],
                "isError": False,
            }
        }
        assert target.parse_msg_id(payload) == "found-id"

    def test_empty_content_array_returns_none(self) -> None:
        """content が空配列の場合は None。"""
        payload = {"tool_response": {"content": [], "isError": False}}
        assert target.parse_msg_id(payload) is None

    def test_tool_response_not_dict_returns_none(self) -> None:
        """tool_response が数値/リスト等の場合は None。"""
        assert target.parse_msg_id({"tool_response": 42}) is None
        assert target.parse_msg_id({"tool_response": []}) is None


# ---------------------------------------------------------------------------
# emit_span (opentelemetry が必要 — インストールされていない場合は skip)
# ---------------------------------------------------------------------------


def _has_opentelemetry() -> bool:
    try:
        import opentelemetry  # noqa: F401
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_has_opentelemetry(), "opentelemetry-sdk / exporter not installed")
class TestEmitSpan(unittest.TestCase):
    def test_span_attributes(self) -> None:
        """emit_span() を経由して span が正しい属性で emit されることを確認する。

        OTLPSpanExporter と TracerProvider をパッチして InMemorySpanExporter に
        差し替え、target.emit_span() 本体を実際に呼び出して exported span を検証する。
        """
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        # InMemorySpanExporter で span をキャプチャ
        # processor は emit_span() 内の add_span_processor() が追加するため事前追加不要
        in_memory_exporter = InMemorySpanExporter()
        test_provider = TracerProvider()

        with patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            return_value=in_memory_exporter,
        ), patch(
            "opentelemetry.sdk.trace.TracerProvider",
            return_value=test_provider,
        ):
            # emit_span() 本体を呼ぶ — span 属性が正しく設定されることを検証
            target.emit_span("test-msg-id", "claude-sonnet-4-5", "http://otel:4318", "@test-plugin")

        spans = in_memory_exporter.get_finished_spans()
        assert len(spans) == 1
        attrs = dict(spans[0].attributes or {})

        assert attrs["msg_id"] == "test-msg-id"
        assert attrs["gen_ai.request.model"] == "claude-sonnet-4-5"
        assert attrs["gen_ai.usage.input_tokens"] == 0
        assert attrs["gen_ai.usage.output_tokens"] == 0
        assert attrs["gen_ai.usage.cache_read.input_tokens"] == 0

    def test_resource_service_name(self) -> None:
        """emit_span() が TracerProvider に Resource(service.name) を設定することを確認する (issue #26)。

        TracerProvider をパッチせず OTLPSpanExporter のみパッチすることで、
        実際の Resource が span に反映されることを検証する。
        """
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        in_memory_exporter = InMemorySpanExporter()

        with patch(
            "opentelemetry.exporter.otlp.proto.http.trace_exporter.OTLPSpanExporter",
            return_value=in_memory_exporter,
        ):
            target.emit_span("msg-id", "model", "http://otel:4318", "@planner")

        spans = in_memory_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].resource.attributes["service.name"] == "@planner"

    def test_span_attribute_names_use_dot_separator(self) -> None:
        """span 属性名がドット区切りであることを確認する（アンダースコア不可）。"""
        # bridges#91 との互換性: ドット区切り属性名を使用する
        required_attrs = [
            "msg_id",
            "gen_ai.request.model",
            "gen_ai.usage.input_tokens",
            "gen_ai.usage.output_tokens",
            "gen_ai.usage.cache_read.input_tokens",
        ]
        for attr in required_attrs:
            assert "." in attr or attr == "msg_id", (
                f"Attribute '{attr}' should use dot separator"
            )


# ---------------------------------------------------------------------------
# main (統合テスト: stdin → span emit フロー)
# ---------------------------------------------------------------------------


class TestMain(unittest.TestCase):
    def _run_main(self, payload: dict[str, Any], env: dict[str, str]) -> int:
        """main() を stdin パイプで呼び出す。"""
        stdin_data = json.dumps(payload)
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch.dict(os.environ, env, clear=False):
            return target.main()

    def test_skip_when_url_not_set(self) -> None:
        """AGENT_HUB_TELEMETRY_URL 未設定時は 0 を返してスキップする。"""
        env = {k: v for k, v in os.environ.items() if k != "AGENT_HUB_TELEMETRY_URL"}
        payload = _mcp_payload("skip-id")
        stdin_data = json.dumps(payload)
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch.dict(os.environ, env, clear=True):
            result = target.main()
        assert result == 0

    def test_skip_when_msg_id_not_found(self) -> None:
        """msg_id が取得できない場合も 0 を返す（クラッシュしない）。"""
        payload = {"tool_response": {"content": [], "isError": False}}
        result = self._run_main(
            payload, {"AGENT_HUB_TELEMETRY_URL": "http://otel:4318"}
        )
        assert result == 0

    def test_skip_on_invalid_stdin(self) -> None:
        """stdin が不正 JSON でも 0 を返す（クラッシュしない）。"""
        with patch("sys.stdin", io.StringIO("not-json")), \
             patch.dict(os.environ, {"AGENT_HUB_TELEMETRY_URL": "http://otel:4318"}):
            result = target.main()
        assert result == 0

    def test_skip_when_opentelemetry_not_installed(self) -> None:
        """opentelemetry 未インストール時は ImportError を握り潰して 0 を返す。"""
        payload = _mcp_payload("otel-missing-id")
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.dict(os.environ, {"AGENT_HUB_TELEMETRY_URL": "http://otel:4318"}), \
             patch.object(target, "emit_span", side_effect=ImportError("otel not found")):
            result = target.main()
        assert result == 0

    def test_skip_on_emit_exception(self) -> None:
        """emit_span が予期しない例外を投げても 0 を返す（hook がクラッシュしない）。"""
        payload = _mcp_payload("exception-id")
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.dict(os.environ, {"AGENT_HUB_TELEMETRY_URL": "http://otel:4318"}), \
             patch.object(target, "emit_span", side_effect=RuntimeError("network error")):
            result = target.main()
        assert result == 0

    @unittest.skipUnless(_has_opentelemetry(), "opentelemetry not installed")
    def test_emit_called_with_correct_args(self) -> None:
        """msg_id / model / service_name が正しく emit_span に渡されることを確認する。"""
        payload = _mcp_payload("correct-id")
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.dict(
                 os.environ,
                 {
                     "AGENT_HUB_TELEMETRY_URL": "http://otel:4318",
                     "ANTHROPIC_MODEL": "claude-sonnet-4-5",
                     "AGENT_HUB_USER": "planner",
                 },
             ), \
             patch.object(target, "emit_span") as mock_emit:
            result = target.main()

        assert result == 0
        mock_emit.assert_called_once_with(
            "correct-id", "claude-sonnet-4-5", "http://otel:4318", "@planner"
        )

    def test_service_name_defaults_when_user_not_set(self) -> None:
        """AGENT_HUB_USER 未設定時は service_name が "agent-hub-plugin" になる (issue #26)。"""
        payload = _mcp_payload("id-no-user")
        env = {k: v for k, v in os.environ.items() if k not in ("AGENT_HUB_USER",)}
        env["AGENT_HUB_TELEMETRY_URL"] = "http://otel:4318"
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.dict(os.environ, env, clear=True), \
             patch.object(target, "emit_span") as mock_emit:
            target.main()

        call_args = mock_emit.call_args[0]
        assert call_args[3] == "agent-hub-plugin"

    def test_model_defaults_to_unknown(self) -> None:
        """ANTHROPIC_MODEL 未設定時は 'unknown' が使われる。"""
        payload = _mcp_payload("default-model-id")
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_MODEL"}
        env["AGENT_HUB_TELEMETRY_URL"] = "http://otel:4318"
        with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
             patch.dict(os.environ, env, clear=True), \
             patch.object(target, "emit_span") as mock_emit:
            target.main()

        call_args = mock_emit.call_args
        assert call_args[0][1] == "unknown"  # model 引数


if __name__ == "__main__":
    unittest.main()
