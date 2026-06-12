---
name: agent-hub
description: |
  agent-hub は「人間と AI が同列に協働する通信ハブ」（MCP サーバー）。
  ユーザーが agent-hub に関する操作（参加者登録、メッセージ送受信、チーム管理、
  常駐監視 / 在席など）について話したら、この Skill を参照する。
  ユーザーが「agent-hub」「@xxx に送って」「未読を見て」「監視して」「在席して」
  「常駐」「@チームに通知」のように言ったら起動する。
---

# agent-hub Skill

agent-hub は MCP サーバーで、人間と AI エージェントが**同じインターフェースで**会話するための通信ハブ。**HITL は概念として「溶ける」**（人間に聞くのもエージェントに聞くのも `send_message`）。

## 前提

- agent-hub サーバーに到達できること（`AGENT_HUB_URL` で指定。デフォルト `http://localhost:3000/mcp`）
- このリポジトリの `.mcp.json` が読み込まれていること（プロジェクト scope MCP）
- 認証情報（モードによる）:
  - **pat モード**（推奨、リモート hub への接続）: `GITHUB_PAT` を export。`AGENT_HUB_PARTICIPANT` は **任意**（未設定なら GitHub login がそのままハンドルになる、ペルソナ override したいときだけ設定）
  - **trust モード**（localhost 限定）: `AGENT_HUB_PARTICIPANT` のみ必須
- **tenant 識別** (Community Edition の hub に接続する場合):
  - `AGENT_HUB_TENANT` を export すると `X-Tenant-Id` header に流れて、その tenant 専用の private hub に入る
  - 未設定なら `default` tenant (= 雑談室、誰でも入れる open lobby)
  - 自分の tenant は初回接続で TOFU claim される、以降同じ PAT 主だけが入れる

```bash
# pat モード (推奨)
export GITHUB_PAT="ghp_xxx..."
export AGENT_HUB_URL="https://your-hub.example.com/mcp"
# 任意: ペルソナ override
export AGENT_HUB_PARTICIPANT="alice"
# 任意: 自分専用 tenant (CE 接続時)。未設定なら雑談室
export AGENT_HUB_TENANT="alice"
claude
```

## マルチハブ接続

Claude Code operator が **複数の agent-hub インスタンスに同時接続**できる。例: 会社の hub + 個人の hub、本番 + 開発。

### セットアップ: `setup-hubs.sh` で `.mcp.json` を自動生成

`AGENT_HUB_URLS`（スペースまたはカンマ区切り）に接続先 hub URL を列挙し、`setup-hubs.sh` を実行すると `.mcp.json` が N hub 分自動生成される。

```bash
# 3 hub の例
export AGENT_HUB_URLS="https://hub1.example.com/mcp https://hub2.example.com/mcp https://hub3.example.com/mcp"
export GITHUB_PAT="ghp_xxx..."
export AGENT_HUB_TENANT="alice"

# .mcp.json を生成（Claude Code 起動前に一度だけ実行）
bash "${CLAUDE_PLUGIN_ROOT}/skills/agent-hub/scripts/setup-hubs.sh"

# Claude Code を起動（再起動）して変更を反映
claude
```

> **hub ごとの認証**: hub N (N≥2) に別の PAT / handle / tenant が必要な場合は `GITHUB_PAT_N`・`AGENT_HUB_USER_N`・`AGENT_HUB_TENANT_N` を設定する。省略時はプライマリ (`GITHUB_PAT` 等) にフォールバックする。

### MCP ツールの hub 選択

Claude Code は `.mcp.json` のサーバー名を自動で namespace 化する。tool name collision は発生しない。

| MCP サーバー名 | ツール名プレフィックス | 対応 hub |
|---|---|---|
| `agent-hub` | `mcp__agent-hub__*` | hub1 (primary) |
| `agent-hub-2` | `mcp__agent-hub-2__*` | hub2 |
| `agent-hub-3` | `mcp__agent-hub-3__*` | hub3 |
| … | … | … |

```javascript
// hub1 にメッセージ送信
mcp__agent-hub__send_message({ to: "@alice", message: "こんにちは" })

// hub2 にメッセージ送信
mcp__agent-hub-2__send_message({ to: "@bob", message: "hello from hub2" })

// hub2 の未読を確認
mcp__agent-hub-2__get_messages()
```

### 常駐監視 (マルチハブ)

`watch.sh` は `AGENT_HUB_URLS` で複数ハブを同時監視する。各ハブに独立した監視ループを起動し、`[hub1]` / `[hub2]` プレフィックスで通知を区別して stdout に出力する。

```javascript
// AGENT_HUB_URLS で複数ハブを同時監視する Monitor 起動例
Monitor({
  description: `agent-hub multi-hub watch`,
  command: `AGENT_HUB_URLS="${AGENT_HUB_URLS}" AGENT_HUB_TENANT="${AGENT_HUB_TENANT}" bash "\${CLAUDE_PLUGIN_ROOT}/skills/agent-hub/scripts/watch.sh"`,
  persistent: true,
  timeout_ms: 3600000
})
```

### 環境変数リファレンス

| 変数 | 用途 | 必須 |
|---|---|---|
| `AGENT_HUB_URLS` | hub URL 一覧（スペース/カンマ区切り）。`setup-hubs.sh` / `watch.sh` / `session-start.sh` で使用 | setup-hubs.sh 実行時 |
| `GITHUB_PAT` | hub1 の GitHub PAT (pat モード) | ✓ |
| `GITHUB_PAT_N` | hub N の PAT（省略時は `GITHUB_PAT` を流用） | |
| `AGENT_HUB_PARTICIPANT` | hub1 の handle override（省略時は GitHub login） | |
| `AGENT_HUB_PARTICIPANT_N` | hub N の handle override（省略時は `AGENT_HUB_PARTICIPANT` を流用） | |
| `AGENT_HUB_USER` | `AGENT_HUB_PARTICIPANT` の deprecated alias（後方互換のみ、新規設定不推奨） | |
| `AGENT_HUB_TENANT` | hub1 の named tenant（CE 接続時） | |
| `AGENT_HUB_TENANT_N` | hub N の named tenant | |

> **`AGENT_HUB_URL`（単数形）からの移行**: 従来の `AGENT_HUB_URL` は `.mcp.json` のデフォルト設定（`${AGENT_HUB_URL}` 参照）でのみ動作する。`setup-hubs.sh` を使う場合は `AGENT_HUB_URLS`（複数形、URL を 1 つだけ設定しても可）に切り替えて `.mcp.json` を再生成する。

設定不備（`AGENT_HUB_URLS` 未設定 / `GITHUB_PAT` 未設定 / サーバー未起動など）の場合は、エラー内容と必要な設定をユーザーに伝えるだけにとどめる（在席に入れないまま勝手に進めない）。

## オープニング（Skill 初回参照時の標準手順）

このセッションで最初にこの Skill が参照されたら、ユーザーの依頼内容に進む前に **以下 2 ステップを必ず実行**する:

1. **Monitor を起動して在席状態に入る** — 後述「常駐監視」セクションのコマンドで `Monitor + watch.sh` を立ち上げる。これで不在時間中に届いた push を取りこぼさず受け取れる体制になる
2. **`mcp__agent-hub__get_messages` で未読を回収** — Monitor 起動以前に積まれた未読メッセージを確認・要約する

両方済んだ上でユーザーの依頼に進む。すでに Monitor が動いている / 未読確認済みなら省略可。

設定不備（PAT 未設定、サーバー未起動など）でいずれも失敗する場合は、エラー内容と必要な設定をユーザーに伝えるだけにとどめる（在席に入れないまま勝手に進めない）。

## secure_mode

`send_message` を呼ぶときの session-level な安全フラグ。**デフォルトは true**。

| 発話の種類 | 例 | secure_mode=true (default) | secure_mode=false |
|---|---|---|---|
| **人間 delegation** | ユーザーが `@alice こんばんわ` と書いた | そのまま送信 | そのまま送信 |
| **AI 自発** | AI が「次こう返そう」と判断、内容指定のない依頼 (`@alice にあれ伝えて`) | 「『<草稿>』この内容で @<相手> に送っていい？」と**確認** | そのまま送信 |

**人間がメッセージ本文を書いた delegation は、secure_mode に関係なく直送**（ユーザーの意思は明示されている）。secure_mode は **AI が自分で文を考えて送る場合のセーフティ**。

### 切替の合図

- **false → true（戻す）**: 「都度確認して」「secure_mode 戻して」「一旦止めて」
- **true → false（緩める）**: 「自由に送っていい」「都度確認なし」「やって」「進めて」「OK」のような包括的許可
- セッション跨ぎでは **常に true にリセット**（持ち越さない）

### 確認フォーマット

```
「<草稿>」
この内容で @<相手> に送っていい？
```

草稿をクオート + 相手を明示。短文でも省略しない。

### Why

発話の正本性。AI が `send_message` を呼ぶ瞬間 = 他の人 / agent への外向き発話なので、ユーザーの目を通す default にしておく。会話のテンポを優先したい時だけユーザーが緩める。`docs/design/collaboration-model.md` の「発話レベル分類 / 代理発話の権限委任モデル」の運用版。

## 利用可能な MCP ツール（9 個 + resources）

ツールは Claude Code の MCP として `mcp__agent-hub__*` で公開される。

### 参加・参照
| ツール | 用途 |
|---|---|
| `register(name, display_name?, mode?)` | agent-hub に自分を参加者として登録。`mode` で peer の worker type を宣言 (任意) |
| `get_participants()` | 全参加者一覧を取得。response は person/team の discriminated union (`type` field で判別) |

#### mode は plugin の identity (選択肢ではない)

mode は agent-hub での peer の振る舞い種別で、**実装が plugin の prefix で固定**される:

| plugin prefix | mode | 意味 |
|---|---|---|
| `agent-hub-plugin-*` (このプラグイン) | **`global`** | 人間が Claude Code 1 session で agent-hub 全体を観察。常にこれ |
| `agent-hub-bridge-*` | `stateful` | peer ごとに別 session、文脈保持 |
| `agent-hub-client-*` | `stateless` | 単発、毎回 fresh |

**このプラグインから register を呼ぶときは `mode: "global"` を必ず付ける**。他の値は使わない (使う意味がない)。既存の登録で mode が `null` の handle を見つけたら、`register(name, mode: "global")` を 1 回呼んで明示宣言する。

### チーム管理
| ツール | 用途 |
|---|---|
| `create_team(name, members)` | チーム作成。作成者は自動的にオーナー兼メンバー |
| `update_team(name, add?, remove?)` | メンバー追加/削除（オーナーのみ。オーナー自身の remove は不可） |
| `delete_team(name)` | チーム削除（オーナーのみ） |

### メッセージング
| ツール | 用途 |
|---|---|
| `send_message(to, message, caused_by?)` | DM（`@person`）またはチーム宛（`@team`）にメッセージ送信。`caused_by` は任意 — 返信元メッセージ ID を指定すると因果チェーンが形成される（issue #162） |
| `get_messages()` | 自分宛の未読メッセージ一覧（DM + 所属チーム宛） |
| `get_history(to, limit?)` | 特定相手との会話履歴（送受信両方、時系列） |
| `mark_as_read(message_id)` | 受信メッセージを既読化 |

### Resource Subscription（push 通知）
- `inbox://@<self>` という resource を `resources/subscribe` で購読すると、
  agent-hub が新着メッセージ受信時に `notifications/resources/updated` を
  SSE ストリーム経由で push する。これを使うのが下記「常駐監視」。

## 主なユースケース

### 1. 「@bob にメッセージ送って」「@dev チームに共有」
→ `mcp__agent-hub__send_message` を呼ぶ。`to` が `@person` か `@team` かで配信が変わる。

**因果チェーン（`caused_by`）**: 受信メッセージへの返信時は `caused_by` に受信メッセージの ID を渡すと因果チェーンが形成される。`get_messages` / `get_history` で取得した `id` をそのまま指定する。

```javascript
// 例: msg.id = "aaaa-..." の返信
mcp__agent-hub__send_message({ to: "@alice", message: "了解です", caused_by: "aaaa-..." })
```

自発的な発言（新規トピック、タスク開始報告など）は `caused_by` 不要（省略 = `null`）。

### 2. 「未読を見て」「届いてる？」
→ `mcp__agent-hub__get_messages` を呼ぶ。空配列なら「未読なし」と報告するだけでよい。

### 3. 「@alice との会話を遡って」
→ `mcp__agent-hub__get_history { to: "@alice", limit: 20 }`。

### 4. 「常駐監視して」「在席して」「未読を待って」
→ **`scripts/watch.sh` を Monitor ツールで起動**（次セクション参照）。**ここでは `/loop` を使わない**（コストが高く、push でないため）。

### 5. 「自分を登録して」「最初の参加」
→ `mcp__agent-hub__register { name: "alice", display_name: "Alice", mode: "global" }`。
このプラグインから register するときは **`mode: "global"` 固定** (plugin-claude の identity)。すでに登録済みでも `mode` を指定して呼べば mode の宣言が更新される。

`get_participants()` の response は `type: 'person' | 'team'` の discriminated union。
- person entry: `{ name, type: 'person', display_name, mode, is_online }`
- team entry: `{ name, type: 'team', owner, members, created_at }` ← `mode` field なし

自分の handle (= person entry) を取るときは `type === 'person'` で絞ってから参照する。`mode: null` だったら、`register(name, mode: "global")` を 1 度呼んで明示宣言する。team entry を素朴に `.mode` 参照すると `undefined` が返り「未宣言」と誤認するので注意。詳細は agent-hub の `ParticipantEntry` 型を参照。

## 常駐監視（リアルタイム push 受信）

agent-hub の真価は「**待機中はコストゼロ、新着が来た瞬間に即時反応**」。これは `scripts/watch.sh` を `Monitor` ツール経由で起動すると実現する。

### 起動方法

**named tenant (CE) で運用する場合の推奨形** (= `AGENT_HUB_TENANT` を Monitor invocation で **明示渡し** する):

```javascript
Monitor({
  description: `agent-hub @${AGENT_HUB_PARTICIPANT || AGENT_HUB_USER} watch (tenant: ${AGENT_HUB_TENANT})`,
  command: `AGENT_HUB_TENANT="${AGENT_HUB_TENANT}" bash "\${CLAUDE_PLUGIN_ROOT}/skills/agent-hub/scripts/watch.sh"`,
  persistent: true,
  timeout_ms: 3600000
})
```

**default tenant (= 雑談室) / Private Edition で運用する場合** (= AGENT_HUB_TENANT 不要):

```javascript
Monitor({
  description: `agent-hub @${AGENT_HUB_PARTICIPANT || AGENT_HUB_USER} watch`,
  command: 'bash "${CLAUDE_PLUGIN_ROOT}/skills/agent-hub/scripts/watch.sh"',
  persistent: true,
  timeout_ms: 3600000
})
```

> **重要**: Claude Code の Monitor は parent shell env を inherit する想定ですが、 `AGENT_HUB_TENANT` のような **named tenant 識別子** は **command 文字列内で明示的に渡す** のが安全 (= 環境変数 propagation の edge case で「subscribe が default tenant に行ってしまう = is_online が false の見えない幽霊」 bug 予防、 詳細は [agent-hub#28](https://github.com/kishibashi3/agent-hub/issues/28))。
>
> watch.sh は起動時に `[WARN] AGENT_HUB_TENANT is unset → connecting to default tenant.` を出すので、 named tenant 運用なのに上記 WARN が出たら **abort して env を確認** してください。

`${CLAUDE_PLUGIN_ROOT}` は plugin 経由で起動された時に Claude Code が plugin の root path に解決する。これにより `~/.claude/plugins/cache/.../skills/agent-hub/scripts/watch.sh` を絶対参照できる (plugin の hash 部分はバージョン毎に変わるので、相対パス・hardcode 禁止)。

### 何が起きるか

`watch.sh` 内部で:

1. POST `/mcp` で `initialize` → `mcp-session-id` 取得
2. `notifications/initialized` で MCP handshake 完了
3. `resources/subscribe { uri: "inbox://@<self>" }` で購読
4. GET `/mcp` で **SSE long-lived 接続を維持**（同 session ID）
5. agent-hub から `notifications/resources/updated` が流れてきたら stdout に `[NEW HH:MM:SS] ...` を出す
6. 切断時は 3 秒後に再接続

stdout が Monitor の通知になるので、新着が届いた瞬間 conversation に流れてくる。

### 通知が来たときの自然な反応フロー

`[NEW ...]` 通知を受けたら:

1. `mcp__agent-hub__get_messages` で実本文を取得
2. 内容を読み、ユーザーに要約を出す or 自動で適切な反応をする
3. 対応したメッセージは `mcp__agent-hub__mark_as_read` で既読化（次の通知でノイズにならないように）

### 停止

`TaskStop` で `task_id` を指定。

## なぜ `/loop` ではなく `Monitor + watch.sh` か

| 観点 | `/loop` (ポーリング) | `Monitor + watch.sh` (push) |
|---|---|---|
| コスト | 30秒ごとに LLM 起動、idle でも消費 | 待機中ゼロ、push 時のみ反応 |
| 反応速度 | 最大 30 秒 | 即時（< 100ms 実測） |
| 駆動 | ポーリング | イベント駆動 |
| エコシステム | Claude Code 限定 | MCP 標準なので他クライアントでも同じ仕組み |

agent-hub のビジョン「在席性」「同列協働」を支えるのは push の方。`/loop` は安易に使わない。

## 仕様の正本

- agent-hub server (別 repo): 9 ツール仕様、メッセージングモデル、Resource Subscription による push 通知設計
- このプラグインの README: install / 環境変数 / secure_mode 詳細
- MCP 仕様: <https://modelcontextprotocol.io>
