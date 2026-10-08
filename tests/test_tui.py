import asyncio
import shutil
import socket
import sys
from pathlib import Path
from typing import Any, cast

import pytest
from textual.widgets import Input, Log, Static, Tree

from ecusdk.cli import main
from ecusdk.config import load_vehicle
from ecusdk.tui import (
    EcusdkApp,
    EditorScreen,
    RunScreen,
    VehiclesScreen,
    parse_value,
    save_validated,
)

DEMO = Path(__file__).parent.parent / "examples" / "demo-car.toml"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_parse_value() -> None:
    assert parse_value("0x7E0") == 0x7E0
    assert parse_value('"rpm"') == "rpm"
    assert parse_value("rpm") == "rpm"
    assert parse_value("[1, 2]") == [1, 2]
    assert parse_value("{ type = 'sine' }") == {"type": "sine"}


def test_save_validated_keeps_file_on_error(tmp_path: Path) -> None:
    path = tmp_path / "car.toml"
    shutil.copyfile(DEMO, path)
    before = path.read_text()
    error = save_validated(path, {"vehicle": {}})
    assert error is not None and "name" in error
    assert path.read_text() == before
    assert list(tmp_path.iterdir()) == [path]


def test_cli_tui_without_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ecusdk.tui", None)
    with pytest.raises(SystemExit) as exit_info:
        main(["tui"])
    assert exit_info.value.code == 1


def test_cli_tui_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("ecusdk.tui.run_tui", seen.append)
    assert main(["tui", "a.toml"]) == 0
    assert seen == [["a.toml"]]


def test_vehicles_new_copy_delete(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = EcusdkApp([DEMO], home=tmp_path)
        async with app.run_test() as pilot:
            screen = app.screen
            assert isinstance(screen, VehiclesScreen)
            assert "demo-car" in str(screen.query_one("#preview", Static).content)
            await pilot.press("c")
            assert (tmp_path / "demo-car-copia.toml").exists()
            await pilot.press("d")
            await pilot.click("#yes")
            assert not (tmp_path / "demo-car-copia.toml").exists()
            await pilot.press("n")
            assert isinstance(app.screen, EditorScreen)
            assert load_vehicle(tmp_path / "nuevo-auto.toml").name == "nuevo-auto"
            await pilot.press("escape")
            await pilot.press("e")
            assert isinstance(app.screen, EditorScreen)

    asyncio.run(scenario())


def test_editor_edits_adds_and_saves(tmp_path: Path) -> None:
    path = tmp_path / "car.toml"
    shutil.copyfile(DEMO, path)

    async def scenario() -> None:
        app = EcusdkApp([path], home=tmp_path / "home")
        async with app.run_test() as pilot:
            await pilot.press("e")
            editor = app.screen
            assert isinstance(editor, EditorScreen)
            tree = cast(Tree[Any], editor.query_one("#tree", Tree))
            nodes = {node.data: node for node in _walk(tree.root)}
            tree.move_cursor(nodes[("ecus", "ecm", "signals", "rpm", "initial")])
            await pilot.pause()
            assert editor.query_one("#value", Input).value == "850"
            editor.query_one("#value", Input).value = "3000"
            await pilot.click("#apply")
            tree.move_cursor(nodes[("ecus", "ecm", "signals")])
            await pilot.pause()
            editor.query_one("#key", Input).value = "temp"
            editor.query_one("#value", Input).value = "{ initial = 90 }"
            await pilot.click("#add")
            await pilot.press("ctrl+s")
            ecm = load_vehicle(path).ecu("ecm")
            assert ecm.get_signal("rpm") == 3000
            assert ecm.get_signal("temp") == 90
            editor.document["ecus"]["ecm"]["can"]["request_id"] = "bad"
            await pilot.press("ctrl+s")
            assert "no guardado" in str(editor.query_one("#status", Static).content)
            assert load_vehicle(path).ecu("ecm").request_id == 0x7E0

    asyncio.run(scenario())


def _walk(node: Any) -> list[Any]:
    return [node, *(found for child in node.children for found in _walk(child))]


def test_run_screen_launches_and_controls(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = EcusdkApp([DEMO], home=tmp_path)
        async with app.run_test() as pilot:
            await pilot.press("l")
            run = app.screen
            assert isinstance(run, RunScreen)
            run.query_one("#port", Input).value = str(_free_port())
            run.query_one("#control", Input).value = str(_free_port())
            await pilot.click("#toggle")
            live = run.query_one("#live", Static)
            for _ in range(50):
                await pilot.pause(0.2)
                if "demo-car" in str(live.content):
                    break
            assert "vehicle demo-car" in str(live.content)
            await pilot.click("#cmd")
            await pilot.press(*"set ecm.rpm 4000", "enter")
            await pilot.click("#pause")
            for _ in range(25):
                await pilot.pause(0.2)
                if "4000" in "\n".join(run.query_one(Log).lines):
                    break
            assert "4000" in "\n".join(run.query_one(Log).lines)
            process = run.process
            assert process is not None
            await pilot.click("#toggle")
            await asyncio.wait_for(process.wait(), 10)

    asyncio.run(scenario())


def test_vehicles_screen_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    async def scenario() -> None:
        app = EcusdkApp([], home=tmp_path / "home")
        async with app.run_test() as pilot:
            preview = app.screen.query_one("#preview", Static)
            assert "sin vehículos" in str(preview.content)
            await pilot.press("e", "c", "d", "l")
            assert isinstance(app.screen, VehiclesScreen)

    asyncio.run(scenario())
