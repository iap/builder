# Tools Reference

All tools registered by the builder plugin.

## `ask_q`

Send a prompt to Amazon Q (Claude) and return the answer.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `prompt` | string | yes | The prompt to send to Q. |
| `model` | string | no | Model to use. Defaults to `auto` (Q picks). See [Model Catalog](model-catalog.md). |
| `conversation_id` | string | no | Q conversation ID for multi-turn server-side context. |

**Returns:** `{"success": true, "answer": "...", "conversation_id": "..."}` (conversation_id omitted when absent).

**Note:** This path is chat/reasoning only. Q cannot execute Hermes tools from `ask_q`. Use the `-m aws-builder` model path for tool calls.

---

## `bid_login`

Start an Amazon Builder ID device login.

**Parameters:** none

**Returns:**
```json
{
  "success": true,
  "user_code": "ABCD-EFGH",
  "verification_uri": "https://device.sso.us-east-1.amazonaws.com",
  "verification_uri_complete": "https://device.sso.us-east-1.amazonaws.com/?user_code=ABCD-EFGH",
  "expires_in": 600,
  "interval": 5,
  "message": "Open the verification URL in your browser..."
}
```

If already authenticated: `{"success": true, "already_authenticated": true, "message": "Already authenticated..."}`.

A background thread polls for completion. Call `bid_status` to check.

---

## `bid_status`

Report current auth / device-login state. Actively polls once if a flow is pending.

**Parameters:** none

**Returns (authenticated):**
```json
{
  "success": true,
  "authenticated": true,
  "phase": "authenticated",
  "token_expires_at": 1234567890.0,
  "token_expires_at_iso": "2025-01-01T00:00:00+00:00",
  "scopes": ["codewhisperer:completions", "..."],
  "refreshed": false
}
```

**Returns (pending):**
```json
{
  "success": true,
  "authenticated": false,
  "phase": "awaiting_approval",
  "user_code": "ABCD-EFGH",
  "verification_uri_complete": "https://..."
}
```

**Phases:** `idle`, `awaiting_approval`, `authenticated`, `expired`, `error`.

---

## `bid_show_identity`

Return token identity metadata. No raw token.

**Parameters:** none

**Returns:**
```json
{
  "success": true,
  "authenticated": true,
  "token_type": "Bearer",
  "scopes": ["codewhisperer:completions", "..."],
  "has_refresh_token": true,
  "expires_at": 1234567890.0,
  "expires_at_iso": "2025-01-01T00:00:00+00:00"
}
```

---

## `bid_logout`

Stop polling and delete all stored secrets (`auth/bid_token.json`, `auth/bid_registration.json`, `auth/bid_flow.json`).

**Parameters:** none

**Returns:** `{"success": true, "message": "Logged out; secrets cleared."}`

---

## `models`

List available models and plugin tags.

**Parameters:** none

**Returns:**
```json
{
  "success": true,
  "models": ["auto", "claude-sonnet-4.5", "claude-sonnet-4", "claude-haiku-4.5"],
  "tags": ["aws", "amazon-q", "claude", "chat", "builder-id", "auth"]
}
```

---

## `tags`

List free-form tags describing the plugin.

**Parameters:** none

**Returns:** `{"success": true, "tags": ["aws", "amazon-q", "claude", "chat", "builder-id", "auth"]}`

---

## `q_debug`

Lightweight calibration snapshot: auth state, identity metadata, models, tags, and active Hermes render preferences. No raw secrets.

**Parameters:** none

**Returns:**
```json
{
  "success": true,
  "auth": {"authenticated": true, "phase": "authenticated", "token_expires_at": ..., "refreshed": false},
  "identity": {"token_type": "Bearer", "has_refresh_token": true, "scopes": [...], "expires_at": ...},
  "models": ["auto", "claude-sonnet-4.5", "claude-sonnet-4", "claude-haiku-4.5"],
  "tags": ["aws", "..."],
  "render": {"render_mode": "cli", "theme": "default"}
}
```
