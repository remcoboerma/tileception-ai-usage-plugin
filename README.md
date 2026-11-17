# tileception-ai-usage-plugin

A [Tileception](https://github.com/robinvandernoord/tileception) plugin that shows your
rate-limit usage for **Claude**, **Codex** (OpenAI) and **OpenCode Go** as chips in the bar
and as a block in the notification panel.

- One chip per provider in the bar, showing the window that is closest to its limit.
- A panel block with a progress card for every window (5 hours, week, month, ...).
  The block unfolds by itself when a window passes 80%.
- Linux only, Python standard library only, no dependencies.

> **Unofficial endpoints.** None of the three usage endpoints is a documented public API.
> They can change or stop working without notice. See [How it works](#how-it-works).

## Requirements

- Tileception
- [uv](https://docs.astral.sh/uv/) (it fetches a suitable Python for you; the package needs 3.12 or newer)
- At least one of:
  - a Claude Code login (`~/.claude/.credentials.json`)
  - a Codex login with ChatGPT (`~/.codex/auth.json`)
  - an OpenCode Go API key

## Install

### From the launcher (recommended)

Open the Tileception launcher and type:

```
plugins add remcoboerma/tileception-ai-usage-plugin
```

Press Enter to see what will be installed, then Enter again to install. Tileception installs
the package with uv, runs `install --non-interactive`, and reloads plugins. A pinned version
works too: `plugins add remcoboerma/tileception-ai-usage-plugin@<tag-or-commit>`.

Claude and Codex need no setup: the plugin reads the login those tools already have.
For OpenCode Go, run `plugins configure usage` in the launcher and enter your API key.

### From a shell

```sh
uv tool install git+https://github.com/remcoboerma/tileception-ai-usage-plugin
tileception-usage install --non-interactive
tileception msg plugins reload
```

Or from a local checkout: `uv tool install .` instead of the first line.

Make sure `~/.local/bin` is on the compositor's `PATH`. The manifest written by `install`
uses the absolute path of the script, so this only matters for finding `tileception-usage`
yourself.

### OpenCode Go API key

If the launcher does not offer to configure the plugin (this can happen for plugins that were
not installed with `plugins add`), set the key in a terminal. Input is hidden:

```sh
tileception-usage configure
tileception msg plugins reload
```

The key is stored in `~/.config/tileception-usage/config.json` with mode `0600`.
The environment variable `OPENCODE_API_KEY` takes precedence over the stored key when it is set
in the session environment.

## Check that it works

```sh
tileception-usage status
```

This prints one line per provider with the reason it has no data, for example
`codex: token 3u 12m geleden verlopen: gebruik codex om te verversen` or
`go: geen API-key: kies 'plugins configure'`. It never prints tokens.

The same reasons appear as grey rows in the panel block. To poll right away:

```sh
tileception msg plugin usage refresh
```

## Troubleshooting

| What you see | Cause | What to do |
| --- | --- | --- |
| No chip for Codex | The access token in `auth.json` has expired. Codex only refreshes it when you use it, and this plugin never refreshes tokens. | Run `codex` once. |
| No chip for Go | No API key | `tileception-usage configure`, then `tileception msg plugins reload`. |
| `auth.json heeft geen tokens` | Codex is logged in with an API key, not with ChatGPT | No usage data exists for this endpoint. |
| `poll mislukt: HTTPError 429` | The endpoint rate-limited the request | Nothing; the plugin backs off up to 30 minutes. |
| Value shown as `~42%` in grey | The data is older than 10 minutes or the token has expired | Use the provider's CLI to refresh the login. |
| `reset` in grey | The window has reset since the last poll; the new value is unknown | Wait for the next poll. |

Errors from the plugin go to stderr, which Tileception writes to its session log with the
plugin name. `plugins` in the launcher lists discovery errors.

## Uninstall

From the launcher: `plugins remove usage` (plugins installed with `plugins add`).

From a shell:

```sh
tileception-usage uninstall --purge   # --purge also removes config and snapshots
uv tool uninstall tileception-usage
tileception msg plugins reload
```

## How it works

| Provider | Endpoint | Credentials |
| --- | --- | --- |
| Claude | `GET api.anthropic.com/api/oauth/usage` | OAuth token from `~/.claude/.credentials.json` (or `$CLAUDE_CONFIG_DIR`) |
| Codex | `GET chatgpt.com/backend-api/wham/usage` | Bearer token and account ID from `~/.codex/auth.json` (or `$CODEX_HOME`) |
| OpenCode Go | `GET opencode.ai/zen/go/v1/usage` | API key |

- **Read-only for credentials.** The plugin never refreshes or writes tokens. It only polls while
  the access token is still valid.
- **Event-driven polling.** The credential files are checked for changes every 5 seconds. A
  change triggers a poll, with at least 60 seconds between polls. Otherwise it polls every
  5 minutes, with exponential backoff up to 30 minutes after errors.
- **No stale numbers presented as fresh.** When the token has expired or the data is older than
  10 minutes, the last value is shown as `~42%` in a muted tint, with its age. A window whose
  reset time has passed shows `reset` instead of a value.
- **State.** Snapshots are stored in `$XDG_STATE_HOME/tileception-usage/` and config in
  `$XDG_CONFIG_HOME/tileception-usage/`, both written with mode `0600`.

The OpenCode Go parser accepts several field names (`usedPercent`, `used_percent`, `percent`,
`resetsAt`, `resetInSec`) because the response format is not documented. If your Go bars look
wrong, check the raw response and open an issue with the JSON (without your key):

```sh
curl -H "Authorization: Bearer $KEY" https://opencode.ai/zen/go/v1/usage
```

## Development

```sh
uv venv
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests
```

The tests use synthetic data and a fake `HOME` inside the repository. They do not call the
real endpoints.

The package implements the contract from the Tileception
[plugin docs](https://github.com/robinvandernoord/tileception/blob/master/docs/plugins.md):
`serve`, `install --non-interactive`, `configure`, `status` and `uninstall [--purge]`.
