#!/usr/bin/env python3
"""PostToolUse hook: emit OTLP artifact span for Write / Edit / Bash tools (issue #28).

Claude Code の PostToolUse hook として呼び出される。
Write/Edit でのファイル書き込み、Bash での git commit / PR 作成を
OTLP span に記録する。

**Opt-in**: ``AGENT_HUB_TELEMETRY_URL`` が未設定の場合は何もしない（サイレント skip）。
**耐障害性**: 例外はすべて握り潰す — hook の失敗で Claude Code を止めない。

**msg_id 引き継ぎ**:
  ``emit_span.py``（send_message hook）が保存した一時ファイルから
  セッションの最新 msg_id を読み出し、span の join key として使用する。
  未保存の場合は空文字（span は emit するが msg_id 属性は空）。

**Bash フィルタリング**:
  Bash hook はすべての bash 呼び出しで発火するが、本スクリプトは
  ``git commit`` / ``gh pr create`` を含むコマンドのみ span を emit する。

使い方 (hooks.json 内):
  {
    "hooks": {
      "PostToolUse": [
        {"matcher": "Write",  "hooks": [{"type": "command", "command": "python3 ..."}]},
        {"matcher": "Edit",   "hooks": [{"type": "command", "command": "python3 ..."}]},
        {"matcher": "Bash",   "hooks": [{"type": "command", "command": "python3 ..."}]}
      ]
    }
  }

Span 名とその属性:
  plugin.artifact.file_write  : artifact.type / artifact.path
  plugin.artifact.file_edit   : artifact.type / artifact.path
  plugin.artifact.git_commit  : artifact.type / artifact.commit_hash / artifact.command
  plugin.artifact.pr_create   : artifact.type / artifact.pr_url / artifact.command
  すべての span に共通: msg_id / gen_ai.request.model

依存ライブラリ (opt-in で自動 skip):
  pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

# emit_span.py と同じ scripts/ ディレクトリにあるため、直接実行時は
# sys.path[0] がそのディレクトリになり import 可能。
# テストでは test_emit_artifact_span.py が sys.path に scripts/ を追加する。
from emit_span import get_telemetry_url, load_msg_id


# ---------------------------------------------------------------------------
# 正規表現パターン
# ---------------------------------------------------------------------------

# git commit output: "[branch-name abc1234] commit message"
_GIT_COMMIT_HASH_RE = re.compile(r"\[[\w/.\-]+\s+([0-9a-f]{7,40})\]")

# gh pr create output: URL like https://github.com/owner/repo/pull/123
_PR_URL_RE = re.compile(r"https://github\.com/[^\s]+/pull/\d+")

# Bash コマンド中の "git commit" キーワード
_GIT_COMMIT_CMDS = ("git commit",)

# Bash コマンド中の "gh pr create" キーワード
_GH_PR_CREATE_CMD = "gh pr create"

# Bash コマンド中の "gh pr merge" キーワード
_GH_PR_MERGE_CMD = "gh pr merge"


# ---------------------------------------------------------------------------
# Artifact 抽出
# ---------------------------------------------------------------------------


def _extract_text_output(tool_response: Any) -> str:
    """tool_response から text コンテンツを抽出する。

    MCP content array フォーマット / 文字列の両方に対応。
    """
    if isinstance(tool_response, str):
        return tool_response
    if not isinstance(tool_response, dict):
        return ""
    content = tool_response.get("content", [])
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts)


def parse_artifact(hook_payload: dict[str, Any]) -> dict[str, Any] | None:
    """PostToolUse hook ペイロードから artifact メタデータを抽出する。

    対象ツール:
      - ``Write``  → plugin.artifact.file_write
      - ``Edit``   → plugin.artifact.file_edit
      - ``Bash``   → git commit なら plugin.artifact.git_commit
                     gh pr なら plugin.artifact.pr_create
                     それ以外はスキップ

    Args:
        hook_payload: stdin から読んだ PostToolUse hook JSON。

    Returns:
        ``{"span_name": str, "attributes": dict}`` または None（スキップ）。
    """
    tool_name: str = str(hook_payload.get("tool_name", ""))
    tool_input: dict[str, Any] = hook_payload.get("tool_input") or {}
    tool_response: Any = hook_payload.get("tool_response")

    # エラーレスポンスはスキップ（失敗した操作は記録しない）
    if isinstance(tool_response, dict) and tool_response.get("isError"):
        return None

    # ---- Write ----
    if tool_name == "Write":
        file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if not file_path:
            return None
        return {
            "span_name": "plugin.artifact.file_write",
            "attributes": {
                "artifact.type": "file_write",
                "artifact.path": file_path,
            },
        }

    # ---- Edit ----
    if tool_name == "Edit":
        file_path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        if not file_path:
            return None
        return {
            "span_name": "plugin.artifact.file_edit",
            "attributes": {
                "artifact.type": "file_edit",
                "artifact.path": file_path,
            },
        }

    # ---- Bash ----
    if tool_name == "Bash":
        command: str = str(tool_input.get("command") or "")
        output: str = _extract_text_output(tool_response)

        # git commit
        if any(kw in command for kw in _GIT_COMMIT_CMDS):
            m = _GIT_COMMIT_HASH_RE.search(output)
            commit_hash = m.group(1) if m else ""
            return {
                "span_name": "plugin.artifact.git_commit",
                "attributes": {
                    "artifact.type": "git_commit",
                    "artifact.commit_hash": commit_hash,
                    "artifact.command": command[:256],  # 長大コマンドを truncate
                },
            }

        # gh pr create
        if _GH_PR_CREATE_CMD in command:
            m = _PR_URL_RE.search(output)
            pr_url = m.group(0) if m else ""
            return {
                "span_name": "plugin.artifact.pr_create",
                "attributes": {
                    "artifact.type": "pr_create",
                    "artifact.pr_url": pr_url,
                    "artifact.command": command[:256],
                },
            }

        # gh pr merge — create と意味が異なるため別 span 名で区別 (Minor #3)
        if _GH_PR_MERGE_CMD in command:
            m = _PR_URL_RE.search(output)
            pr_url = m.group(0) if m else ""
            return {
                "span_name": "plugin.artifact.pr_merge",
                "attributes": {
                    "artifact.type": "pr_merge",
                    "artifact.pr_url": pr_url,
                    "artifact.command": command[:256],
                },
            }

    return None  # 対象外のツール / Bash コマンドはスキップ


# ---------------------------------------------------------------------------
# Span emit
# ---------------------------------------------------------------------------


def emit_artifact_span(
    span_name: str,
    attributes: dict[str, Any],
    msg_id: str,
    model: str,
    telemetry_url: str,
    service_name: str,
) -> None:
    """artifact OTLP span を emit する (issue #28).

    ``opentelemetry-sdk`` / ``opentelemetry-exporter-otlp-proto-http`` が
    インストールされていない場合は ImportError を raise する（呼び出し元が処理）。

    hook スクリプトは短命プロセスのため SimpleSpanProcessor を使用する。

    Args:
        span_name:     span 名 (例: "plugin.artifact.git_commit")。
        attributes:    artifact 固有属性 dict (artifact.type 等)。
        msg_id:        join key（空文字の場合は属性として空文字を設定）。
        model:         ANTHROPIC_MODEL 環境変数の値。
        telemetry_url: OTLP エンドポイント URL (``/v1/traces`` を自動付与)。
        service_name:  OTel ``service.name`` リソース属性 (例: "@planner")。
                       ``AGENT_HUB_PARTICIPANT`` 環境変数 (deprecated alias の ``AGENT_HUB_USER`` も fallback) から ``f"@{handle}"`` で設定する (issue #26)。

    Raises:
        ImportError: opentelemetry パッケージが未インストール。
        Exception:   その他エラー（呼び出し元が握り潰す）。
    """
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace import StatusCode

    endpoint = telemetry_url.rstrip("/") + "/v1/traces"
    exporter = OTLPSpanExporter(endpoint=endpoint)
    # issue #26: service.name を @handle 名に設定する (Resource 経由; bridges#96 と同パターン)
    resource = Resource({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    # hook は短命プロセス: SimpleSpanProcessor で span 終了時に即エクスポート
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # グローバル TracerProvider を書き換えずに tracer を取得 (テスタビリティ向上)
    tracer = provider.get_tracer("agent-hub-plugin")

    with tracer.start_as_current_span(span_name) as span:
        # join key: send_message span と telemetry 平面で紐付けるための msg_id
        span.set_attribute("msg_id", msg_id)
        # optional with documented default — ANTHROPIC_MODEL は Claude Code runtime が設定する。
        span.set_attribute("gen_ai.request.model", model)
        for key, value in attributes.items():
            span.set_attribute(key, value)
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

    # ---- artifact 抽出 ----
    artifact = parse_artifact(hook_payload)
    if artifact is None:
        return 0  # 対象外ツール / エラーレスポンス → サイレント skip

    # ---- msg_id 読み出し (emit_span.py が保存した状態ファイルから) ----
    session_id = str(hook_payload.get("session_id", ""))
    msg_id = load_msg_id(session_id) if session_id else ""

    # ---- model 取得 ----
    # optional with documented default — ANTHROPIC_MODEL は Claude Code runtime が設定する。
    # 未設定時は 'unknown' を gen_ai.request.model telemetry label として使用する。
    model = os.environ.get("ANTHROPIC_MODEL", "unknown")

    # ---- service.name 取得 (issue #26: bridges#96 と同パターン) ----
    # AGENT_HUB_PARTICIPANT が設定されていれば "@{handle}" を service.name に設定する。
    # AGENT_HUB_USER は deprecated alias として後方互換で読む (scheduler.py:116 と同形式)。
    # 未設定時は "agent-hub-plugin" をフォールバックとして使用する。
    # テレメトリ専用属性のため fail-fast ではなく fallback が許容される（欠落しても動作に影響しない）。
    handle = os.environ.get("AGENT_HUB_PARTICIPANT") or os.environ.get("AGENT_HUB_USER", "")
    service_name = f"@{handle}" if handle else "agent-hub-plugin"

    # ---- span emit ----
    try:
        emit_artifact_span(
            span_name=artifact["span_name"],
            attributes=artifact["attributes"],
            msg_id=msg_id,
            model=model,
            telemetry_url=telemetry_url,
            service_name=service_name,
        )
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
