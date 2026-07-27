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
  table{border-collapse:collapse;width:100%;max-width:600px;}
  th,td{text-align:left;padding:.4rem .8rem;border-bottom:1px solid #333;}
  th{color:#888;font-weight:normal;width:40%;}
  td{color:#eee;}
  .section{margin-top:2rem;}
  .section-title{color:#888;font-size:.75rem;letter-spacing:.1em;text-transform:uppercase;margin-bottom:.5rem;}
  .prefs-card{border:1px solid #333;border-radius:4px;padding:.75rem 1rem;max-width:600px;display:flex;gap:2rem;}
  .pref{display:flex;flex-direction:column;gap:.25rem;}
  .pref-label{color:#888;font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;}
  .pref-value{background:#1e3a5f;color:#7eb8f7;padding:.2rem .6rem;border-radius:3px;font-size:.85rem;display:inline-block;}
  /* theme: midnight */
  body.midnight{background:#080c14;color:#a8b8d0;}
  body.midnight h1{color:#c8daf0;}
  body.midnight th,body.midnight td{border-color:#1a2535;}
  body.midnight .prefs-card{border-color:#1a2535;}
  /* theme: ember */
  body.ember{background:#140c08;color:#d0b8a0;}
  body.ember h1{color:#f0d8c0;}
  body.ember th,body.ember td{border-color:#352015;}
  body.ember .prefs-card{border-color:#352015;}
  body.ember .pref-value{background:#3d1f0a;color:#f7b87e;}
  /* render_mode: tui */
  body.tui{border-radius:0 !important;padding:1rem;}
  body.tui .prefs-card{border-radius:0;}
  body.tui .pref-value{border-radius:0;}
  /* render_mode: cli */
  body.cli{font-size:.8rem;}
  body.cli .prefs-card{border:none;padding:0;}
</style>
</head>
<body>
<h1>builder</h1>
<table id="info"></table>
<div class="section">
  <div class="section-title">Render Preferences</div>
  <div class="prefs-card" id="prefs"></div>
</div>
<script>
fetch('/builder').then(r=>r.json()).then(d=>{
  const r = d.render || {};
  if(r.theme && r.theme !== 'default') document.body.classList.add(r.theme);
  if(r.render_mode && r.render_mode !== 'auto') document.body.classList.add(r.render_mode);
  const skip = new Set(['render']);
  const t = document.getElementById('info');
  for(const [k,v] of Object.entries(d)){
    if(skip.has(k)) continue;
    t.innerHTML += `<tr><th>${k}</th><td>${v}</td></tr>`;
  }
  const p = document.getElementById('prefs');
  for(const [k,v] of Object.entries(r)){
    p.innerHTML += `<div class="pref"><span class="pref-label">${k}</span><span class="pref-value">${v}</span></div>`;
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
