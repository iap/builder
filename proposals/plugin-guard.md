# Proposal: Plugin-Level Tool Guard for Hermes Plugins (Global Pattern)

**Status:** Draft — not yet implemented
**Author:** iap
**Date:** 2026-07-30
**Scope:** Global plugin governance, not builder-specific
**Related:** PR #72 (merged), `hermes_cli/plugins.py` hooks, `agent/tool_guardrails.py`

---

## Problem

The builder plugin (and any plugin operating in agentic mode) currently has **no mechanism to enforce tool-use policy** at the plugin level when a model selected as `aws-builder` generates tool calls via Hermes's `openai_chat` transport. Specifically:

1. No plugin-level gate on destructive shell commands (`rm -rf`, `sudo`, etc.)
2. No boundary guard preventing writes to Hermes core paths (`~/.hermes/hermes-agent/`)
3. No way to signal capability boundaries (vision, reasoning, sandbox isolation) to the Hermes tool router
4. No plugin-level policy distinct from Hermes's global `approvals.mode` setting

The existing Hermes guard layers operate at the **core** level (`approval.mode`, `tool_guardrails.py`, `tool_use_enforcement`) and are unaware of plugin-specific boundaries. Plugins can register hooks for observation (`post_tool_call`, `pre_approval_request`) but these are **observer-only** — they cannot block or veto.

**However**, Hermes does expose a `pre_tool_call` hook that *can* block: `get_pre_tool_call_block_message()` checks registered `pre_tool_call` callbacks and returns the first block directive. This is the correct integration point for plugin-level tool governance.

---

## Current State: What Hermes Exposes

### `pre_tool_call` Hook (blocking-capable)

Registered in `hermes_cli/plugins.py` line 135 as a `VALID_HOOK`. Unlike observer-only hooks (`post_tool_call`, `pre_approval_request`), `pre_tool_call` callbacks can **block** tool execution:

```python
# From hermes_cli/plugins.py:2049
def get_pre_tool_call_block_message(tool_name, args, ...) -> Optional[str]:
    """Check pre_tool_call hooks for a blocking directive.

    Plugins return {"action": "block", "message": "Reason"} from
    their pre_tool_call callback. The first valid block wins.
    """
```

This is called in `agent_runtime_helpers.py:2101` and `tool_executor.py:418` **before** the tool is dispatched — if any plugin returns a block message, the tool call is intercepted and the message is surfaced to the user.

### How Plugins Register Hooks

In her plugin's `register()` function, the plugin calls `ctx.register_hook("pre_tool_call", callback)` where `callback` receives `(tool_name, args, ...)` and returns either `None` (allow) or `{"action": "block", "message": "..."}` (block).

This is the **existing mechanism** — no Hermes core changes needed.

### Builder's Current Hook Registration

The builder plugin currently registers zero hooks — it only registers tools via `registry.register()`. The `__init__.py` `register()` function has the `ctx` (PluginContext) argument but does not call `ctx.register_hook()` for any hook.

---

## Revised Proposal: Builder Uses pre_tool_call Hook

### Phase 1: Builder Registers a pre_tool_call Guard (plugin-only)

The builder plugin adds a `pre_tool_call` hook in its `register()` function (`__init__.py`) that enforces plugin-specific boundaries:

#### What the guard checks:

| Tool | Pattern | Action | Rationale |
|------|---------|--------|-----------|
| `terminal` | `rm -rf`, `shutil.rmtree`, `chmod -R`, `>/dev/sda`, `mkfs`, `dd if=` | `block` | Destructive shell commands — plugin should warn even if global `approvals.mode` is `off` |
| `terminal` | `sudo `, `su -`, `su `, `pkexec`, `doas ` | `block` | Privilege escalation from a plugin-originated request is suspicious |
| `write_file` / `patch` | Target under `~/.hermes/hermes-agent/` | `block` | Writes to Hermes core would break the installation |
| `write_file` / `patch` | Target under `~/.hermes/plugins/builder/` | `block` | Writes to the plugin's own install dir would corrupt the plugin |
| `terminal` | `>.hermes/config.yaml` | `block` | Modifying core config without explicit user action |
| `delegate_task` | No `allowed_toolsets` restriction | `warn` | Unrestricted subagent delegation could escape plugin boundaries |
| `process` | Long-running daemons | `warn` | Spawning persistent background processes from plugin tool calls |

#### How it works in practice:

1. User selects `aws-builder` model, Q generates a tool call containing `terminal` with `command: "rm -rf ~/.hermes/hermes-agent"`
2. Hermes dispatches this through the `openai_chat` transport to the adapter
3. The adapter translates `<tool_call>` XML back to a tool call and Hermes dispatches natively
4. Hermes calls `get_pre_tool_call_block_message()` before execution
5. The builder's `pre_tool_call` hook fires, sees `tool_name="terminal"`, checks `args["command"]` for `rm -rf` patterns
6. Hook returns `{"action": "block", "message": "⚠ The builder model attempted a destructive shell command: rm -rf ~/.hermes/hermes-agent. This is blocked by the builder plugin's tool guard. Set approvals.mode to 'off' in your Hermes config to allow auto-approval of non-destructive commands."}`
7. Hermes surfaces the block message; the tool call never executes
8. Q sees the block in the conversation and can self-correct

#### Why this is correct, not adapter-level:

The `pre_tool_call` hook fires at the **Hermes dispatch layer** — the right abstraction level. Adapter-level guarding was proposed as a "soft nudge" (advisory text delta), but the hook system gives us **hard enforcement** with full context (tool name, args, session ID). There's no reason to build a parallel guard in the adapter when Hermes already provides the right integration point.

### Phase 2: Capability Manifest (future, plugin.yaml extension)

Add a `capabilities` section to `plugin.yaml` that declares what the builder adapter supports **as guidance for the model** when it generates tool calls. This does NOT gate anything at dispatch — it's purely informative, included in the system prompt so Q can make better decisions about what tool calls to emit:

```yaml
capabilities:
  tool_use: true          # Q can request tool calls; adapter translates XML → tool_calls
  reasoning: true         # Q supports extended reasoning (Claude variants)
  vision: false           # Adapter is text-only; Q's vision capability is not exposed through this transport
  file_write: true        # Plugin can write files within Hermes's tool boundary
  file_read: true         # Plugin can read files within Hermes's tool boundary
  shell: true             # Terminal commands allowed (subject to approvals.mode and guard)
  sandbox: false          # Commands run in the user's real shell, not sandboxed
  delegate: false         # Plugin does not support delegate_task from Q-originated requests
```

This section is read at registration time by the builder plugin and passed as a **system context hint** in the adapter's system prompt (`_TOOL_CALL_INSTRUCTION`). It has **no enforcement effect** — it only informs Q's decision-making about what tool calls to propose. Enforcement is handled by Phase 1's `pre_tool_call` hook.

### Phase 3: Workdir Boundary Guard (future, plugin-specific)

A `workdir` boundary that restricts plugin-originated tool calls to a safe working directory. This is a Hermes-level concept that doesn't currently exist per-plugin, so it would require a Hermes core change (plugin-scoped `workdir` in the provider entry). This is out of scope for the builder plugin but documented here as a feature request.

---

## Implementation: Phase 1 Only (Ready to Implement)

### File: `__init__.py`

Add a `_plugin_pre_tool_call` handler and register it in `register()`:

```python
def _plugin_pre_tool_call(
    tool_name: str,
    args: dict[str, Any],
    **kwargs,
) -> dict[str, str] | None:
    """Builder plugin guard: blocks dangerous tool calls that
    could break the Hermes installation or compromise security."""
    _HERMES_CORE = (
        os.path.expanduser("~/.hermes/hermes-agent"),
        os.path.expanduser("~/.hermes/config.yaml"),
    )
    _DESTRUCTIVE_PATTERNS = (
        "rm -rf ", "shutil.rmtree", "chmod -R",
        ">/dev/sda", "mkfs", "dd if=",
    )
    _PRIVILEGE_PATTERNS = ("sudo ", "su -", "su ", "pkexec ", "doas ")

    if tool_name == "terminal":
        cmd = (args.get("command") or "").strip()
        for pattern in _DESTRUCTIVE_PATTERNS:
            if pattern in cmd:
                return {
                    "action": "block",
                    "message": (
                        f"⚠ Destructive shell command blocked by builder guard: "
                        f"`{cmd[:200]}`. This action was prevented even though "
                        f"approvals.mode is not set to 'off'. If you intend to run "
                        f"this command, do so directly from a terminal session."
                    ),
                }
        for pattern in _PRIVILEGE_PATTERNS:
            if cmd.startswith(pattern):
                return {
                    "action": "block",
                    "message": (
                        f"⚠ Privilege escalation blocked by builder guard: "
                        f"`{cmd[:200]}`. Plugin-originated shell commands do not "
                        f"support sudo/su. Run such commands directly from a terminal."
                    ),
                }
    elif tool_name in ("write_file", "patch"):
        target = str(args.get("path") or args.get("file") or "")
        for core_path in _HERMES_CORE:
            if target.startswith(core_path):
                return {
                    "action": "block",
                    "message": (
                        f"⚠ Write to Hermes protected path blocked by builder guard: "
                        f"`{target}`. Modifying Hermes core files may break the "
                        f"installation."
                    ),
                }
    return None  # allow the call
```

Then in `register()`:
```python
ctx.register_hook("pre_tool_call", _plugin_pre_tool_call)
```

### File: `plugin.yaml`

No changes needed — the hook mechanism is runtime-only.

---

## What This Does NOT Do (Explicit Boundaries)

| Item | Reason |
|------|--------|
| Override `approvals.mode` | The guard is additive; Hermes's approval layer remains the authority |
| Sandbox shell commands | Hermes has no per-plugin sandbox; `terminal` runs in the user's real shell |
| Restrict `read_file` / `search_files` | These are inherently safe (read-only); no guard needed |
| Cross-plugin governance | Each plugin guard only governs its own model path; Hermes core handles inter-plugin policy |
| Capability enforcement | `capabilities` in `plugin.yaml` (Phase 2) is advisory only |

---

## Comparison: Original vs Revised Approach

| Aspect | Original (Adapter-Level) | Revised (pre_tool_call Hook) |
|--------|--------------------------|------------------------------|
| Integration point | Adapter SSE stream (text delta) | Hermes `pre_tool_call` hook (pre-dispatch) |
| Enforcement | Advisory only (soft nudge) | Hard block (tool call never executes) |
| Context | Only tool_name + args | tool_name, args, session_id, task_id, turn_id |
| Correctness | Wrong layer — adapter only translates wire protocol | Right layer — hook fires at Hermes dispatch |
| User visibility | Warning text in chat | Block message in chat, tool call never reaches user |
| Hermes core changes | None needed | None needed |
| Future extensibility | Adapter must be modified for each new guard | New hooks can be added without adapter changes |

---

## Testing Plan

1. Unit tests for `_plugin_pre_tool_call` covering each block category (destructive, privilege, core-path write)
2. Verify block message is surfaced correctly through `get_pre_tool_call_block_message()`
3. Verify non-blocking tools (`read_file`, `search_files`, `models`, `tags`) are unaffected
4. `pytest tests/` passes (108+)
5. `verify.py` passes (no secret leak)
6. Integration test with a mock chat completion that triggers a blocked terminal call

---

## Recommendation

**Implement Phase 1 immediately.** It's a pure plugin-side change with no Hermes core dependency, it uses the existing `pre_tool_call` hook mechanism (proven at scale in Hermes core), and it provides real enforcement rather than advisory-only text deltas.

**Phase 2 (capability manifest)** is a lower-priority improvement that makes the adapter's capability signals explicit in `plugin.yaml`, improving model behavior when Q decides which tool calls to emit.

**Phase 3 (workdir boundary)** is a feature request for Hermes core — track separately and revisit when Hermes adds per-provider `workdir` scoping.