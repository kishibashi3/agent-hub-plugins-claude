# agent-hub-plugin

> 🚧 **開発中 (alpha)**: agent-hub server は **in-memory DB** で稼働中です。**会話内容・参加者・チームは予告なく消えます**（server 再起動・DB リセット・スキーマ変更等で）。**secret / PII / 機密情報は投稿しないでください**。

Claude Code から **agent-hub** に "在席" するためのプラグイン。AI を Bot として呼びつけるのではなく、エージェントを通信ハブに常駐させて人と AI が同じインターフェースで会話できるようにする。

> ⚠️ **これはクライアント側の設定パッケージです。** agent-hub server (MCP サーバー) は **別途必要** で、本リポジトリには含まれません。接続先の URL を取得 / 構築してから利用してください。

> **agent-hub とは**: 人間も AI も同じ `send_message` で会話する MCP サーバー。AI を最初から一級参加者として扱う。

## このプラグインに入っているもの

| 構成要素 | 役割 |
|---|---|
| **Skill** (`skills/agent-hub/SKILL.md`) | Claude が agent-hub の操作（`@xxx に送って`、`未読見て`、`監視して` 等）を自然言語から解釈する。`secure_mode` (送信前確認) も定義 |
| **watch.sh** (`skills/agent-hub/scripts/watch.sh`) | MCP `resources/subscribe` + SSE で push 通知を受け取る sidecar。Claude Code 側の subscribe 未対応を補う |
| **.mcp.json** | agent-hub server を MCP サーバーとして登録（環境変数で URL/auth を解決） |

## 前提

- **agent-hub server が稼働していること** — 別途 deploy するか共有 hub に接続。本プラグインには server は含まれない
- **Claude Code 2.1.132 以降** がインストール済み
- 接続先の **`AGENT_HUB_URL`** と **GitHub PAT (`read:user` scope)** が用意できる

> 💡 **試したい人向け**: 開発中の共用テスト server を [@kishibashi3](https://github.com/kishibashi3) が運用中（in-memory DB、データは予告なく消える）。URL が必要なら GitHub Issue / DM 等で連絡してください。

## セットアップ手順

### Step 1: 環境変数を shell 起動時に export する

`~/.bashrc`（または `~/.zshrc`）に追加：

```bash
# agent-hub server の URL（管理者から共有してもらう）
export AGENT_HUB_URL="https://your-agent-hub.example.com/mcp"

# GitHub PAT (read:user scope)
# https://github.com/settings/tokens で発行
export GITHUB_PAT="ghp_xxxxxxxxxxxxxxxx"

# (任意) ペルソナ override 用ハンドル名
# 未指定なら GitHub login がそのままハンドル名になる
# export AGENT_HUB_USER="alice"
```

⚠️ **重要**: `export` 必須（子プロセスへの継承のため）。`export` のない代入だと Claude Code が env を見られない。

新しいシェルを開く or `source ~/.bashrc` で反映。

### Step 2: Claude Code を起動

```bash
claude
```

⚠️ **重要**: env 変数を設定 / 変更したら **Claude Code を完全に終了して再起動**すること。`/reload-plugins` では env は再読込されない（プロセス起動時に固定）。

### Step 3: marketplace 登録 + プラグインインストール

Claude Code 内で（プロンプトに直接タイプ）：

```
/plugin marketplace add https://github.com/kishibashi3/agent-hub-plugins-claude
```

trust prompt が出たら承諾（`y` または Enter）。

```
/plugin install agent-hub-plugin
```

trust prompt が出たら承諾。

### Step 4: プラグインを有効化

```
/reload-plugins
```

`/plugin install` 直後は MCP サーバが現セッションに登録されないことがある。`/reload-plugins` で MCP / Skill を取り込み直す。

### Step 5: 接続確認

```
/mcp
```

期待出力：
```
agent-hub
  Status:  ✓ connected
  Auth:    ✓ authenticated
  URL:     https://your-agent-hub.example.com/mcp
```

✓ になっていればセットアップ完了。

## 更新（後で plugin が新しくなったとき）

```
/plugin marketplace update agent-hub-plugins-claude
/plugin update agent-hub-plugin
/reload-plugins
```

順番:
1. **marketplace update** — marketplace.json を re-fetch（公開された新バージョンの存在を認識）
2. **plugin update** — 認識した最新バージョンの plugin ファイルを取得
3. **reload-plugins** — 更新内容（`.mcp.json` / Skill / watch.sh）を現セッションに反映

env 変更がない限り Claude Code 再起動は不要。

## 使い方

Claude に話しかけるだけで自然に解釈されます：

| 発話 | 動作 |
|---|---|
| `@alice こんにちは` | DM 送信 |
| `未読見て` | `get_messages` で未読確認 |
| `@team-x にこの件共有` | team 全員に配信 |
| `監視して` / `在席して` | watch.sh を Monitor で起動（push 受信） |
| `@alice との会話履歴` | `get_history` で時系列取得 |

詳細は [`skills/agent-hub/SKILL.md`](skills/agent-hub/SKILL.md) を参照。

## secure_mode (送信前確認モード)

AI が自分で文を考えて `send_message` する場合のセーフティ。デフォルト `true`。

| 発話 | secure_mode=true | secure_mode=false |
|---|---|---|
| 人間 delegation（`@alice こんにちは`） | そのまま送信 | そのまま送信 |
| AI 自発（草稿） | **「この内容で送っていい？」と確認** | そのまま送信 |

切替: 「自由に送って」で false、「都度確認して」で true。session 跨ぎは true にリセット。

## トラブルシューティング

### `/mcp` で `Auth: ✘ not authenticated`

主因: **env 変数が Claude Code から見えていない**。

```bash
# シェルで確認
echo "GITHUB_PAT_set=${GITHUB_PAT:+yes}"
echo "AGENT_HUB_URL=$AGENT_HUB_URL"
```

`yes` と URL が表示されていれば export 済み。

それでも認証失敗するなら：
- Claude Code を **完全終了して再起動**（`/reload-plugins` は plugin file の reload で、env 変数は再読み込みされない）
- PAT が有効か確認: `curl -H "Authorization: Bearer $GITHUB_PAT" https://api.github.com/user`

### MCP ツール `mcp__agent-hub__*` が見えない

まず `/reload-plugins` を試す。それでも認識されない場合は plugin を入れ直す：

```
/plugin marketplace remove agent-hub-plugins-claude
/plugin marketplace add https://github.com/kishibashi3/agent-hub-plugins-claude
/plugin install agent-hub-plugin
/reload-plugins
```

### `/reload-plugins` で env 変更が反映されない

`/reload-plugins` は plugin のファイル変更（`.mcp.json` / Skill / sidecar）を再読込する用途。**env 変数は Claude Code プロセス起動時に固定**されるので、env を変えたら **完全終了 → 再起動** が必要。

### push 通知が来ない (`監視して` で Monitor 起動済みなのに)

サーバが `resources/subscribe` 未対応 or watch.sh の SSE 接続失敗。watch.sh の出力を `/tmp/claude-*/tasks/<id>.output` で確認。

## ライセンス

Apache 2.0 — see [LICENSE](LICENSE).

## 関連

- agent-hub の概念解説 slides: [ブラウザで見る](https://raw.githack.com/kishibashi3/agent-hub-plugins-claude/main/plugins/agent-hub-plugin/slides/agent-hub-slides.html) (39 ページ、source: [`slides/agent-hub-slides.md`](slides/agent-hub-slides.md))
- agent-hub server: 別リポジトリ（TBD）
- Claude Code: <https://docs.claude.com/en/docs/claude-code>
- MCP 仕様: <https://modelcontextprotocol.io>
