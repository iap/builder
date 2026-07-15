# Plan: Anthropic-native mode for amazon_q_bridge

## Goal
Let the bridge also speak Claude's *native* Messages API shape, so Anthropic-SDK
clients (`anthropic` Python/TS SDK, or any code hitting `/v1/messages`) work
against `q chat` — not just OpenAI-SDK clients. Today the bridge emits OpenAI
shape only (lines 349-368 of amazon_q_bridge.py).

## Design decisions
- **New path** `POST /v1/anthropic/messages` (mirrors api.anthropic.com/v1/messages).
  Keeps OpenAI mode untouched (no behavior change for existing clients).
- **Response header** `anthropic-version: bedrock-2023-05-31` (Anthropic SDKs
  require/inspect this).
- **Non-streaming only first** (Anthropic `stream:false`). Streaming is a
  follow-up — `q chat` output is scraped post-hoc, so SSE needs the PTY
  incremental read we already have; defer to keep this change small.
- `max_tokens` is required by the Anthropic schema but `q chat` ignores it —
  accept and ignore (validate presence only if strict mode wanted; skip for now).
- Token counts: word-split estimate (same as OpenAI mode) — `q chat` gives no
  real token counts.

## Changes (amazon_q_bridge.py)
1. New `do_POST` branch: if path == `/v1/anthropic/messages`, parse Anthropic
   fields: `model`, `system` (optional string), `messages` (list of
   {role, content}), `max_tokens` (optional/ignored), `stream` (reject true w/
   400 "streaming not supported yet").
2. Build prompt: concatenate messages in order (system first if present), take
   last user turn like today — feed `_run_q_chat_pty(prompt, model)`.
3. Reuse existing guards: `_subscription_blocked`, `valid_models()`, `_run_q_chat_pty`,
   `extract_answer`. No new subprocess logic.
4. Emit Anthropic-native response:
   ```
   {
     "content": [{"type": "text", "text": answer}],
     "id": "msg_qbridge-<pid>",
     "model": model,
     "role": "assistant",
     "stop_reason": "end_turn",
     "stop_sequence": null,
     "type": "message",
     "usage": {"input_tokens": <est>, "output_tokens": <est>}
   }
   ```
   Set `self.send_header("anthropic-version", "bedrock-2023-05-31")` before body.
5. (Optional) `GET /v1/anthropic/models` → same model list, for symmetry. Skip
   unless needed (Anthropic SDK doesn't probe models).

## Tests
- **pytest** (tests/test_bridge.py or new test): start bridge on ephemeral port,
  POST to `/v1/anthropic/messages` with `{"model":"claude-sonnet-4",
  "max_tokens":100, "messages":[{"role":"user","content":"reply with exactly:
  ANTH_OK"}]}`, assert: 200, `type=="message"`, `content[0].type=="text"`,
  `content[0].text` contains `ANTH_OK`, `stop_reason=="end_turn"`, header present.
  Also assert OpenAI mode still returns `choices` (no regression).
- **Live curl** after reload: hit both paths, show both shapes side by side.
- **Bridge reload** via launchctl so the running service picks up the new code.

## Verification gates
- pytest: all pass (no regression on OpenAI path).
- Live: both `/v1/chat/completions` (OpenAI) and `/v1/anthropic/messages`
  (Anthropic) return correct shapes and real answers from `q chat`.
- py_compile clean.

## Adopted methodology: "Loops that write & review code" (Bun blog)
Per the user's direction, apply Bun's LLM-code-trust loop:
1. **Test suite as gate** — language-independent assertions (pytest + live
   curl) are the objective pass/fail, not self-approval.
2. **Adversarial review (separate context)** — after implementing, spawn an
   INDEPENDENT subagent whose ONLY job is to find faults in the change:
   response-shape mismatches vs the real Anthropic Messages API, missing
   `anthropic-version` header, wrong `max_tokens`/`stream` handling, OpenAI
   regression, prompt-build bugs, error-path gaps. It must NOT write code.
   Separation of duties: implementer != reviewer.
3. **Fix the process, not the code** — any gap the reviewer finds gets baked
   into a pytest assertion so it cannot regress, rather than hand-patched ad hoc.
4. No commit until adversarial review is clean AND all gates pass.

## Out of scope (follow-ups)
- SSE streaming for Anthropic mode.
- Real token counts (needs a tokenizer; q chat doesn't expose them).
- Bedrock-direct (Converse) backend — bridge stays in front of `q chat`.
