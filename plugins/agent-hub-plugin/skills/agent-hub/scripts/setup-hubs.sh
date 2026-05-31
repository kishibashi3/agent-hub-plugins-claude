#!/usr/bin/env bash
# setup-hubs.sh: AGENT_HUB_URLS から .mcp.json を自動生成する
#
# 使い方:
#   AGENT_HUB_URLS="http://hub1:3000/mcp http://hub2:3000/mcp" \
#   bash ./skills/agent-hub/scripts/setup-hubs.sh
#
# AGENT_HUB_URLS にスペースまたはカンマ区切りで hub URL を列挙すると、
# その数だけ .mcp.json に MCP サーバーエントリを生成する（N hub 対応）。
# 生成後は Claude Code を再起動して変更を反映させる。
#
# 認証環境変数（Claude Code 起動時に export して使う）:
#   Hub 1 (primary):
#     GITHUB_PAT            必須 (pat モード)
#     AGENT_HUB_USER        任意 (handle override。省略時は GitHub login をハンドルとして使用)
#     AGENT_HUB_TENANT      任意 (CE の named tenant。省略時は default tenant)
#   Hub N (N>=2):
#     GITHUB_PAT_N          任意 (省略時は GITHUB_PAT を流用)
#     AGENT_HUB_USER_N      任意 (省略時は AGENT_HUB_USER を流用)
#     AGENT_HUB_TENANT_N    任意 (省略時は空 = default tenant)
#
# 生成例 (2 hub):
#   {
#     "agent-hub": {
#       "type": "http",
#       "url": "http://hub1:3000/mcp",
#       "headers": { "Authorization": "Bearer ${GITHUB_PAT}", ... }
#     },
#     "agent-hub-2": {
#       "type": "http",
#       "url": "http://hub2:3000/mcp",
#       "headers": { "Authorization": "Bearer ${GITHUB_PAT_2:-${GITHUB_PAT}}", ... }
#     }
#   }
#
# ツール名は Claude Code が MCP サーバー名をそのまま namespace 化する:
#   hub1 → mcp__agent-hub__send_message, mcp__agent-hub__get_messages, ...
#   hub2 → mcp__agent-hub-2__send_message, mcp__agent-hub-2__get_messages, ...

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# scripts/ → agent-hub/ → skills/ → agent-hub-plugin/
PLUGIN_DIR="$(cd "$SCRIPT_DIR/../../.." && pwd)"
MCP_JSON="$PLUGIN_DIR/.mcp.json"

# ---------------------------------------------------------------------------
# AGENT_HUB_URLS のパース
# ---------------------------------------------------------------------------
if [ -z "${AGENT_HUB_URLS:-}" ]; then
  echo "ERROR: AGENT_HUB_URLS が未設定です。" >&2
  echo "" >&2
  echo "使い方:" >&2
  echo "  AGENT_HUB_URLS='http://hub1:3000/mcp http://hub2:3000/mcp' \\" >&2
  echo "  bash \"$(basename "$0")\"" >&2
  exit 1
fi

# カンマをスペースに正規化してから配列化（空要素を除去）
# unquoted word-split による glob expansion を避けるため read -ra を使用
_raw="${AGENT_HUB_URLS//,/ }"
HUBS=()
read -ra _tokens <<< "$_raw"
for _url in "${_tokens[@]}"; do
  [ -n "$_url" ] && HUBS+=("$_url")
done
HUB_COUNT="${#HUBS[@]}"

if [ "$HUB_COUNT" -eq 0 ]; then
  echo "ERROR: AGENT_HUB_URLS に有効な URL がありません。" >&2
  exit 1
fi

echo "[setup-hubs] $HUB_COUNT hub(s) を検出。$MCP_JSON を生成中..."

# ---------------------------------------------------------------------------
# .mcp.json の生成
# ---------------------------------------------------------------------------
{
  printf '{\n'

  for i in "${!HUBS[@]}"; do
    n=$((i + 1))
    hub_url="${HUBS[$i]}"

    # hub名とauth環境変数参照を決定
    # hub 1 はプライマリ変数を使用（後方互換性維持）
    # hub N (N>=2) は _N サフィックス変数を使用、省略時はプライマリにフォールバック
    if [ "$i" -eq 0 ]; then
      _name="agent-hub"
      _pat='${GITHUB_PAT}'
      _user='${AGENT_HUB_USER:-}'
      _tenant='${AGENT_HUB_TENANT:-}'
    else
      _name="agent-hub-${n}"
      _pat="\${GITHUB_PAT_${n}:-\${GITHUB_PAT}}"
      _user="\${AGENT_HUB_USER_${n}:-\${AGENT_HUB_USER:-}}"
      _tenant="\${AGENT_HUB_TENANT_${n}:-}"
    fi

    # エントリ間のカンマ区切り
    [ "$i" -gt 0 ] && printf ',\n'

    # URL を JSON 文字列として安全に埋め込む（\ と " をエスケープ）
    _escaped_url="${hub_url//\\/\\\\}"
    _escaped_url="${_escaped_url//\"/\\\"}"

    printf '  "%s": {\n' "$_name"
    printf '    "type": "http",\n'
    printf '    "url": "%s",\n' "$_escaped_url"
    printf '    "headers": {\n'
    printf '      "Authorization": "Bearer %s",\n' "$_pat"
    printf '      "X-User-Id": "%s",\n' "$_user"
    printf '      "X-Tenant-Id": "%s"\n' "$_tenant"
    printf '    }\n'
    printf '  }'
  done

  printf '\n}\n'
} > "$MCP_JSON"

# ---------------------------------------------------------------------------
# 結果の表示
# ---------------------------------------------------------------------------
echo "[setup-hubs] 完了: $MCP_JSON"
echo ""
for i in "${!HUBS[@]}"; do
  n=$((i + 1))
  _name="agent-hub"
  [ "$i" -gt 0 ] && _name="agent-hub-${n}"
  printf "  hub%-2d  %s\n" "$n" "${HUBS[$i]}"
  printf "        → ツール: mcp__%s__send_message, mcp__%s__get_messages, ...\n" "$_name" "$_name"
done
echo ""
echo "変更を反映するには Claude Code を再起動してください。"
echo "  (env 変数は起動時に固定されるため /reload-plugins では反映されません)"
