"""Minimal dashboard server for the builder plugin."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from . import __version__, q_debug

_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>builder dashboard</title>
<style>
  body{font-family:monospace;background:#111;color:#ccc;padding:2rem;}
  h1{color:#fff;margin-bottom:1.5rem;}
  .board{display:flex;gap:1.5rem;flex-wrap:wrap;}
  .card{border:1px solid #333;border-radius:4px;padding:1rem;min-width:180px;}
  .card-title{color:#888;font-size:.7rem;text-transform:uppercase;letter-spacing:.1em;margin-bottom:.75rem;}
  .field{display:flex;flex-direction:column;gap:.15rem;margin-bottom:.6rem;}
  .field-label{color:#666;font-size:.65rem;text-transform:uppercase;letter-spacing:.08em;}
  .field-value{color:#eee;font-size:.85rem;word-break:break-all;}
  .badge{background:#1e3a5f;color:#7eb8f7;padding:.15rem .5rem;border-radius:3px;font-size:.8rem;display:inline-block;}
  body.midnight{background:#080c14;color:#a8b8d0;}
  body.midnight h1{color:#c8daf0;}
  body.midnight .card{border-color:#1a2535;}
  body.ember{background:#140c08;color:#d0b8a0;}
  body.ember h1{color:#f0d8c0;}
  body.ember .card{border-color:#352015;}
  body.ember .badge{background:#3d1f0a;color:#f7b87e;}
  body.tui .card{border-radius:0;}
  body.tui .badge{border-radius:0;}
  body.cli{font-size:.8rem;}
  body.cli .card{border:none;padding:0;}
</style>
</head>
<body>
<h1>builder</h1>
<div class="board">
  <div class="card" id="info"><div class="card-title">Plugin</div></div>
  <div class="card" id="prefs"><div class="card-title">Render Preferences</div></div>
</div>
<script>
fetch('/builder').then(r=>r.json()).then(d=>{
  const r=d.render||{};
  if(r.theme&&r.theme!=='default') document.body.classList.add(r.theme);
  if(r.render_mode&&r.render_mode!=='auto') document.body.classList.add(r.render_mode);
  const skip=new Set(['render']);
  const info=document.getElementById('info');
  for(const [k,v] of Object.entries(d)){
    if(skip.has(k)) continue;
    info.innerHTML+=`<div class="field"><span class="field-label">${k}</span><span class="field-value">${v}</span></div>`;
  }
  const prefs=document.getElementById('prefs');
  for(const [k,v] of Object.entries(r)){
    prefs.innerHTML+=`<div class="field"><span class="field-label">${k}</span><span class="badge">${v}</span></div>`;
  }
});
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_: Any) -> None:
        pass

    def do_GET(self) -> None:
        if self.path == "/":
            body = _HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/builder":
            data = q_debug()
            body = json.dumps(data, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def start(host: str = "127.0.0.1", port: int = 9119) -> None:
    """Start the dashboard HTTP server (blocking)."""
    server = HTTPServer((host, port), _Handler)
    server.serve_forever()
