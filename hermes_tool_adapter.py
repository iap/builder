"""Hermes tool adapter for AWS Build agentic backend.

Receives tool-call requests from the bridge (a separate Q-inference process)
over a Unix socket and executes them through the live Hermes runtime.

Ownership model (verified):
  * The plugin process owns this server. `register(ctx)` captures the Hermes
    PluginContext and passes it in, so dispatch runs INSIDE the live Hermes
    process where every tool (write_file, read_file, terminal, ...) is already
    registered and has agent/session context.
  * The detached bridge is a client only. It cannot import Hermes model_tools
    reliably and must not execute tools itself.

Protocol: JSON lines over a Unix socket.
Request:
  {"tool":"write_file","args":{"path":"...","content":"..."}}
Response:
  {"ok":true,"result":"..."} or {"ok":false,"error":"..."}

Tool-name mapping (bridge -> Hermes):
  fs_write -> write_file
  fs_read  -> read_file
  bash     -> terminal
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_SOCKET = "aws-build-tools.sock"
_server: socket.socket | None = None
_server_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None

# The executor is set by the plugin at register() time. It must run inside the
# live Hermes process so tool handlers have their full runtime context.
_executor: Optional[Callable[[str, Dict[str, Any]], str]] = None


def set_executor(executor: Optional[Callable[[str, Dict[str, Any]], str]]) -> None:
    """Wire the live Hermes tool dispatcher (ctx.dispatch_tool) into the server."""
    global _executor
    _executor = executor


def _socket_path() -> str:
    socket_dir = os.environ.get("AMAZON_Q_TOOL_SOCKET_DIR") or os.path.join(
        os.path.expanduser("~"), ".hermes", "run"
    )
    return os.environ.get("AMAZON_Q_TOOL_SOCKET") or os.path.join(socket_dir, _DEFAULT_SOCKET)


def _safe_under_root(path: str, root: str | None) -> str:
    root = os.path.abspath(root or os.getcwd())
    cand = os.path.abspath(path if os.path.isabs(path) else os.path.join(root, path))
    if not (cand == root or cand.startswith(root + os.sep)):
        raise ValueError(f"path escapes sandbox: {path}")
    return cand


def _map_request(tool_name: str, args: Dict[str, Any]):
    """Map a bridge tool request to a Hermes tool name + args.

    Returns (hermes_tool_name, hermes_args). Raises ValueError on a sandbox
    escape so the connection handler can return a clean error.
    """
    mapped_name = tool_name
    hermes_args: Dict[str, Any] = dict(args)

    if tool_name == "fs_write":
        text = str(hermes_args.get("content") or "")
        root = hermes_args.get("root") or os.getcwd()
        if "\n" in text:
            rel_path, content = text.split("\n", 1)
        else:
            rel_path, content = text, ""
        hermes_args = {
            "path": _safe_under_root(rel_path.strip(), root),
            "content": content,
        }
        mapped_name = "write_file"
    elif tool_name == "fs_read":
        hermes_args = {
            "path": _safe_under_root(
                str(hermes_args.get("path") or "").strip(),
                hermes_args.get("root"),
            ),
        }
        mapped_name = "read_file"
    elif tool_name == "bash":
        hermes_args = {
            "command": str(hermes_args.get("command") or "").strip(),
            "workdir": _safe_under_root(".", hermes_args.get("root")) or None,
        }
        mapped_name = "terminal"
    return mapped_name, hermes_args


def _dispatch(tool_name: str, args: Dict[str, Any]) -> str:
    """Execute a tool via the live Hermes runtime; return a JSON result string."""
    if _executor is None:
        return json.dumps(
            {"error": "Hermes tool executor not initialized (plugin register() "
                      "did not set an executor)"},
            ensure_ascii=False,
        )
    try:
        mapped_name, hermes_args = _map_request(tool_name, args)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)

    try:
        result = _executor(mapped_name, hermes_args)
        if isinstance(result, str):
            return result
        return json.dumps({"result": result}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


def _handle_connection(conn: socket.socket) -> None:
    """Handle a single socket connection, reading JSON lines until close."""
    buf = b""
    conn.settimeout(None)
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception as exc:
                    conn.sendall(
                        json.dumps({"ok": False, "error": f"bad json: {exc}"}).encode()
                        + b"\n"
                    )
                    continue
                if not isinstance(payload, dict):
                    conn.sendall(
                        json.dumps({"ok": False, "error": "expected object"}).encode()
                        + b"\n"
                    )
                    continue
                tool_name = str(payload.get("tool") or "").strip()
                if not tool_name:
                    conn.sendall(
                        json.dumps({"ok": False, "error": "missing tool"}).encode()
                        + b"\n"
                    )
                    continue
                result = _dispatch(tool_name, dict(payload.get("args") or {}))
                conn.sendall(
                    json.dumps({"ok": True, "result": result}, ensure_ascii=False).encode()
                    + b"\n"
                )
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _serve(sock: socket.socket, stop_event: threading.Event) -> None:
    sock.listen(8)
    while not stop_event.is_set():
        try:
            sock.settimeout(0.5)
            try:
                conn, _ = sock.accept()
            except socket.timeout:
                continue
            t = threading.Thread(target=_handle_connection, args=(conn,), daemon=True)
            t.start()
        except Exception:
            if stop_event.is_set():
                break
            logger.debug("tool socket accept error", exc_info=True)


def start_tool_server() -> str:
    """Start the Hermes tool IPC server and return the socket path.

    Idempotent per path: if a server is already bound to the current socket
    path, its path is returned. If the desired path changed (e.g. env moved
    between tests), the old server is stopped and a new one bound.
    """
    global _server, _server_thread, _stop_event
    desired = _socket_path()
    if _server is not None:
        if os.path.exists(desired) and _socket_path() == desired:
            return desired
        stop_tool_server()

    path = desired
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except Exception:
        pass

    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(path)
        os.chmod(path, 0o600)
    except Exception as exc:
        sock.close()
        raise RuntimeError(f"tool socket bind failed: {path}: {exc}") from exc

    _server = sock
    _stop_event = threading.Event()
    _server_thread = threading.Thread(
        target=_serve, args=(sock, _stop_event), daemon=True
    )
    _server_thread.start()
    logger.info("aws-build Hermes tool server listening on %s", path)
    return path


def stop_tool_server() -> None:
    global _server, _server_thread, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _server is not None:
        try:
            _server.close()
        except Exception:
            pass
    _server = None
    _server_thread = None
    _stop_event = None
