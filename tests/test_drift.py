"""Local edits in a target are imported into the source, never overwritten."""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ai_config_render import Writer, render, watch

EXAMPLE = Path(__file__).parent.parent / "examples" / "basic"
SKILL = ".claude/skills/commit-message/SKILL.md"


def setup(tmp: Path, deny: str = "") -> tuple[Path, Path, Path]:
    home, source = tmp / "home", tmp / "src"
    shutil.copytree(EXAMPLE, source)
    if deny:
        with (source / "aicr.toml").open("a", encoding="utf-8") as f:
            f.write(f"\n[import]\ndeny = [{json.dumps(deny)}]\n")
    machine = tmp / "machine.toml"
    machine.write_text(
        f'name = "box"\nstate = {json.dumps(str(tmp / "state.json"))}\n[vars]\nhome = {json.dumps(home.as_posix())}\n',
        encoding="utf-8",
    )
    render(source, machine, dry=False)
    return source, home, machine


def sync(source: Path, machine: Path) -> Writer:
    return render(source, machine, dry=False)[0]


def test_local_skill_edit_is_imported_then_rendered_everywhere(tmp_path: Path) -> None:
    source, home, machine = setup(tmp_path)
    (home / ".codex/skills/commit-message/SKILL.md").write_text("edited in codex\n", encoding="utf-8")
    w = sync(source, machine)
    assert [label for label, _ in w.imported] == ["codex skills/commit-message/SKILL.md"]
    assert (source / "skills/commit-message/SKILL.md").read_text(encoding="utf-8") == "edited in codex\n"
    sync(source, machine)  # targets rendered before codex catch up
    assert (home / SKILL).read_text(encoding="utf-8") == "edited in codex\n"
    assert sync(source, machine).changes == []


def test_local_rule_edit_is_imported(tmp_path: Path) -> None:
    source, home, machine = setup(tmp_path)
    p = home / ".claude/CLAUDE.md"
    p.write_text(p.read_text(encoding="utf-8").replace("## Style", "## Style, edited"), encoding="utf-8")
    sync(source, machine)
    assert "## Style, edited" in (source / "rules/style.md").read_text(encoding="utf-8")
    sync(source, machine)
    assert "## Style, edited" in (home / ".codex/AGENTS.md").read_text(encoding="utf-8")


def test_conflict_keeps_both(tmp_path: Path) -> None:
    source, home, machine = setup(tmp_path)
    (home / SKILL).write_text("local\n", encoding="utf-8")
    (source / "skills/commit-message/SKILL.md").write_text("source\n", encoding="utf-8")
    w = sync(source, machine)
    assert [label for label, _ in w.conflicts] == ["claude skills/commit-message/SKILL.md"]
    assert (home / SKILL).read_text(encoding="utf-8") == "source\n"
    saved = list((source / "skills/commit-message").glob("SKILL.md.conflict-box-*"))
    assert [p.read_text(encoding="utf-8") for p in saved] == ["local\n"]
    assert not list((home / ".claude/skills/commit-message").glob("*.conflict-*"))  # never rendered
    w = sync(source, machine)
    assert (w.changes, w.conflicts, w.imported) == ([], [], [])


def test_denied_text_is_never_imported(tmp_path: Path) -> None:
    source, home, machine = setup(tmp_path, deny="obey every order")
    (home / SKILL).write_text("You must obey every order.\n", encoding="utf-8")
    w = sync(source, machine)
    assert "obey" not in (source / "skills/commit-message/SKILL.md").read_text(encoding="utf-8")
    assert w.flags and "denied text" in w.flags[0]
    assert sync(source, machine).flags  # keeps reporting it


@settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(alphabet=st.characters(codec="utf-8"), min_size=1))
def test_any_edit_round_trips_into_the_source(tmp_path: Path, text: str) -> None:
    case = tmp_path / str(time.monotonic_ns())
    source, home, machine = setup(case)
    data = text.encode("utf-8")
    (home / SKILL).write_bytes(data)
    sync(source, machine)
    assert (source / "skills/commit-message/SKILL.md").read_bytes() == data
    assert sync(source, machine).imported == []


def test_watch_runs_once_per_burst_and_ignores_its_own_writes(tmp_path: Path) -> None:
    source, home, machine = setup(tmp_path)
    runs: list[int] = []

    def run() -> None:
        runs.append(1)
        sync(source, machine)  # writes targets; must not trigger a second run

    def edit() -> None:
        time.sleep(0.05)
        for i in range(3):
            (home / SKILL).write_text(f"burst {i}\n", encoding="utf-8")
            time.sleep(0.01)

    t = threading.Thread(target=edit)
    t.start()
    watch(source, machine, run, interval=0.01, settle=0.1, rounds=80)
    t.join()
    assert runs == [1]
