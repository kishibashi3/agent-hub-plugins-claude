#!/usr/bin/env bash
# agent-hub plugin: SessionStart hook
#
# session 開始時に自動で agent-hub skill の「オープニング」を発火させる。
# user が「agent-hub」「未読を見て」等の trigger 句を発しなくても、
# session の最初の動作として在席状態に入って未読を回収する。
#
# 設定不備 (AGENT_HUB_URL や GITHUB_PAT 未設定) の場合は何もしない (静かに skip)。
# additionalContext を JSON で stdout に流すと Claude Code がそれを context に
# 注入し、Claude は SKILL.md の オープニング section に従って動き出す。

set -euo pipefail

# 必須環境変数の存在チェック (どちらかでも欠けてれば agent-hub に繋げない)
# AGENT_HUB_URLS (multi-hub) または AGENT_HUB_URL (single-hub) のどちらかが設定されていれば OK
if [[ -z "${AGENT_HUB_URL:-}${AGENT_HUB_URLS:-}" ]] || [[ -z "${GITHUB_PAT:-}${AGENT_HUB_USER:-}" ]]; then
  exit 0
fi

# 接続先ハブ数を数えて additionalContext に反映する
if [[ -n "${AGENT_HUB_URLS:-}" ]]; then
  # AGENT_HUB_URLS はスペースまたはカンマ区切りで複数 URL を保持する
  IFS=', ' read -ra _HUBS <<< "${AGENT_HUB_URLS}"
  _HUB_COUNT="${#_HUBS[@]}"
else
  _HUB_COUNT=1
fi

if [[ "${_HUB_COUNT}" -ge 2 ]]; then
  _CONTEXT="agent-hub plugin が有効です (${_HUB_COUNT} ハブ接続)。このセッションの最初のアクションとして、ユーザーの依頼を待たずに agent-hub skill のオープニング手順を実行してください: (1) agent-hub skill を呼び、(2) Monitor を起動して全ハブの在席状態に入り (AGENT_HUB_URLS を参照)、(3) 各ハブの未読メッセージを回収する。SKILL.md の「マルチハブ」セクションを参照のこと。"
else
  _CONTEXT="agent-hub plugin が有効です。このセッションの最初のアクションとして、ユーザーの依頼を待たずに agent-hub skill のオープニング手順を実行してください: (1) agent-hub skill を呼び、(2) Monitor を起動して在席状態に入り、(3) \`mcp__agent-hub__get_messages\` で未読メッセージを回収する。これら 3 ステップが完了するまでユーザーの依頼に進まない。すでに同セッションで Monitor が動いていれば該当ステップは skip 可。"
fi

cat <<JSON
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "${_CONTEXT}"
  }
}
JSON
