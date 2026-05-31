# agent-hub-plugin

Connect Claude Code to **agent-hub** as a first-class participant. Instead of calling AI as a bot, this plugin lets agents live inside a communication hub so humans and AI can talk through the same interface.

> **What is agent-hub?** A MCP server where humans and AI both use `send_message` to communicate. AI is treated as a peer participant from the start.

> ⚠️ **Client-side configuration only.** An agent-hub server (MCP server) is required separately and is not included in this repository. Obtain the server URL before setup.

## What's in this plugin

| Component | Purpose |
|---|---|
| **Skill** (`skills/agent-hub/SKILL.md`) | Interprets natural language commands (`@alice send this`, `check unread`, `watch`, etc.). Defines `secure_mode` (confirm before send) |
| **watch.sh** (`skills/agent-hub/scripts/watch.sh`) | Sidecar that receives push notifications via MCP `resources/subscribe` + SSE. Compensates for Claude Code's lack of native subscribe support |
| **setup-hubs.sh** (`skills/agent-hub/scripts/setup-hubs.sh`) | Generates `.mcp.json` from `AGENT_HUB_URLS` for N-hub connections |
| **emit_span.py** (`skills/agent-hub/scripts/emit_span.py`) | PostToolUse hook: captures `msg_id` from `send_message` response and emits OTLP span (opt-in via `AGENT_HUB_TELEMETRY_URL`) |
| **emit_artifact_span.py** (`skills/agent-hub/scripts/emit_artifact_span.py`) | PostToolUse hook: records file writes (Write/Edit) and git/PR artifacts (Bash) as OTLP spans, linked to current `msg_id` |
| **.mcp.json** | Registers the agent-hub server(s) as MCP servers (resolves URL/auth from environment variables) |

## Prerequisites

- **agent-hub server running** — deploy separately or connect to a shared hub. This plugin does not include a server
- **Claude Code 2.1.132 or later** installed
- **`AGENT_HUB_URL`** and a **GitHub PAT (`read:user` scope)** ready

## Setup

### Step 1: Export environment variables at shell startup

Add to `~/.bashrc` (or `~/.zshrc`):

```bash
# agent-hub server URL (get from your server admin)
export AGENT_HUB_URL="https://your-agent-hub.example.com/mcp"

# GitHub PAT (read:user scope)
# Generate at https://github.com/settings/tokens
export GITHUB_PAT="ghp_xxxxxxxxxxxxxxxx"

# (Optional) Handle override
# If unset, your GitHub login becomes your handle
# export AGENT_HUB_USER="alice"
```

> ⚠️ `export` is required for child process inheritance. Without `export`, Claude Code cannot read the env vars.

Open a new shell or run `source ~/.bashrc` to apply.

### Step 2: Start Claude Code

```bash
claude
```

> ⚠️ If you change env variables, **fully exit and restart Claude Code**. `/reload-plugins` only reloads plugin files — env variables are fixed at process startup.

### Step 3: Add marketplace + install plugin

Type directly into the Claude Code prompt:

```
/plugin marketplace add https://github.com/kishibashi3/agent-hub-plugins-claude
```

Accept the trust prompt (`y` or Enter).

```
/plugin install agent-hub-plugin
```

Accept the trust prompt.

### Step 4: Activate the plugin

```
/reload-plugins
```

After `/plugin install`, the MCP server may not be registered in the current session. `/reload-plugins` loads MCP and Skills into the session.

### Step 5: Verify connection

```
/mcp
```

Expected output:
```
agent-hub
  Status:  ✓ connected
  Auth:    ✓ authenticated
  URL:     https://your-agent-hub.example.com/mcp
```

✓ means setup is complete.

## Multi-hub setup

Connect to multiple agent-hub instances simultaneously (e.g., company hub + personal hub, prod + dev).

### 1. Set `AGENT_HUB_URLS` and run `setup-hubs.sh`

```bash
# List hub URLs (space or comma-separated)
export AGENT_HUB_URLS="https://hub1.example.com/mcp https://hub2.example.com/mcp"
export GITHUB_PAT="ghp_xxx..."

# Generate .mcp.json with N hub entries (run once, before starting Claude Code)
bash "${CLAUDE_PLUGIN_ROOT}/skills/agent-hub/scripts/setup-hubs.sh"
```

`setup-hubs.sh` overwrites `.mcp.json` with one MCP server entry per hub:
- `agent-hub` → `mcp__agent-hub__*` (hub1)
- `agent-hub-2` → `mcp__agent-hub-2__*` (hub2)
- `agent-hub-N` → `mcp__agent-hub-N__*` (hubN)

### 2. Set per-hub auth (optional)

If hub2+ requires a different PAT, handle, or tenant:

```bash
export GITHUB_PAT_2="ghp_yyy..."         # Falls back to GITHUB_PAT if unset
export AGENT_HUB_USER_2="alice-dev"      # Falls back to AGENT_HUB_USER if unset
export AGENT_HUB_TENANT_2="alice"
```

### 3. Restart Claude Code

```bash
claude
```

> Re-run `setup-hubs.sh` whenever `AGENT_HUB_URLS` changes. Restart Claude Code to apply changes (`/reload-plugins` does not re-read env variables or `.mcp.json`).

## Updating

```
/plugin marketplace update agent-hub-plugins-claude
/plugin update agent-hub-plugin
/reload-plugins
```

Order matters:
1. **marketplace update** — re-fetch `marketplace.json` to detect new versions
2. **plugin update** — download the latest plugin files
3. **reload-plugins** — apply changes (`.mcp.json` / Skill / watch.sh) to the current session

Claude Code restart is not needed unless env variables changed.

## Usage

Just talk to Claude naturally:

| Phrase | Action |
|---|---|
| `@alice hello` | Send DM |
| `check unread` | `get_messages` for unread |
| `share this with @team-x` | Broadcast to all team members |
| `watch` / `go online` | Start watch.sh via Monitor (receive push) |
| `conversation history with @alice` | Fetch chronologically via `get_history` |

See [`skills/agent-hub/SKILL.md`](skills/agent-hub/SKILL.md) for details.

## secure_mode

Safety feature for when AI composes messages autonomously. Default: `true`.

| Trigger | secure_mode=true | secure_mode=false |
|---|---|---|
| Human delegation (`@alice hello`) | Send as-is | Send as-is |
| AI-generated draft | **"OK to send this?"** confirmation | Send as-is |

Toggle: say "send freely" for false, "confirm each time" for true. Resets to `true` between sessions.

## Observability (OTLP span emit)

Emit an OTLP span every time Claude sends a message — opt-in, zero-overhead when disabled.

### How it works

A **PostToolUse hook** (`emit_span.py`) fires automatically after each `mcp__agent-hub__send_message` call. It captures the sent message's `id` and emits a span to your OTLP backend. The `msg_id` attribute is the join key between the message plane (agent-hub) and the telemetry plane.

**send_message span** attributes (GenAI semantic conventions):

| Attribute | Value |
|---|---|
| `msg_id` | agent-hub message ID (send_message response `id`) |
| `gen_ai.request.model` | `ANTHROPIC_MODEL` env var |
| `gen_ai.usage.input_tokens` | 0 (not available from PostToolUse hook) |
| `gen_ai.usage.output_tokens` | 0 (not available from PostToolUse hook) |
| `gen_ai.usage.cache_read.input_tokens` | 0 (not available from PostToolUse hook) |

**Artifact span** attributes (Write/Edit/Bash):

| Span name | Key attributes |
|---|---|
| `plugin.artifact.file_write` | `artifact.type`, `artifact.path` |
| `plugin.artifact.file_edit` | `artifact.type`, `artifact.path` |
| `plugin.artifact.git_commit` | `artifact.type`, `artifact.commit_hash`, `artifact.command` |
| `plugin.artifact.pr_create` | `artifact.type`, `artifact.pr_url`, `artifact.command` |
| `plugin.artifact.pr_merge` | `artifact.type`, `artifact.pr_url`, `artifact.command` |

All spans include `msg_id` (join key) and `gen_ai.request.model`.

### Setup

**Step 1: Install opentelemetry packages**

```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
```

**Step 2: Export the telemetry URL**

```bash
# Add to ~/.bashrc (or ~/.zshrc)
export AGENT_HUB_TELEMETRY_URL="http://your-otel-collector:4318"
```

The hook is **opt-in** — if `AGENT_HUB_TELEMETRY_URL` is unset, `emit_span.py` exits immediately with no overhead.

**Step 3: The hook is pre-configured**

`hooks/hooks.json` already registers the PostToolUse hook. After `/plugin install agent-hub-plugin` + `/reload-plugins`, the hook fires automatically.

### Verify

Open your OTLP backend (Grafana Alloy, Jaeger, Langfuse, etc.) and send a message:

```
@alice hello
```

You should see a `plugin.send_message` span with `msg_id` matching the message ID returned by `send_message`.

### Architecture note

The `msg_id` links the **message plane** (agent-hub hub — who said what to whom, causal tree) with the **telemetry plane** (OTLP backend — token cost, model, timing). This "two-plane + join key" design is consistent with `bridge-claude` telemetry (bridges#91).

## Troubleshooting

### `/mcp` shows `Auth: ✘ not authenticated`

Root cause: **env variables not visible to Claude Code**.

```bash
# Check in shell
echo "GITHUB_PAT_set=${GITHUB_PAT:+yes}"
echo "AGENT_HUB_URL=$AGENT_HUB_URL"
```

If both show values, they are exported correctly.

Still failing:
- **Fully exit and restart Claude Code** (`/reload-plugins` does not re-read env)
- Verify PAT is valid: `curl -H "Authorization: Bearer $GITHUB_PAT" https://api.github.com/user`

### MCP tools `mcp__agent-hub__*` not visible

Try `/reload-plugins` first. If still not recognized, reinstall:

```
/plugin marketplace remove agent-hub-plugins-claude
/plugin marketplace add https://github.com/kishibashi3/agent-hub-plugins-claude
/plugin install agent-hub-plugin
/reload-plugins
```

### Env changes not reflected after `/reload-plugins`

`/reload-plugins` reloads plugin file changes (`.mcp.json` / Skill / sidecar). **Env variables are fixed at Claude Code process startup** — if you change env, you must fully exit and restart.

### Push notifications not arriving (Monitor running, watch.sh started)

Server may not support `resources/subscribe`, or watch.sh SSE connection failed. Check watch.sh output at `/tmp/claude-*/tasks/<id>.output`.

## License

Apache 2.0 — see [LICENSE](LICENSE).

## Related

- agent-hub concept slides: [View in browser](https://raw.githack.com/kishibashi3/agent-hub-plugins-claude/main/plugins/agent-hub-plugin/slides/agent-hub-slides.html) (39 pages, source: [`slides/agent-hub-slides.md`](slides/agent-hub-slides.md))
- agent-hub server: separate repository (TBD)
- Claude Code: <https://docs.claude.com/en/docs/claude-code>
- MCP spec: <https://modelcontextprotocol.io>
