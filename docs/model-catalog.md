# Model Catalog

Available models and how the catalog is resolved.

## Available models

| Model | Notes |
|-------|-------|
| `claude-haiku-4.5` | Fast, lightweight |
| `claude-sonnet-4` | Balanced |
| `claude-sonnet-4.5` | Latest Sonnet |
| `auto` | Q picks the model server-side (default) |

`claude-opus-*` is not offered — Amazon Q rejects it ("Model does not exist").

## Resolution order

`backend.list_models()` resolves the catalog in this order:

1. `models:` list in `plugin.yaml` (operator-editable, no code change needed).
2. Built-in `STATIC_MODELS` fallback: `claude-haiku-4.5`, `claude-sonnet-4`, `claude-sonnet-4.5`.

The override is loaded lazily and cached on first call. Editing `plugin.yaml` takes effect on the next call without restarting Hermes.

## Unknown model handling

`_resolve_model_id()` coerces any model name not in the catalog to `"auto"`. Q returns an opaque HTTP 500 (`InternalServerException`) for any unsupported `modelId` — including plausible typos like `claude-sonnet-4-5` (dashes instead of dots). The coercion turns that crash into a usable response.

## Overriding the catalog

Edit `plugin.yaml`:

```yaml
models:
  - claude-haiku-4.5
  - claude-sonnet-4
  - claude-sonnet-4.5
```

The `ask_q` tool's `model` parameter enum and the `providers.aws-builder` model list in `config.yaml` are both derived from `list_models()`, so they stay in sync automatically.

## Tags

Free-form tags are resolved the same way: `tags:` in `plugin.yaml` overrides `STATIC_TAGS`. Exposed via the `tags` tool and included in `models` tool output.

Default tags: `aws`, `amazon-q`, `claude`, `chat`, `builder-id`, `auth`.
