"""Render one canonical AI-agent config into many agents' native formats.

It touches only what it owns:
  - instruction files: blocks between <!-- aicr:NAME --> and <!-- /aicr:NAME -->
  - MCP configs: the server names it renders now, and the ones it rendered last run
  - skills and plain files: paths it wrote last run (tracked in a state file)
Everything it overwrites is backed up first (one rolling <file>.aicr-bak).

It never clobbers a local edit. The state file keeps a hash of what aicr last
wrote to each owned file and block. If a target no longer matches that hash,
someone edited it there:
  - source unchanged -> the edit is imported into the source folder
  - source changed too -> conflict: the edit is saved next to its source as
    <source>.conflict-<machine>-<date>, then the source version is written
Text matching an [import] deny pattern is never imported.

`aicr watch` re-renders within seconds of a change to an owned file or the
source folder, and optionally runs a command after each render (commit, push).
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__version__ = "0.1.0"

CONFIG_NAME = "aicr.toml"
DEFAULT_MACHINE = Path("~/.config/aicr/machine.toml")
DEFAULT_STATE = "~/.config/aicr/state.json"
VAR = re.compile(r"\$\{(\w+)\}")
SKIP_NAMES = re.compile(r"__pycache__|\.aicr-(bak|tmp)|\.conflict-")

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


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


@dataclass
class Writer:
    dry: bool
    last: dict[str, str] = field(default_factory=dict)  # key -> hash of what we wrote last run
    host: str = "machine"
    deny: list[re.Pattern[str]] = field(default_factory=list)  # never import text that matches
    changes: list[tuple[Path, str | None, str | None]] = field(default_factory=list)
    pending: dict[Path, str] = field(default_factory=dict)  # what a dry run would have written
    rendered: dict[str, str] = field(default_factory=dict)
    imported: list[tuple[str, Path]] = field(default_factory=list)
    conflicts: list[tuple[str, Path]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def guard(self, key: str, cur: bytes | None, new: bytes, src: Path | None, label: str) -> bytes:
        """Return what the target should hold. A local edit is imported into src, or saved next to it as a conflict."""
        last = self.last.get(key)
        if cur is None or cur == new or sha(cur) == last:
            self.rendered[key] = sha(new)
            return new
        denied = any(rx.search(cur.decode("utf-8", "replace")) for rx in self.deny)
        if last is not None and sha(new) == last:  # only the target changed
            if src is None or denied:
                self.flags.append(
                    f"local edit to {label} not imported ({'denied text' if denied else 'templated source'}); target kept"
                )
                self.rendered[key] = last  # keep reporting it until someone resolves it
                return cur
            self.imported.append((label, src))
            if not self.dry:
                src.write_bytes(cur)
            self.rendered[key] = sha(cur)
            return cur
        # Both changed, or no record of what we wrote: keep the local copy next to the source, then write the source.
        if src is None or denied:
            self.flags.append(f"local edit to {label} overwritten, not saved ({'denied text' if denied else 'templated source'})")
        else:
            day = dt.datetime.now().astimezone().strftime("%Y%m%d")
            dst = src.with_name(f"{src.name}.conflict-{self.host}-{day}")
            self.conflicts.append((label, dst))
            if not self.dry:
                dst.write_bytes(cur)
        self.rendered[key] = sha(new)
        return new

    def read(self, p: Path) -> str | None:
        return self.pending.get(p, p.read_text(encoding="utf-8") if p.exists() else None)

    def backup(self, p: Path) -> None:
        if p.exists() and not self.dry:
            dst = p.with_name(f"{p.name}.aicr-bak")
            if dst.is_dir():
                shutil.rmtree(dst)
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

    def copy(self, src: Path, dst: Path, label: str) -> None:
        have = dst.read_bytes() if dst.exists() else None
        want = self.guard(str(dst), have, src.read_bytes(), src, label)
        if have == want:
            return
        self.changes.append((dst, None, None))
        if not self.dry:
            self.backup(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(want)

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


def block_body(text: str, name: str) -> str | None:
    n = re.escape(name)
    m = re.search(rf"<!-- aicr:{n} -->\n(.*?)<!-- /aicr:{n} -->", text, re.DOTALL)
    return m.group(1).rstrip() + "\n" if m else None


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
    deny = [re.compile(p, re.IGNORECASE) for p in cfg.get("import", {}).get("deny", [])]
    w = Writer(dry, last=state.get("_rendered", {}), host=machine, deny=deny)
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
            p = Path(t["instructions"]).expanduser()
            for name, r in cfg.get("rules", {}).items():
                if not applies(r, tname, machine):
                    continue
                src = source / "rules" / f"{name}.{tname}.md"
                src = src if src.exists() else source / "rules" / f"{name}.md"
                try:
                    rule_text = src.read_text(encoding="utf-8")
                    body = sub(rule_text, env).rstrip() + "\n"
                except Missing as e:
                    notes.append(f"{tname}: rule {name} skipped, no var {e}")
                    continue
                have = block_body(w.read(p) or "", name)
                blocks[name] = w.guard(
                    f"{p}#{name}",
                    have.encode("utf-8") if have is not None else None,
                    body.encode("utf-8"),
                    None if VAR.search(rule_text) else src,
                    f"{tname} rules/{name}",
                ).decode("utf-8")
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
                    if f.is_file() and not SKIP_NAMES.search(f.relative_to(skill).as_posix()):
                        w.copy(f, root / n / f.relative_to(skill), f"{tname} skills/{n}/{f.relative_to(skill).as_posix()}")
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
            templated = f.get("template", False)
            want = (sub(data, env) if templated else data).encode("utf-8")
            have = w.read(dest_p)
            got = w.guard(
                str(dest_p),
                have.encode("utf-8") if have is not None else None,
                want,
                None if templated else src,
                f"file {f['src']}",
            )
            w.text(dest_p, got.decode("utf-8"))
        except Missing as e:
            notes.append(f"file {dest} skipped, no var {e}")
            continue
        owned_files.append(str(dest_p))
    for gone in set(state.get("_files", [])) - set(owned_files):
        w.remove(Path(gone))
    new_state["_files"] = owned_files
    new_state["_rendered"] = w.rendered
    new_state.pop("_instructions", None)

    if not dry:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(new_state, indent=1), encoding="utf-8")
    return w, notes


# ---------- watch ----------


def watched(source: Path, machine_file: Path) -> list[Path]:
    """Every file aicr owns on this machine, plus the source tree."""
    _, mach, env = load(source, machine_file)
    state_file = Path(sub(mach.get("state", DEFAULT_STATE), env)).expanduser()
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    files = {Path(k.split("#")[0]) for k in state.get("_rendered", {})}
    files |= {p for p in source.rglob("*") if ".git" not in p.relative_to(source).parts}
    return sorted(files)


def snapshot(files: list[Path]) -> dict[Path, tuple[int, int]]:
    out = {}
    for f in files:
        try:
            st = f.stat()
            out[f] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass
    return out


def git(source: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def watch(
    source: Path,
    machine_file: Path,
    run: Callable[[], None],
    interval: float = 1.0,
    settle: float = 2.0,
    remote_every: float = 0.0,
    rounds: int | None = None,
) -> None:
    """Call `run` once a burst of changes to owned files or the source tree has been quiet for `settle` seconds.
    With remote_every > 0 and a git source, also pull and run when the upstream branch moves.
    Our own writes do not loop: the snapshot is retaken after each run.
    ponytail: polls stat() of the owned files every `interval` s, identical on Windows, macOS and Linux;
    switch to inotify/FSEvents/ReadDirectoryChangesW if the owned set grows to many thousands of files."""
    files = watched(source, machine_file)
    seen = snapshot(files)
    dirty_since: float | None = None
    next_remote = time.monotonic()
    n = 0
    while rounds is None or n < rounds:
        n += 1
        time.sleep(interval)
        now = time.monotonic()
        cur = snapshot(files)
        if cur != seen:
            seen, dirty_since = cur, now
        trigger = dirty_since is not None and now - dirty_since >= settle
        if not trigger and remote_every > 0 and now >= next_remote:
            next_remote = now + remote_every
            remote = git(source, "ls-remote", "origin", "HEAD").split()
            if remote and remote[0] != git(source, "rev-parse", "HEAD"):
                trigger = bool(git(source, "pull", "--ff-only", "-q") or True)
        if trigger:
            run()
            files = watched(source, machine_file)
            seen, dirty_since = snapshot(files), None


# ---------- CLI ----------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="aicr", description=(__doc__ or "").splitlines()[0])
    ap.add_argument("command", choices=["render", "diff", "check", "list-targets", "watch"])
    ap.add_argument("--source", type=Path, default=Path("."), help=f"folder with {CONFIG_NAME} (default: .)")
    ap.add_argument("--machine", type=Path, default=DEFAULT_MACHINE, help="per-machine vars file")
    ap.add_argument(
        "--exec", dest="exec_cmd", help="watch: shell command to run after each render, e.g. a commit-and-push script"
    )
    ap.add_argument("--remote-every", type=float, default=0.0, help="watch: seconds between upstream checks (0 = off)")
    a = ap.parse_args(argv)
    machine = a.machine.expanduser()

    if a.command == "watch":

        def run() -> None:
            stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            w, notes = render(a.source, machine, dry=False)
            for line in report(w, notes):
                print(f"{stamp} {line}", flush=True)
            if a.exec_cmd:
                subprocess.run(a.exec_cmd, shell=True, cwd=a.source, check=False)  # noqa: S602 - the user's own command

        watch(a.source, machine, run, remote_every=a.remote_every)
        return 0

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
    for line in report(w, notes)[len(w.changes) :]:
        print(line, file=sys.stderr)
    return 1 if a.command == "check" and (w.changes or w.conflicts or w.flags) else 0


def report(w: Writer, notes: list[str]) -> list[str]:
    return (
        [f"{'removed' if old == '' and new is None else 'changed'} {p}" for p, old, new in w.changes]
        + [f"imported: {label} -> {p}" for label, p in w.imported]
        + [f"CONFLICT: {label}: local edit saved as {p}" for label, p in w.conflicts]
        + [f"FLAG: {f}" for f in w.flags]
        + [f"note: {n}" for n in notes]
    )
