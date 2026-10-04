"""Render one canonical AI-agent config into many agents' native formats.

It touches only what it owns:
  - instruction files: blocks between <!-- aicr:NAME --> and <!-- /aicr:NAME -->
  - MCP configs: the server names it renders now, and the ones it rendered last run
  - skills and plain files: paths it wrote last run (tracked in a state file)
Everything it overwrites is backed up first.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__version__ = "0.1.0"

CONFIG_NAME = "aicr.toml"
DEFAULT_MACHINE = Path("~/.config/aicr/machine.toml")
DEFAULT_STATE = "~/.config/aicr/state.json"
VAR = re.compile(r"\$\{(\w+)\}")

# A target is data. `mcp.format` is "json" (servers under `mcp.key`, default
# "mcpServers") or "toml" (servers under [mcp_servers.<name>]). Any field can be
# overridden, and new targets added, under [targets.<name>] in aicr.toml.
BUILTIN_TARGETS: dict[str, dict[str, Any]] = {
    "claude": {
        "instructions": "${home}/.claude/CLAUDE.md",
        "skills": ["${home}/.claude/skills"],
        "mcp": {"format": "json", "path": "${home}/.claude.json"},
    },
    "codex": {
        "instructions": "${home}/.codex/AGENTS.md",
        "skills": ["${home}/.codex/skills"],
        "mcp": {"format": "toml", "path": "${home}/.codex/config.toml"},
    },
    "copilot": {
        "instructions": "${home}/.copilot/copilot-instructions.md",
        "skills": ["${home}/.copilot/skills"],
        "mcp": {"format": "json", "path": "${home}/.copilot/mcp-config.json", "defaults": {"tools": ["*"]}},
    },
    "gemini": {
        "instructions": "${home}/.gemini/GEMINI.md",
        "skills": ["${home}/.gemini/skills"],
        "mcp": {"format": "json", "path": "${home}/.gemini/settings.json"},
    },
    "antigravity": {
        "instructions": "${home}/.gemini/GEMINI.md",
        "skills": ["${home}/.gemini/antigravity/skills"],
        "mcp": {"format": "json", "path": "${home}/.gemini/antigravity/mcp_config.json"},
    },
    "cursor": {
        "mcp": {"format": "json", "path": "${home}/.cursor/mcp.json"},
    },
    "windsurf": {
        "instructions": "${home}/.codeium/windsurf/memories/global_rules.md",
        "mcp": {"format": "json", "path": "${home}/.codeium/windsurf/mcp_config.json"},
    },
}


class Missing(Exception):
    """A ${var} has no value."""


def sub(value: Any, env: dict[str, str]) -> Any:
    """Expand ${var} in every string of a nested value. An unknown var raises Missing."""
    if isinstance(value, str):

        def rep(m: re.Match[str]) -> str:
            if m.group(1) not in env:
                raise Missing(m.group(1))
            return env[m.group(1)]

        return VAR.sub(rep, value)
    if isinstance(value, list):
        return [sub(v, env) for v in value]
    if isinstance(value, dict):
        return {k: sub(v, env) for k, v in value.items()}
    return value


def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def applies(entry: dict[str, Any], target: str, machine: str) -> bool:
    """Filters: `targets` (default all), `machines` (default all)."""
    if target and "targets" in entry and target not in entry["targets"]:
        return False
    return "machines" not in entry or machine in entry["machines"]


@dataclass
class Writer:
    dry: bool
    stamp: str = field(default_factory=lambda: dt.datetime.now().astimezone().strftime("%Y%m%d%H%M%S"))
    changes: list[tuple[Path, str | None, str | None]] = field(default_factory=list)
    pending: dict[Path, str] = field(default_factory=dict)  # what a dry run would have written

    def read(self, p: Path) -> str | None:
        return self.pending.get(p, p.read_text(encoding="utf-8") if p.exists() else None)

    def backup(self, p: Path) -> None:
        if p.exists() and not self.dry:
            dst = p.with_name(f"{p.name}.aicr-bak-{self.stamp}")
            if p.is_dir():
                shutil.copytree(p, dst)
            else:
                shutil.copy2(p, dst)

    def text(self, p: Path, new: str) -> None:
        old = self.read(p)
        if old == new:
            return
        self.changes.append((p, old, new))
        self.pending[p] = new
        if self.dry:
            return
        self.backup(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".aicr-tmp")
        tmp.write_text(new, encoding="utf-8", newline="")
        os.replace(tmp, p)

    def copy(self, src: Path, dst: Path) -> None:
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            return
        self.changes.append((dst, None, None))
        if not self.dry:
            self.backup(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    def remove(self, p: Path) -> None:
        if not p.exists():
            return
        self.changes.append((p, "", None))
        if not self.dry:
            self.backup(p)
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()


# ---------- instruction files ----------


def block_re(name: str) -> re.Pattern[str]:
    n = re.escape(name)
    return re.compile(rf"<!-- aicr:{n} -->\n.*?<!-- /aicr:{n} -->\n?", re.DOTALL)


def render_instructions(text: str, blocks: dict[str, str], dropped: set[str]) -> str:
    """Replace or append each owned block; remove dropped ones; keep all other text."""
    for name in dropped:
        text = block_re(name).sub("", text)
    for name, body in blocks.items():
        new = f"<!-- aicr:{name} -->\n{body.rstrip()}\n<!-- /aicr:{name} -->\n"
        rx = block_re(name)
        first = rx.search(text)
        if first:
            # Keep the first position, drop duplicates.
            text = text[: first.start()] + new + rx.sub("", text[first.end() :])
        else:
            text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + new
    return text


# ---------- MCP ----------


def toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else json.dumps(k)


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False).replace(chr(0x7F), "\\u007f")  # TOML forbids a raw DEL
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{toml_key(k)} = {toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def render_toml(text: str, servers: dict[str, dict[str, Any]], owned: set[str]) -> str:
    """Drop every [mcp_servers.<owned>...] table, then append ours in one marked block."""
    text = re.sub(r"# aicr:mcp\n.*?# /aicr:mcp\n?", "", text, flags=re.DOTALL)
    out: list[str] = []
    skip = False
    for line in text.splitlines(keepends=True):
        m = re.match(r"\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$", line)
        if m:
            parts = [p.strip().strip("\"'") for p in re.split(r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", m.group(1))]
            skip = len(parts) >= 2 and parts[0] == "mcp_servers" and parts[1] in owned
        if not skip:
            out.append(line)
    text = "".join(out).rstrip("\n")
    if servers:
        body = ["# aicr:mcp"]
        for name, cfg in servers.items():
            body.append(f"[mcp_servers.{toml_key(name)}]")
            body += [f"{toml_key(k)} = {toml_value(v)}" for k, v in cfg.items()]
            body.append("")
        text += ("\n\n" if text else "") + "\n".join(body) + "# /aicr:mcp\n"
    tomllib.loads(text)  # refuse to write a broken config
    return text if not text or text.endswith("\n") else text + "\n"


def render_json(text: str, servers: dict[str, dict[str, Any]], owned: set[str], key: str) -> str:
    data = json.loads(text) if text.strip() else {}
    cur = data.setdefault(key, {})
    for name in owned - servers.keys():
        cur.pop(name, None)
    cur.update(servers)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


# ---------- render ----------


def load(source: Path, machine_file: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    cfg = tomllib.loads((source / CONFIG_NAME).read_text(encoding="utf-8"))
    mach = tomllib.loads(machine_file.read_text(encoding="utf-8")) if machine_file.exists() else {}
    env = {
        "home": str(Path.home()),
        "home_posix": Path.home().as_posix(),
        "os": "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else "linux",
        **{k: str(v) for k, v in cfg.get("vars", {}).items()},
        **{k: str(v) for k, v in mach.get("vars", {}).items()},
    }
    env["machine"] = str(mach.get("name", env["os"]))
    for k, v in env.items():  # one level of vars-in-vars, e.g. projects = "${home}/code"
        env[k] = VAR.sub(lambda m: env.get(m.group(1), m.group(0)), v)
    return cfg, mach, env


def targets(cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Built-ins merged with [targets.*]. Without a [targets] table, no target is enabled."""
    out = {}
    for name, t in cfg.get("targets", {}).items():
        spec = merge(BUILTIN_TARGETS.get(name, {}), t)
        if spec.pop("enabled", True):
            out[name] = spec
    return out


def render(source: Path, machine_file: Path, dry: bool) -> tuple[Writer, list[str]]:
    cfg, mach, env = load(source, machine_file)
    machine = env["machine"]
    state_file = Path(sub(mach.get("state", DEFAULT_STATE), env)).expanduser()
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    new_state: dict[str, Any] = {}
    w = Writer(dry)
    notes: list[str] = []

    for tname, raw in targets(cfg).items():
        if tname in mach.get("skip_targets", []):
            continue
        try:
            t = sub(raw, env)
        except Missing as e:
            notes.append(f"{tname}: skipped, no var {e}")
            continue
        prev = state.get(tname, {})
        cur = new_state.setdefault(tname, {})

        if "instructions" in t:
            blocks: dict[str, str] = {}
            for name, r in cfg.get("rules", {}).items():
                if not applies(r, tname, machine):
                    continue
                src = source / "rules" / f"{name}.{tname}.md"
                src = src if src.exists() else source / "rules" / f"{name}.md"
                try:
                    blocks[name] = sub(src.read_text(encoding="utf-8"), env)
                except Missing as e:
                    notes.append(f"{tname}: rule {name} skipped, no var {e}")
            p = Path(t["instructions"]).expanduser()
            # Two targets can share one file (gemini, antigravity): merge their blocks.
            file_state = new_state.setdefault("_instructions", {}).setdefault(str(p), [])
            dropped = set(prev.get("rules", [])) - blocks.keys() - set(file_state)
            file_state.extend(sorted(blocks))
            if blocks or dropped:
                w.text(p, render_instructions(w.read(p) or "", blocks, dropped))
            cur["rules"] = sorted(blocks)

        if "mcp" in t:
            servers: dict[str, dict[str, Any]] = {}
            for name, s in cfg.get("mcp", {}).items():
                if not applies(s, tname, machine):
                    continue
                m = mach.get("mcp", {}).get(name, {})
                # Precedence, low to high: target defaults, server, server.target.<t>, machine, machine.target.<t>
                spec: dict[str, Any] = t["mcp"].get("defaults", {})
                for layer in (s, s.get("target", {}).get(tname, {}), m, m.get("target", {}).get(tname, {})):
                    spec = merge(spec, layer)
                spec = {k: v for k, v in spec.items() if k not in ("targets", "machines", "target")}
                try:
                    servers[name] = sub(spec, env)
                except Missing as e:
                    notes.append(f"{tname}: mcp {name} skipped, no var {e}")
            p = Path(t["mcp"]["path"]).expanduser()
            owned = set(servers) | set(prev.get("mcp", []))
            if servers or owned:
                old = w.read(p) or ""
                fmt = t["mcp"]["format"]
                if fmt == "toml":
                    w.text(p, render_toml(old, servers, owned))
                elif fmt == "json":
                    w.text(p, render_json(old, servers, owned, t["mcp"].get("key", "mcpServers")))
                else:
                    raise SystemExit(f"{tname}: unknown mcp format {fmt!r} (use json or toml)")
            cur["mcp"] = sorted(servers)

        names = [n for n, s in cfg.get("skills", {}).items() if applies(s, tname, machine)]
        for d in t.get("skills", []):
            root = Path(d).expanduser()
            for n in names:
                skill = source / "skills" / n
                for f in sorted(skill.rglob("*")) if skill.is_dir() else []:
                    if f.is_file() and "__pycache__" not in f.parts:
                        w.copy(f, root / n / f.relative_to(skill))
            for n in set(prev.get("skills", [])) - set(names):
                w.remove(root / n)
        cur["skills"] = names

    owned_files: list[str] = []
    for dest, f in cfg.get("files", {}).items():
        if not applies(f, "", machine):
            continue
        try:
            dest_p, src = Path(sub(dest, env)).expanduser(), source / sub(f["src"], env)
            data = src.read_text(encoding="utf-8")
            w.text(dest_p, sub(data, env) if f.get("template", False) else data)
        except Missing as e:
            notes.append(f"file {dest} skipped, no var {e}")
            continue
        owned_files.append(str(dest_p))
    for gone in set(state.get("_files", [])) - set(owned_files):
        w.remove(Path(gone))
    new_state["_files"] = owned_files
    new_state.pop("_instructions", None)

    if not dry:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(new_state, indent=1), encoding="utf-8")
    return w, notes


# ---------- CLI ----------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aicr", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("command", choices=["render", "diff", "check", "list-targets"])
    ap.add_argument("--source", type=Path, default=Path("."), help=f"folder with {CONFIG_NAME} (default: .)")
    ap.add_argument("--machine", type=Path, default=DEFAULT_MACHINE, help="per-machine vars file")
    a = ap.parse_args(argv)
    machine = a.machine.expanduser()

    if a.command == "list-targets":
        cfg = tomllib.loads((a.source / CONFIG_NAME).read_text(encoding="utf-8")) if (a.source / CONFIG_NAME).exists() else {}
        enabled = targets(cfg)
        for name in sorted(BUILTIN_TARGETS.keys() | enabled.keys()):
            spec = enabled.get(name, BUILTIN_TARGETS.get(name, {}))
            mark = "*" if name in enabled else " "
            print(f"{mark} {name:12} {spec.get('instructions', '-')}  mcp={spec.get('mcp', {}).get('path', '-')}")
        return 0

    w, notes = render(a.source, machine, dry=a.command != "render")
    for p, old, new in w.changes:
        if a.command == "diff" and new is not None:
            sys.stdout.writelines(difflib.unified_diff((old or "").splitlines(True), new.splitlines(True), f"a/{p}", f"b/{p}"))
        else:
            print(f"{'removed' if old == '' and new is None else 'changed'} {p}")
    for n in notes:
        print(f"note: {n}", file=sys.stderr)
    return 1 if a.command == "check" and w.changes else 0
