from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from ai_config_render import BUILTIN_TARGETS, main, render_instructions, render_json, render_toml

EXAMPLE = Path(__file__).parent.parent / "examples" / "basic"


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A source copy, a fake home, and a machine file that points both into tmp_path."""
    home, source = tmp_path / "home", tmp_path / "src"
    shutil.copytree(EXAMPLE, source)
    cfg = (source / "aicr.toml").read_text(encoding="utf-8")
    extra = "".join(f"[targets.{t}]\n" for t in BUILTIN_TARGETS if f"[targets.{t}]" not in cfg)
    (source / "aicr.toml").write_text(cfg + extra, encoding="utf-8")
    machine = tmp_path / "machine.toml"
    machine.write_text(
        f'name = "test"\nstate = {json.dumps(str(tmp_path / "state.json"))}\n[vars]\nhome = {json.dumps(home.as_posix())}\n',
        encoding="utf-8",
    )
    return source, home, machine


def run(cmd: str, source: Path, machine: Path) -> int:
    return main([cmd, "--source", str(source), "--machine", str(machine)])


def test_renders_every_builtin_format(env: tuple[Path, Path, Path]) -> None:
    source, home, machine = env
    assert run("render", source, machine) == 0
    for spec in BUILTIN_TARGETS.values():
        if "instructions" in spec:
            text = Path(spec["instructions"].replace("${home}", str(home))).read_text(encoding="utf-8")
            assert "<!-- aicr:style -->\n## Style" in text and "<!-- /aicr:testing -->" in text
        p = Path(spec["mcp"]["path"].replace("${home}", str(home)))
        if spec["mcp"]["format"] == "toml":
            srv = tomllib.loads(p.read_text(encoding="utf-8"))["mcp_servers"]["filesystem"]
        else:
            srv = json.loads(p.read_text(encoding="utf-8"))["mcpServers"]["filesystem"]
        assert srv["command"] == "npx" and srv["args"][-1] == f"{home.as_posix()}/projects"
        for d in spec.get("skills", []):
            assert (Path(d.replace("${home}", str(home))) / "commit-message" / "SKILL.md").is_file()
    copilot = json.loads((home / ".copilot" / "mcp-config.json").read_text(encoding="utf-8"))
    assert copilot["mcpServers"]["filesystem"]["tools"] == ["*"]


def test_idempotent_and_check(env: tuple[Path, Path, Path]) -> None:
    source, _, machine = env
    assert run("check", source, machine) == 1
    run("render", source, machine)
    assert run("check", source, machine) == 0
    assert run("render", source, machine) == 0
    assert run("check", source, machine) == 0


def test_preserves_user_content_and_drops_removed_entries(env: tuple[Path, Path, Path]) -> None:
    source, home, machine = env
    claude_md, codex = home / ".claude" / "CLAUDE.md", home / ".codex" / "config.toml"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("# Mine\nkeep me\n", encoding="utf-8")
    codex.parent.mkdir(parents=True)
    codex.write_text('model = "x"\n\n[mcp_servers.mine]\ncommand = "a"\n', encoding="utf-8")
    run("render", source, machine)
    cfg = (source / "aicr.toml").read_text(encoding="utf-8")
    (source / "aicr.toml").write_text(
        cfg.replace("[rules.testing]\n", "").replace("[mcp.filesystem]", "[mcp.gone]"), encoding="utf-8"
    )
    run("render", source, machine)
    text = claude_md.read_text(encoding="utf-8")
    assert text.startswith("# Mine\nkeep me\n") and "aicr:style" in text and "aicr:testing" not in text
    data = tomllib.loads(codex.read_text(encoding="utf-8"))
    assert data["model"] == "x" and set(data["mcp_servers"]) == {"mine", "gone"}
    assert list(claude_md.parent.glob("CLAUDE.md.aicr-bak-*"))


def test_list_targets(capsys: pytest.CaptureFixture[str], env: tuple[Path, Path, Path]) -> None:
    source, _, machine = env
    assert run("list-targets", source, machine) == 0
    assert "* codex" in capsys.readouterr().out


text_st = st.text(st.characters(exclude_categories=["Cs"]), max_size=200).filter(lambda s: "<!--" not in s)
name_st = st.from_regex(r"[a-z][a-z0-9_-]{0,8}", fullmatch=True)


@given(text_st, st.dictionaries(name_st, text_st.filter(lambda s: s.strip() != ""), max_size=4), text_st)
def test_instruction_merge_round_trip(user: str, blocks: dict[str, str], new_body: str) -> None:
    once = render_instructions(user, blocks, set())
    assert render_instructions(once, blocks, set()) == once  # idempotent
    for name, body in blocks.items():
        assert f"<!-- aicr:{name} -->\n{body.rstrip()}\n<!-- /aicr:{name} -->\n" in once
    assert render_instructions(once, {}, set(blocks)).rstrip("\n") == user.rstrip("\n")  # removing restores user text


json_val = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=10), lambda c: st.lists(c, max_size=3), max_leaves=5
)


@given(st.dictionaries(name_st, json_val, max_size=3), st.dictionaries(name_st, st.just({"command": "c"}), max_size=3))
def test_json_merge_keeps_user_keys(user: dict[str, object], servers: dict[str, dict[str, object]]) -> None:
    out = json.loads(render_json(json.dumps({"other": user}), servers, set(servers), "mcpServers"))
    assert out["other"] == user and out["mcpServers"] == servers
    assert json.loads(render_json(json.dumps(out), {}, set(servers), "mcpServers")) == {"other": user, "mcpServers": {}}


@given(
    st.dictionaries(
        name_st.filter(lambda n: n != "user"),
        st.dictionaries(name_st, st.text(max_size=10) | st.integers(), max_size=3),
        max_size=3,
    )
)
@example({"a": {"a": chr(0x7F)}})  # regression: a raw DEL made invalid TOML
def test_toml_round_trip(servers: dict[str, dict[str, object]]) -> None:
    out = render_toml('top = 1\n\n[mcp_servers.user]\ncommand = "u"\n', servers, set(servers))
    data = tomllib.loads(out)
    assert data["top"] == 1 and data["mcp_servers"].pop("user") == {"command": "u"}
    assert data["mcp_servers"] == servers
    assert render_toml(out, servers, set(servers)) == out
