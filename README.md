# SiliconSandbox

Apple Silicon native AI agent sandbox and orchestration platform. Runs entirely on macOS using native primitives — no Docker, no cloud dependency.

## What It Does

Provides isolated execution environments for AI agents to run code safely, plus an orchestrator to break complex tasks into parallel subtasks routed across local and cloud models.

## Architecture

```
Web UI (:8095) ─── Orchestrator (:8094) ─── Sandbox Engine (:8093)
                         │                        │
                    Model Router              Three Tiers:
                    ├─ Qwen3.5 (:8080)        ├─ A: Seatbelt (sandbox-exec, ~0 RAM)
                    ├─ Phi-4 (:8081)          ├─ B: MicroVM (Virt.framework, ~256MB)
                    └─ Claude API             └─ C: Native (subprocess + rlimit)
                                                   │
MCP Server (:8100) ──────────────────────────── Engine API
  11 tools for Claude Code
```

## Services

| Port | Service | LaunchAgent |
|------|---------|-------------|
| 8093 | Sandbox Engine | `com.siliconsandbox.engine` |
| 8094 | Orchestrator | `com.siliconsandbox.orchestrator` |
| 8095 | Web UI | `com.siliconsandbox.ui` |
| 8098 | Network Allowlist Proxy | (embedded in engine) |
| 8100 | MCP Tool Server | `com.siliconsandbox.mcp` |

All ports bind to 127.0.0.1 only.

## Quick Start

```bash
# Install dependencies, build vm-launcher, set up LaunchAgents
./scripts/install.sh

# Start all services
./scripts/launch.sh

# Verify
curl -s http://127.0.0.1:8093/health | python3 -m json.tool

# Stop all services
./scripts/stop.sh
```

## Usage

### Direct API

```bash
# Run a command in a Seatbelt sandbox
curl -X POST http://127.0.0.1:8093/sandbox \
  -H 'Content-Type: application/json' \
  -d '{"command": "python3 -c \"print(2+2)\"", "tier": "A", "timeout": 10}'

# Create a persistent session
curl -X POST http://127.0.0.1:8093/session \
  -H 'Content-Type: application/json' \
  -d '{"tier": "A", "ttl_seconds": 3600}'
```

### Python SDK

```python
from silicon_sandbox import Sandbox, Session

# One-shot execution
result = Sandbox.run("echo hello", tier="A")
print(result.stdout)

# Persistent session
with Session.create() as session:
    session.write_files({"main.py": "print('hello')"})
    result = session.exec("python3 main.py")
    print(result.stdout)
```

### MCP Tools (Claude Code)

Registered in `~/.mcp.json` as `silicon-sandbox`. Available tools:

- `sandbox_run` — one-shot sandboxed execution
- `sandbox_health` — engine health check
- `session_create` / `session_exec` / `session_destroy` — persistent sessions
- `session_write_files` / `session_read_file` — file operations
- `session_pause` / `session_resume` — SIGSTOP/SIGCONT
- `session_list` — list active sessions

## Sandbox Tiers

| Tier | Technology | RAM | Boot | Use Case |
|------|-----------|-----|------|----------|
| A (Seatbelt) | `sandbox-exec` + SBPL | ~0 MB | ~50ms | Default for code execution |
| B (MicroVM) | Virtualization.framework + Alpine | ~256 MB | ~260ms | Linux envs, untrusted binaries |
| C (Native) | `subprocess` + rlimit + setpgrp | ~0 MB | ~10ms | Trusted internal tools |

### Security

- **Deny-default** Seatbelt profiles (v2): `(deny default)` base with selective allows
- **Blocked paths**: `~/.ssh`, `~/.gnupg`, `~/Library/Keychains`, `~/.openclaw/`, `~/.claude/`
- **Process isolation**: `os.setpgrp()` + `resource.setrlimit()` per sandbox
- **Network**: deny-all by default, opt-in through domain allowlist proxy on :8098
- **Auth**: optional Bearer token via `SILICONSANDBOX_AUTH_TOKEN`

## Tests

```bash
cd ~/workspace/silicon-sandbox
.venv/bin/python3 -m pytest tests/ -v
# 226 tests across 8 test modules
```

## Project Structure

```
silicon-sandbox/
├── sandbox-engine/          # Core engine (seatbelt, microvm, native, server)
│   ├── sandbox_engine/      # Python package
│   ├── guest-agent/         # MicroVM guest agent (init + shell scripts)
│   └── vm-launcher/         # Swift CLI (Virtualization.framework)
├── orchestrator/            # DAG engine, model router, planner, memory
├── tools/                   # MCP tool servers
│   ├── sandbox-mcp/         # Main MCP server (port 8100)
│   ├── code-interpreter/    # Python/Node/Bash execution
│   ├── file-manager/        # Scoped workspace operations
│   ├── web-research/        # DuckDuckGo + readability
│   └── browser-automation/  # Playwright (requires MicroVM)
├── sdk/                     # Python SDK (silicon_sandbox package)
├── ui/                      # Web UI (Alpine.js, single HTML file)
├── config/                  # YAML config, SBPL profiles, VM images
├── scripts/                 # install.sh, launch.sh, stop.sh
├── launchd/                 # LaunchAgent plists
└── tests/                   # 226 tests
```

## Version

0.4.0
