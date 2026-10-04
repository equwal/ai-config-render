# ai-config-render (`aicr`)

Write your AI-agent rules, skills, and MCP servers once. `aicr` renders them into the native files of Claude Code, Codex, Copilot CLI, Gemini CLI, Antigravity, Cursor, Windsurf, or any agent you describe in five lines of TOML.

It never takes over a file. It owns only:

- **Instruction files:** the blocks between `<!-- aicr:NAME -->` and `<!-- /aicr:NAME -->`. Your own text stays.
- **MCP configs:** the server entries it wrote. Your other servers and settings stay.
- **Skills and plain files:** the paths it wrote last run (tracked in a state file).

It backs up every file before it changes it (`<file>.aicr-bak-<timestamp>`). Python 3.11+ standard library only.

## Quickstart

```bash
pipx install git+https://github.com/equwal/ai-config-render
aicr list-targets --source examples/basic   # built-in targets; * = enabled
aicr diff   --source examples/basic         # dry run: unified diff of every change
aicr render --source examples/basic         # write, with backups
aicr check  --source examples/basic         # exit 1 if any target drifted (CI, cron)
```

## Source layout

Any folder works, git or not:

```
aicr.toml            targets, rules, MCP servers, skills, files, vars
rules/<name>.md      one block per rule; rules/<name>.<target>.md overrides for one target
skills/<name>/       copied whole into each target's skills folders
```

See [`examples/basic/aicr.toml`](examples/basic/aicr.toml). Enable a built-in target with `[targets.claude]`; override any field, or define a new target:

```toml
[targets.myagent]
instructions = "${home}/.myagent/RULES.md"
skills = ["${home}/.myagent/skills"]
mcp = { format = "json", path = "${home}/.myagent/mcp.json", key = "servers" }  # or format = "toml"
```

Any rule, server, skill, or file takes `targets = [...]` and `machines = [...]` filters.

## Per-machine values

`~/.config/aicr/machine.toml` (or `--machine FILE`) holds what differs per computer: `name`, `[vars]`, `skip_targets`, `state`, and `[mcp.<server>]` overrides. `${var}` works in every path and string. Built-in vars: `home`, `home_posix`, `os`, `machine`. See [`examples/machine.example.toml`](examples/machine.example.toml).

## Why not ruler, rulesync, or chezmoi?

- **ruler / rulesync** generate project-level agent files and mostly overwrite them. `aicr` targets user-level config, edits only its own blocks and entries, and backs up first, so it can share a file with your hand edits and with other tools.
- **chezmoi** manages whole dotfiles. A single `~/.claude.json` mixes your MCP servers with app state that changes every minute; `aicr` merges one key instead of owning the file.

## Development

```bash
pip install -e .[dev]
pytest && ruff check . && ruff format --check . && mypy
```

MIT license.
