#!/usr/bin/env python3
"""PostToolUse hook: emit OTLP span after mcp__agent-hub__send_message (issue #26).

Claude Code の PostToolUse hook として呼び出される。
stdin から hook ペイロード (JSON) を受け取り、send_message レスポンスの
``id`` フィールド（agent-hub message ID）をキャプチャして OTLP span を emit する。

**Opt-in**: ``AGENT_HUB_TELEMETRY_URL`` が未設定の場合は何もしない（サイレント skip）。
**耐障害性**: 例外はすべて握り潰す — hook の失敗で Claude Code を止めない。

**msg_id 引き継ぎ (issue #28)**:
  msg_id 抽出後、セッション分離一時ファイルに保存する。
  ``emit_artifact_span.py`` (Write/Edit/Bash hook) はこのファイルを読み、
  msg_id を join key として artifact span と紐付ける。
  ファイルパス: ``/tmp/agent-hub-msg-id-{session_id}``

使い方 (hooks.json または ~/.claude/settings.json 内):
  {
    "hooks": {
      "PostToolUse": [
        {
          "matcher": "mcp__agent-hub__send_message",
          "hooks": [
            {
              "type": "command",
              "command": "python3 \\"${CLAUDE_PLUGIN_ROOT}/skills/agent-hub/scripts/emit_span.py\\""
            }
          ]
        }
      ]
    }
  }

Span 属性 (bridges#91 と同一 — GenAI semantic conventions + custom):
  - ``msg_id``                          : agent-hub message ID (送信メッセージの id フィールド)
  - ``gen_ai.request.model``            : ANTHROPIC_MODEL 環境変数 (未設定時は "unknown")
  - ``gen_ai.usage.input_tokens``       : 0 (PostToolUse hook では LLM usage 非取得)
  - ``gen_ai.usage.output_tokens``      : 0 (同上)
  - ``gen_ai.usage.cache_read.input_tokens``: 0 (同上)

注意:
  token usage は LLM の内部値であり PostToolUse hook では取得不可。
  bridge-claude (bridges#91) では ResultMessage.usage から取得するが、
  plugin 側の hook はツール入出力のみを受け取るため 0 固定となる。
  msg_id がテレメトリ平面とメッセージ平面の join key として機能する (設計セッション 2026-05-31)。

依存ライブラリ (opt-in で自動 skip):
  pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Public API (テスト用に分割)
# ---------------------------------------------------------------------------


def get_telemetry_url() -> str | None:
    """``AGENT_HUB_TELEMETRY_URL`` を返す。未設定または空文字なら None。"""
    return os.environ.get("AGENT_HUB_TELEMETRY_URL") or None


# ---------------------------------------------------------------------------
# msg_id 状態ファイル (issue #28: artifact hook との引き継ぎ用)
# ---------------------------------------------------------------------------


def _state_path(session_id: str) -> Path:
    """セッション分離された msg_id 一時ファイルのパスを返す。

    session_id にはファイル名として安全な文字のみを残す（パストラバーサル対策）。
    """
    tmp = Path(os.environ.get("TMPDIR", "/tmp"))
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64]
    return tmp / f"agent-hub-msg-id-{safe}"


def save_msg_id(session_id: str, msg_id: str) -> None:
    """msg_id を一時ファイルに保存する（非同期 hook 間の引き継ぎ用、非 fatal）。

    ``emit_artifact_span.py`` が ``load_msg_id()`` で読み出す。
    書き込み失敗は握り潰す — span 送信より優先度が低い。

    Args:
        session_id: Claude Code セッション ID（hook ペイロードの session_id）。
        msg_id: 保存する agent-hub message ID。
    """
    try:
        _state_path(session_id).write_text(msg_id, encoding="utf-8")
    except OSError:
        pass  # non-fatal


def load_msg_id(session_id: str) -> str:
    """セッションの最新 msg_id を読み出す。ファイル不在・読み取り失敗時は空文字。

    Args:
        session_id: Claude Code セッション ID（hook ペイロードの session_id）。

    Returns:
        保存済み msg_id 文字列。未保存または読み取りエラー時は ''。
    """
    try:
        p = _state_path(session_id)
        return p.read_text(encoding="utf-8").strip() if p.exists() else ""
    except OSError:
        return ""


def parse_msg_id(hook_payload: dict[str, Any]) -> str | None:
    """PostToolUse hook ペイロードから agent-hub message ID を抽出する。

    send_message レスポンス JSON には ``id`` フィールドが含まれる:
      {"id": "<uuid>", "from": "@sender", "to": "@recipient", ...}

    Claude Code は MCP ツールのレスポンスを content 配列の text として返す:
      tool_response = {"content": [{"type": "text", "text": "<JSON 文字列>"}], "isError": false}

    または tool_response 自体が文字列・直接辞書の場合もあるため、複数フォーマットに対応。

    Args:
        hook_payload: stdin から読んだ PostToolUse hook JSON。

    Returns:
        msg_id 文字列、抽出不能な場合は None。
    """
    tool_response = hook_payload.get("tool_response")
    if tool_response is None:
        return None

    # ---- 文字列の場合: JSON パース試行 ----
    if isinstance(tool_response, str):
        try:
            tool_response = json.loads(tool_response)
        except (json.JSONDecodeError, ValueError):
            return None

    if not isinstance(tool_response, dict):
        return None

    # エラーレスポンスはスキップ
    if tool_response.get("isError"):
        return None

    # ---- MCP format: content 配列の text 要素から抽出 ----
    content = tool_response.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        # server の send_message は "id" フィールドで返す
                        msg_id = parsed.get("id") or parsed.get("msg_id")
                        if msg_id:
                            return str(msg_id)
                except (json.JSONDecodeError, ValueError):
                    continue

    # ---- tool_response 自体に直接 id / msg_id がある場合 ----
    msg_id = tool_response.get("id") or tool_response.get("msg_id")
    if msg_id:
        return str(msg_id)

    return None


def emit_span(msg_id: str, model: str, telemetry_url: str) -> None:
    """send_message 1 呼び出し後に OTLP span を emit する (issue #26).

    ``opentelemetry-sdk`` / ``opentelemetry-exporter-otlp-proto-http`` が
    インストールされていない場合は ImportError を raise する（呼び出し元が処理）。

    hook スクリプトは短命プロセスのため、BatchSpanProcessor ではなく
    SimpleSpanProcessor を使用し、span 終了時に同期エクスポートする。

    Args:
        msg_id: agent-hub message ID（send_message レスポンスの ``id`` フィールド）。
        model:  Claude Code の model 名 (例: "claude-sonnet-4-5")。
        telemetry_url: OTLP エンドポイント URL (``/v1/traces`` を自動付与)。

    Raises:
        ImportError: opentelemetry パッケージが未インストール。
        Exception:   その他のエラー（呼び出し元が握り潰す）。
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import StatusCode

    endpoint = telemetry_url.rstrip("/") + "/v1/traces"
    exporter = OTLPSpanExporter(endpoint=endpoint)
    provider = TracerProvider()
    # hook は短命プロセス: SimpleSpanProcessor で span 終了時に即エクスポート
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # グローバル TracerProvider を書き換えずに tracer を取得 (テスタビリティ向上)
    tracer = provider.get_tracer("agent-hub-plugin")

    with tracer.start_as_current_span("plugin.send_message") as span:
        span.set_attribute("msg_id", msg_id)
        span.set_attribute("gen_ai.request.model", model)
        # token usage は PostToolUse hook 経由では取得不可 (LLM 内部値)
        # bridge-claude (bridges#91) と属性名を合わせるため 0 で emit する
        span.set_attribute("gen_ai.usage.input_tokens", 0)
        span.set_attribute("gen_ai.usage.output_tokens", 0)
        span.set_attribute("gen_ai.usage.cache_read.input_tokens", 0)
        span.set_status(StatusCode.OK)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    """Entry point。0 を返す（成功 / skip とも）。hook 失敗で Claude Code を止めない。"""

    # ---- opt-in guard ----
    telemetry_url = get_telemetry_url()
    if telemetry_url is None:
        return 0

    # ---- stdin から hook ペイロードを読む ----
    try:
        payload_str = sys.stdin.read()
        hook_payload: dict[str, Any] = json.loads(payload_str)
    except Exception:
        return 0  # 解析失敗 → サイレント skip

    # ---- msg_id 抽出 ----
    msg_id = parse_msg_id(hook_payload)
    if msg_id is None:
        return 0  # msg_id なし (エラーレスポンス等) → サイレント skip

    # ---- msg_id 保存 (artifact hook 引き継ぎ用, issue #28) ----
    session_id = str(hook_payload.get("session_id", ""))
    if session_id:
        save_msg_id(session_id, msg_id)

    # ---- model 取得 ----
    # optional with documented default — ANTHROPIC_MODEL は Claude Code runtime が設定する。
    # 未設定時は 'unknown' を gen_ai.request.model telemetry label として使用する。
    model = os.environ.get("ANTHROPIC_MODEL", "unknown")

    # ---- span emit ----
    try:
        emit_span(msg_id, model, telemetry_url)
    except ImportError:
        # opentelemetry 未インストール → サイレント skip
        # インストール方法: pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
        return 0
    except Exception:
        # その他エラー → サイレント skip (hook でクラッシュしない)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
