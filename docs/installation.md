# Installation & Setup

## Prerequisites

- Python 3.11+
- Hermes Agent installed at `$HERMES_HOME` (default `~/.hermes`)
- `requests` and `botocore` (present in the Hermes agent venv)

## Install

```bash
# from GitHub
hermes plugins install iap/builder

# or from a git URL
hermes plugins install https://github.com/iap/builder.git
```

## Register as a selectable chat model

`setup.sh` adds a `providers: aws-builder` entry to `config.yaml` pointing at the in-plugin adapter on `:8088`. It is idempotent and always backs up `config.yaml` first.

```bash
${HERMES_HOME:-$HOME/.hermes}/plugins/builder/scripts/setup.sh
```

Then restart Hermes.

## Authenticate

```bash
# inside a Hermes session
bid_login      # approve the user_code in your browser
bid_status     # confirm authenticated
```

Or use the standalone CLI:

```bash
python3 ${HERMES_HOME:-$HOME/.hermes}/plugins/builder/build_cli.py login
```

## Use

```bash
# as a tool (any model)
ask_q prompt="explain recursion"

# as a selectable model
hermes chat -m aws-builder
# or pick "AWS Builder" in the TUI
```

## Uninstall

Run the companion script before `hermes plugins uninstall` — Hermes core does not auto-remove the config entries that `setup.sh` added.

```bash
${HERMES_HOME:-$HOME/.hermes}/plugins/builder/scripts/uninstall.sh
hermes plugins uninstall builder
# restart Hermes
```

`uninstall.sh` removes `providers.aws-builder`, `plugins.enabled`, and toolset-list entries. Sibling providers are preserved.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HERMES_HOME` | `~/.hermes` | Hermes home directory. |
| `AWS_BUILD_ADAPTER_PORT` | `8088` | Port for the OpenAI-compatible adapter. |
| `AWS_BUILD_ADAPTER_HOST` | `localhost` | Bind host. Loopback only unless `AWS_BUILD_ADAPTER_ALLOW_PUBLIC=1`. |
| `AWS_BUILD_ADAPTER_ALLOW_PUBLIC` | unset | Set to `1` to allow non-loopback bind (not recommended). |

## Drift detection

`setup.sh` stamps the installed copy with its source git SHA (`plugins/builder/REVISION`). `verify.py` warns (does not fail) when the installed copy is behind the repo HEAD.

To sync:

```bash
hermes plugins uninstall builder
hermes plugins install iap/builder
${HERMES_HOME:-$HOME/.hermes}/plugins/builder/scripts/setup.sh
# restart Hermes
```
