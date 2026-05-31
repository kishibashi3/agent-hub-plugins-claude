#!/usr/bin/env bash
# agent-hub-watch: 自分宛て未読メッセージの SSE push を待機する常駐スクリプト
#
# 使い方:
#   # PAT モード（推奨。GitHub PAT で認証、ハンドル=GitHub login）
#   GITHUB_PAT=ghp_xxx... bash .claude/skills/agent-hub/scripts/watch.sh
#
#   # PAT モード + ペルソナ override（同じ owner で別ハンドルを名乗る）
#   GITHUB_PAT=ghp_xxx... AGENT_HUB_USER=alice bash .claude/skills/agent-hub/scripts/watch.sh
#
#   # Trust モード（localhost のみ。サーバー側 AUTH_MODE=trust）
#   AGENT_HUB_USER=alice bash .claude/skills/agent-hub/scripts/watch.sh
#
#   # マルチハブ（複数ハブを同時監視）
#   AGENT_HUB_URLS="http://hub1:3000/mcp http://hub2:3000/mcp" \
#   GITHUB_PAT=ghp_xxx... bash .claude/skills/agent-hub/scripts/watch.sh
#
# 認証モードは agent-hub サーバー側の AUTH_MODE に合わせる:
#   - サーバー pat → GITHUB_PAT を設定（推奨）。AGENT_HUB_USER も併設すれば handle override
#   - サーバー trust（localhost 互換）→ AGENT_HUB_USER のみ
#
# 環境変数:
#   GITHUB_PAT         GitHub Personal Access Token（read:user scope）。pat モード用
#   AGENT_HUB_USER     handle 名 (trust モードでは識別、pat モードでは GitHub login を override)
#   AGENT_HUB_URL      MCP エンドポイント（単一ハブ）。未設定なら http://localhost:3000/mcp
#   AGENT_HUB_URLS     MCP エンドポイント一覧（スペースまたはカンマ区切り）。設定時は AGENT_HUB_URL より優先
#   AGENT_HUB_TENANT   tenant 識別子 (CE 接続時)。未設定なら default tenant

set -u

PAT="${GITHUB_PAT:-}"
HANDLE_OVERRIDE="${AGENT_HUB_USER:-}"
TENANT="${AGENT_HUB_TENANT:-}"

# HUBS 配列を組み立て:
#   AGENT_HUB_URLS (スペースまたはカンマ区切り) が設定されていれば優先使用。
#   未設定なら AGENT_HUB_URL (単一 URL) にフォールバック。
HUBS=()
if [ -n "${AGENT_HUB_URLS:-}" ]; then
  # カンマをスペースに正規化してから word-split で配列へ
  read -ra HUBS <<< "$(echo "${AGENT_HUB_URLS}" | tr ',' ' ')"
fi
if [ ${#HUBS[@]} -eq 0 ]; then
  HUBS=("${AGENT_HUB_URL:-http://localhost:3000/mcp}")
fi

# 認証モード判定 + USER_ID 解決 + curl 用ヘッダ配列を組み立て
AUTH_HEADERS=()
if [ -n "$TENANT" ]; then
  AUTH_HEADERS+=(-H "X-Tenant-Id: $TENANT")
fi
if [ -n "$PAT" ]; then
  # pat モード: GitHub API /user を叩いて login 取得（owner 確認）
  # HTTP code + raw body を一度に取得し、失敗原因を特定できるようにする
  _GH_WATCH_LOG="/tmp/agent-hub-watch-$$.log"
  _GH_RAW=$(curl -s -w '\nHTTP_CODE:%{http_code}' --max-time 10 \
    -H "Authorization: Bearer $PAT" \
    -H "User-Agent: agent-hub-watch" \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/user 2>&1)
  _GH_HTTP_CODE=$(printf '%s' "$_GH_RAW" | grep '^HTTP_CODE:' | cut -d: -f2)
  _GH_BODY=$(printf '%s' "$_GH_RAW" | grep -v '^HTTP_CODE:')
  GITHUB_LOGIN=$(printf '%s' "$_GH_BODY" \
    | sed -nE 's/.*"login"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -1)
  if [ -z "$GITHUB_LOGIN" ]; then
    printf '%s\n' "$_GH_RAW" > "$_GH_WATCH_LOG"
    if [ "$_GH_HTTP_CODE" = "401" ]; then
      echo "[ERR $(date +%H:%M:%S)] GITHUB_PAT rejected by GitHub API (HTTP 401) — PAT may be revoked, expired, or missing 'read:user' scope"
    elif [ -z "$_GH_HTTP_CODE" ] || [ "$_GH_HTTP_CODE" = "000" ]; then
      echo "[ERR $(date +%H:%M:%S)] could not reach GitHub API (network/proxy error?) — debug log: $_GH_WATCH_LOG"
    else
      echo "[ERR $(date +%H:%M:%S)] GitHub API returned HTTP $_GH_HTTP_CODE (login unavailable) — debug log: $_GH_WATCH_LOG"
    fi
    exit 1
  fi
  AUTH_HEADERS+=(-H "Authorization: Bearer $PAT")
  if [ -n "$HANDLE_OVERRIDE" ]; then
    # PAT で本人認証 + X-User-Id でハンドル override（マルチペルソナ）
    USER_ID="$HANDLE_OVERRIDE"
    AUTH_HEADERS+=(-H "X-User-Id: $USER_ID")
    AUTH_MODE_LABEL="pat+override(owner=$GITHUB_LOGIN)"
  else
    # 素の pat モード: GitHub login をそのままハンドルにする
    USER_ID="$GITHUB_LOGIN"
    AUTH_MODE_LABEL="pat"
  fi
elif [ -n "$HANDLE_OVERRIDE" ]; then
  # trust モード: X-User-Id を無検証で信じる（localhost 専用）
  USER_ID="$HANDLE_OVERRIDE"
  AUTH_HEADERS+=(-H "X-User-Id: $USER_ID")
  AUTH_MODE_LABEL="trust"
else
  echo "[ERR $(date +%H:%M:%S)] Set GITHUB_PAT (pat mode) or AGENT_HUB_USER (trust mode)"
  exit 1
fi

HUB_COUNT=${#HUBS[@]}
echo "[boot $(date +%H:%M:%S)] mode=$AUTH_MODE_LABEL user=$USER_ID tenant=${TENANT:-default} hubs=$HUB_COUNT"
for _i in "${!HUBS[@]}"; do
  echo "[boot $(date +%H:%M:%S)]   hub$((_i+1)): ${HUBS[$_i]}"
done

# tenant が unset の場合は「default 行き」を見落とせない強い WARN を出す。
# agent-hub#28 (= 「見えない幽霊」 bug) で報告された operational pitfall への予防策:
# AGENT_HUB_TENANT を export し忘れた / Monitor 経由で env 継承漏れ等の case で、
# 「機能はしているが is_online=false で居ないように見える」 状態を boot 時に即発見させる。
if [ -z "$TENANT" ]; then
  echo "[WARN $(date +%H:%M:%S)] AGENT_HUB_TENANT is unset → connecting to default tenant."
  echo "[WARN $(date +%H:%M:%S)]   If you expected a named tenant (= named tenant に register 済 handle で運用), abort with Ctrl-C and set AGENT_HUB_TENANT before launching."
  echo "[WARN $(date +%H:%M:%S)]   Otherwise (= default tenant 雑談室で運用 / Private Edition), 無視して OK。"
fi

# ---------------------------------------------------------------------------
# ハブ接続ループ（ハブごとに呼ばれる）
#
# 引数:
#   $1  hub_url   接続先 MCP エンドポイント
#   $2  label     ログ prefix ("hub1" など)
#
# 親シェルの AUTH_HEADERS 配列・USER_ID 変数を参照する。
# バックグラウンドサブシェルとして起動するため export は不要（fork 継承）。
#
# retry: exponential backoff (5s→10s→20s→40s→60s cap)。
#        subscribe 成功時にリセット。
# dedup: 同一エラーメッセージの連続出力を抑制し "(repeated Nx)" でサマリ表示。
# ---------------------------------------------------------------------------
_watch_hub() {
  local hub_url="$1"
  local label="$2"
  local first_connect=1

  # exponential backoff state
  local _backoff=5
  local _max_backoff=60
  # error dedup state
  local _last_err=""
  local _err_repeat=0

  while true; do
    # 1) initialize で sessionId を取り出す
    local init
    init=$(curl -s -i --max-time 10 -X POST "$hub_url" \
      "${AUTH_HEADERS[@]}" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent-hub-watch","version":"1.0"}},"id":0}' 2>/dev/null)
    local sid
    sid=$(echo "$init" | grep -i "^mcp-session-id:" | awk '{print $2}' | tr -d '\r\n')
    if [ -z "$sid" ]; then
      # 真因を特定するため HTTP status code を抽出して表示
      local _init_http_code
      _init_http_code=$(printf '%s' "$init" | grep -E "^HTTP/" | tail -1 | awk '{print $2}')
      local _err_msg
      if [ -z "$_init_http_code" ]; then
        _err_msg="initialize failed: no response from $hub_url — is agent-hub running?"
      elif [ "$_init_http_code" = "401" ]; then
        _err_msg="initialize failed: HTTP 401 Unauthorized — check GITHUB_PAT scope for $hub_url"
      else
        _err_msg="initialize failed: HTTP $_init_http_code from $hub_url"
      fi
      # dedup: 同一エラーは repeat カウントのみ。変化時に "(above repeated Nx)" を flush して新メッセージ出力
      if [ "$_err_msg" = "$_last_err" ]; then
        _err_repeat=$((_err_repeat + 1))
      else
        [ "$_err_repeat" -gt 0 ] && \
          echo "[$label ERR $(date +%H:%M:%S)] (above error repeated ${_err_repeat}x)"
        echo "[$label ERR $(date +%H:%M:%S)] $_err_msg, retry in ${_backoff}s"
        _last_err="$_err_msg"
        _err_repeat=0
      fi
      sleep "$_backoff"
      _backoff=$((_backoff * 2))
      [ "$_backoff" -gt "$_max_backoff" ] && _backoff=$_max_backoff
      continue
    fi
    if [ -n "$first_connect" ]; then
      echo "[$label init $(date +%H:%M:%S)] sessionId=${sid:0:8}... user=$USER_ID"
    else
      echo "[$label init $(date +%H:%M:%S)] sessionId=${sid:0:8}... user=$USER_ID" >&2
    fi

    # 2) initialized notification（MCP プロトコル必須）
    curl -s --max-time 5 -X POST "$hub_url" \
      "${AUTH_HEADERS[@]}" \
      -H "mcp-session-id: $sid" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -d '{"jsonrpc":"2.0","method":"notifications/initialized"}' > /dev/null 2>&1

    # 3) resources/subscribe で自分の inbox を購読
    local sub
    sub=$(curl -s --max-time 5 -X POST "$hub_url" \
      "${AUTH_HEADERS[@]}" \
      -H "mcp-session-id: $sid" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -d "{\"jsonrpc\":\"2.0\",\"method\":\"resources/subscribe\",\"params\":{\"uri\":\"inbox://@$USER_ID\"},\"id\":1}" 2>/dev/null)
    if echo "$sub" | grep -q '"error"'; then
      local _sub_msg="subscribe failed: $sub"
      if [ "$_sub_msg" = "$_last_err" ]; then
        _err_repeat=$((_err_repeat + 1))
      else
        [ "$_err_repeat" -gt 0 ] && \
          echo "[$label ERR $(date +%H:%M:%S)] (above error repeated ${_err_repeat}x)"
        echo "[$label ERR $(date +%H:%M:%S)] $_sub_msg, retry in ${_backoff}s"
        _last_err="$_sub_msg"
        _err_repeat=0
      fi
      sleep "$_backoff"
      _backoff=$((_backoff * 2))
      [ "$_backoff" -gt "$_max_backoff" ] && _backoff=$_max_backoff
      continue
    fi

    # 接続成功: dedup pending があれば flush してからリセット
    [ "$_err_repeat" -gt 0 ] && \
      echo "[$label ERR $(date +%H:%M:%S)] (above error repeated ${_err_repeat}x)"
    _backoff=5
    _last_err=""
    _err_repeat=0

    if [ -n "$first_connect" ]; then
      echo "[$label subscribed $(date +%H:%M:%S)] inbox://@$USER_ID — waiting for pushes..."
      first_connect=
    else
      echo "[$label subscribed $(date +%H:%M:%S)] inbox://@$USER_ID — waiting for pushes..." >&2
    fi

    # 4) GET /mcp で long-lived SSE。notifications/resources/updated だけ拾う。
    # awk を使用: grep --line-buffered は GNU grep 専用で macOS BSD grep 非対応のため portable awk に置換
    curl -sN -X GET "$hub_url" \
      "${AUTH_HEADERS[@]}" \
      -H "mcp-session-id: $sid" \
      -H "Accept: text/event-stream" 2>/dev/null \
      | awk '/"method":"notifications\/resources\/updated"/ { print; fflush() }' \
      | while IFS= read -r line; do
          echo "[$label NEW $(date +%H:%M:%S)] $line"
        done

    # 5) ストリーム切断時は再接続（reconnect ログは stderr で静音化）
    echo "[$label reconnect $(date +%H:%M:%S)] SSE stream closed, reconnecting in 3s..." >&2
    sleep 3
  done
}

# SIGINT/SIGTERM 受信時に全バックグラウンドジョブを終了してから exit
trap 'kill $(jobs -p) 2>/dev/null; exit 130' INT TERM

# ハブごとにバックグラウンドで接続ループを起動
for _i in "${!HUBS[@]}"; do
  _watch_hub "${HUBS[$_i]}" "hub$((_i+1))" &
done

# 全バックグラウンドプロセスが終了するまで待機（通常は終了しない）
wait
