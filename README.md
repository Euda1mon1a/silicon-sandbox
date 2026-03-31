# SiliconSandbox

[![PyPI](https://img.shields.io/pypi/v/silicon-sandbox)](https://pypi.org/project/silicon-sandbox/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://python.org)
[![macOS](https://img.shields.io/badge/platform-macOS%20Apple%20Silicon-lightgrey.svg)]()

Apple Silicon native AI agent sandbox and orchestration platform. Runs entirely on macOS using native primitives — no Docker, no cloud dependency.

## Install

```bash
pip install silicon-sandbox          # Engine + SDK
pip install silicon-sandbox[mcp]     # + MCP server for Claude Code / Cursor
```

Or run from source:

```bash
git clone https://github.com/Euda1mon1a/silicon-sandbox.git
cd silicon-sandbox
./scripts/install.sh
```

## What It Does

Provides isolated execution environments for AI agents to run code safely, plus an orchestrator to break complex tasks into parallel subtasks routed across local and cloud LLMs.

Three isolation tiers — pick the right tradeoff between security and speed:

| Tier | Technology | RAM | Boot | Use Case |
|------|-----------|-----|------|----------|
| A (Seatbelt) | macOS `sandbox-exec` + SBPL | ~0 MB | ~50ms | Default for code execution |
| B (MicroVM) | Apple Virtualization.framework + Alpine Linux | ~256 MB | ~260ms | Untrusted binaries, Linux envs, browser automation |
| C (Native) | `subprocess` + rlimit + setpgrp | ~0 MB | ~10ms | Trusted internal tools |

## Architecture

```
Web UI (:8095) ─── Orchestrator (:8094) ─── Sandbox Engine (:8093)
                         │                        │
                    Model Router              Three Tiers:
                    ├─ Local LLM (coder)      ├─ A: Seatbelt (sandbox-exec)
                    ├─ Local LLM (classifier) ├─ B: MicroVM (Virt.framework)
                    └─ Claude API (planner)   └─ C: Native (subprocess + rlimit)
                                                   │
MCP Server (:8100) ──────────────────────────── Engine API
  11 tools for Claude Code                         │
                                              Network Proxy (:8098)
                                              Domain allowlist, deny-all default
```

## Requirements

- macOS 14+ (Sonoma) on Apple Silicon (M1/M2/M3/M4)
- Python 3.12+
- Xcode Command Line Tools (for Swift vm-launcher build)
- Optional: Anthropic API key for the orchestrator's planner

## Quick Start

### From PyPI

```bash
pip install silicon-sandbox

# Start the engine
silicon-sandbox
# Engine running on http://127.0.0.1:8093

# Start the MCP server (optional, for Claude Code / Cursor)
pip install silicon-sandbox[mcp]
silicon-sandbox-mcp
# MCP server running on http://127.0.0.1:8100
```

### From Source

```bash
./scripts/install.sh   # Install deps, build vm-launcher, set up LaunchAgents
./scripts/launch.sh    # Start all services
./scripts/stop.sh      # Stop all services
```

### Verify

```bash
curl -s http://127.0.0.1:8093/health | python3 -m json.tool
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

# Execute in session
curl -X POST http://127.0.0.1:8093/session/{id}/exec \
  -H 'Content-Type: application/json' \
  -d '{"command": "python3 main.py"}'
```

### Python SDK

```python
from silicon_sandbox import Sandbox, Session

# One-shot execution
result = Sandbox.run("echo hello", tier="A")
print(result.stdout)

# Persistent session with file operations
with Session.create() as session:
    session.write_files({"main.py": "print('hello')"})
    result = session.exec("python3 main.py")
    print(result.stdout)
```

### MCP Tools (Claude Code / Cursor / AI Agents)

```bash
pip install silicon-sandbox[mcp]
silicon-sandbox-mcp  # Starts on http://127.0.0.1:8100
```

Add to your Claude Code `~/.claude.json`:

```json
{
  "mcpServers": {
    "silicon-sandbox": {
      "type": "http",
      "url": "http://127.0.0.1:8100/mcp"
    }
  }
}
```

Available tools (11):

- `sandbox_run` — one-shot sandboxed execution
- `sandbox_health` — engine health check
- `session_create` / `session_exec` / `session_destroy` — persistent sessions
- `session_write_files` / `session_read_file` — file operations in sessions
- `session_pause` / `session_resume` — SIGSTOP/SIGCONT process control
- `session_list` — list active sessions

### Desktop Automation (Tier B)

Tier B boots a full Alpine Linux VM with Xvfb, Openbox, and Chromium:

```bash
# Create a desktop session
curl -X POST http://127.0.0.1:8093/session \
  -H 'Content-Type: application/json' \
  -d '{"tier": "B", "image": "desktop"}'

# Take a screenshot
curl http://127.0.0.1:8093/session/{id}/screenshot --output screen.png

# Control the browser via CDP
curl -X POST http://127.0.0.1:8093/session/{id}/browser/control \
  -H 'Content-Type: application/json' \
  -d '{"method": "Page.navigate", "params": {"url": "https://example.com"}}'
```

## Security

- **Deny-default** Seatbelt profiles (SBPL v2): `(deny default)` base with selective allows
- **Blocked paths**: `~/.ssh`, `~/.gnupg`, `~/Library/Keychains`, `~/.config/git/credentials`, `~/.netrc`, `~/.aws`
- **Process isolation**: `os.setpgrp()` + `resource.setrlimit()` per sandbox
- **Network**: deny-all by default, opt-in through domain allowlist proxy on :8098
- **Auth**: optional Bearer token via `SILICONSANDBOX_AUTH_TOKEN` env var

## Configuration

Copy and edit `config/default.yaml`:

```yaml
sandbox:
  seatbelt:
    denied_paths: ["~/.ssh", "~/.gnupg", "~/Library/Keychains"]
    max_cpu_seconds: 120
    max_processes: 50
  microvm:
    default_cpus: 2
    default_memory_gb: 2
  network:
    proxy_port: 8098
    allowed_domains: ["pypi.org", "github.com"]
    deny_all_by_default: true

orchestrator:
  models:
    planner:
      provider: anthropic
      model: claude-sonnet-4-20250514
    coder:
      provider: openai_compatible
      endpoint: "http://127.0.0.1:8080/v1"
      model: "your-local-model"
```

The orchestrator's model router supports any OpenAI-compatible endpoint for local models and Anthropic API for planning/research roles. Set `ANTHROPIC_API_KEY` in your environment for the planner.

## Tests

```bash
.venv/bin/python3 -m pytest tests/ -v
# 246 tests across 8 modules
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
│   └── browser-automation/  # CDP via MicroVM desktop
├── sdk/                     # Python SDK (silicon_sandbox package)
├── ui/                      # Web UI (Alpine.js, single HTML file)
├── config/                  # YAML config, SBPL profiles, VM image scripts
├── scripts/                 # install.sh, launch.sh, stop.sh
├── launchd/                 # LaunchAgent plist templates
└── tests/                   # 246 tests
```

## Acknowledgments

Phase 8 hardening was informed by review of [Alibaba's OpenSandbox](https://github.com/alibaba/OpenSandbox) — specifically the deny-default Seatbelt profile pattern, `preexec_fn` process isolation approach, persistent session concept, and SDK design. No code was copied; the implementation is independent.

## License

MIT — see [LICENSE](LICENSE).
