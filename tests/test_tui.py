import asyncio
import shutil
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from textual.widgets import (
    Checkbox,
    DataTable,
    Input,
    Log,
    RadioButton,
    Select,
    Static,
    Tree,
)

from ecusdk.cli import main
from ecusdk.config import load_vehicle
from ecusdk.tui import (
    CarsScreen,
    ConnectionScreen,
    CustomizeScreen,
    DashboardScreen,
    EcusdkApp,
    EditorScreen,
    Setup,
    SummaryScreen,
    parse_inspect,
    parse_value,
    save_validated,
)

DEMO = Path(__file__).parent.parent / "examples" / "demo-car.toml"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _until(pilot: Any, condition: Callable[[], bool]) -> None:
    """Espera una condición de la UI (CI lento, recompose asíncrono)."""
    for _ in range(100):
        if condition():
            return
        await pilot.pause(0.1)
    raise AssertionError("la UI no llegó al estado esperado")


def _car(tmp_path: Path) -> Path:
    path = tmp_path / "car.toml"
    shutil.copyfile(DEMO, path)
    return path


def test_parse_value() -> None:
    assert parse_value("0x7E0") == 0x7E0
    assert parse_value('"rpm"') == "rpm"
    assert parse_value("rpm") == "rpm"
    assert parse_value("{ type = 'sine' }") == {"type": "sine"}


def test_parse_inspect() -> None:
    live = parse_inspect(
        "vehicle demo (paused)\n"
        "  ecu ecm: request 0x7E0 response 0x7E8 bus *\n"
        "    signal rpm = 850 (generator)\n"
        "    signal speed = 12.5\n"
        "    dtcs P0300\n"
        "    fault ecu_offline (2.000s left)\n"
    )
    assert live.state == "paused"
    assert live.signals == [("ecm", "rpm", 850, True), ("ecm", "speed", 12.5, False)]
    assert live.dtcs == ["P0300 · Motor (ecm)"]
    assert live.faults == ["ecu_offline (2.000s left) · Motor (ecm)"]


def test_setup_argv() -> None:
    setup = Setup(Path("car.toml"), ble=True, share=True, record="s.eculog")
    argv = setup.argv()
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert "--ble" in argv and argv[argv.index("--record") + 1] == "s.eculog"


def test_save_validated_keeps_file_on_error(tmp_path: Path) -> None:
    path = _car(tmp_path)
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


def test_cars_new_copy_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    home = tmp_path / "home"

    async def scenario() -> None:
        app = EcusdkApp([], home=home)
        async with app.run_test(size=(130, 40)) as pilot:
            cars = app.screen
            assert isinstance(cars, CarsScreen)
            assert "Crear" in str(cars.query_one("#card", Static).content)
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, CustomizeScreen)
            await pilot.click("#next")
            await pilot.pause()
            assert app.screen is cars
            assert load_vehicle(home / "mi-auto.toml").name == "Mi auto"
            await pilot.press("c")
            assert (home / "mi-auto-copia.toml").exists()
            await pilot.press("d")
            await pilot.click("#yes")
            assert not (home / "mi-auto-copia.toml").exists()
            (home / "roto.toml").write_text("[vehicle]\n")
            cars.reload(home / "roto.toml")
            await pilot.pause()
            assert "problema" in str(cars.query_one("#card", Static).content)
            await pilot.click("#next")
            await pilot.pause()
            assert app.screen is cars
            await pilot.press("escape")
            await pilot.pause()

    asyncio.run(scenario())


def test_customize_edits_adds_and_drops(tmp_path: Path) -> None:
    path = _car(tmp_path)

    async def scenario() -> None:
        app = EcusdkApp([path], home=tmp_path / "home")
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, CustomizeScreen)
            screen.query_one("#name", Input).value = "Mi demo"
            screen.query_one("#value-0", Input).value = "3000"
            cast(Select[str], screen.query_one("#add-0", Select)).value = "fuel_level"
            await _until(pilot, lambda: bool(screen.query("#drop-2")))
            await pilot.click("#drop-1")  # speed
            await _until(pilot, lambda: not screen.query("#drop-2"))
            screen.query_one("#value-0", Input).value = "abc"
            await pilot.click("#next")
            await pilot.pause()
            assert app.screen is screen
            await pilot.pause(0.3)  # el botón ignora clics durante su animación
            screen.query_one("#value-0", Input).value = "3000"
            await pilot.click("#next")
            await pilot.pause()
            assert isinstance(app.screen, CarsScreen)
        vehicle = load_vehicle(path)
        ecm = vehicle.ecu("ecm")
        assert vehicle.name == "Mi demo"
        assert ecm.get_signal("rpm") == 3000
        assert "speed" not in ecm.signals and "fuel_level" in ecm.signals
        assert ecm.obd.bindings()[0x2F] == "fuel_level"

    asyncio.run(scenario())


def _walk(node: Any) -> list[Any]:
    return [node, *(found for child in node.children for found in _walk(child))]


def test_advanced_editor(tmp_path: Path) -> None:
    path = _car(tmp_path)

    async def scenario() -> None:
        app = EcusdkApp([path], home=tmp_path / "home")
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.press("p")
            await pilot.pause()
            await pilot.click("#advanced")
            await pilot.pause()
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


def test_wizard_launches_and_controls(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = EcusdkApp([DEMO], home=tmp_path)
        async with app.run_test(size=(130, 44)) as pilot:
            await pilot.click("#next")
            await pilot.pause()
            connection = app.screen
            assert isinstance(connection, ConnectionScreen)
            connection.query_one("#port", Input).value = "x"
            await pilot.click("#next")
            await pilot.pause()
            assert app.screen is connection
            await pilot.pause(0.3)
            connection.query_one("#port", Input).value = str(_free_port())
            connection.query_one("#control", Input).value = str(_free_port())
            connection.query_one("#record", Checkbox).value = True
            record = tmp_path / "s.eculog"
            connection.query_one("#record-path", Input).value = str(record)
            await pilot.click("#next")
            await pilot.pause()
            assert isinstance(app.screen, SummaryScreen)
            assert "demo-car" in str(app.screen.query_one("#summary", Static).content)
            await pilot.click("#next")
            await pilot.pause()
            dash = app.screen
            assert isinstance(dash, DashboardScreen)
            table = cast(DataTable[str], dash.query_one("#signals", DataTable))
            for _ in range(50):
                await pilot.pause(0.2)
                if table.row_count:
                    break
            assert table.row_count == 2
            dash.query_one("#new-value", Input).value = "4000"
            await pilot.click("#set-value")
            await pilot.click("#dtc-add")
            await pilot.click("#pause")
            problems = dash.query_one("#problems", Static)
            for _ in range(25):
                await pilot.pause(0.2)
                if "P0300" in str(problems.content) and dash.live.state == "paused":
                    break
            assert "P0300" in str(problems.content)
            assert ("ecm", "rpm", 4000, False) in dash.live.signals
            process = dash.process
            assert process is not None
            await pilot.click("#next")
            await pilot.pause()
            await asyncio.wait_for(process.wait(), 10)
            await pilot.pause(0.5)
            assert "detenida" in str(dash.query_one("#banner", Static).content)
            assert "4000" in "\n".join(dash.query_one(Log).lines)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, CarsScreen)
        assert record.exists()

    asyncio.run(scenario())


def test_ble_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[bool] = [False]
    monkeypatch.setattr("ecusdk.tui.sys.platform", "darwin")
    monkeypatch.setattr("ecusdk.tui.ble_available", lambda: installed[0])
    code = "import sys; print('sin red'); sys.exit(1)"
    monkeypatch.setattr(
        "ecusdk.tui.ble_install_argv", lambda: [sys.executable, "-c", code]
    )

    async def scenario() -> None:
        app = EcusdkApp([DEMO], home=tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.click("#next")
            await pilot.pause()
            connection = app.screen
            assert isinstance(connection, ConnectionScreen)
            ble = connection.query_one("#ble", RadioButton)
            tcp = connection.query_one("#tcp", RadioButton)
            # Cancelar vuelve a Wi-Fi.
            await pilot.click("#ble")
            await _until(pilot, lambda: bool(app.screen.query("#no")))
            await pilot.click("#no")
            await _until(pilot, lambda: tcp.value)
            # Falla de instalación: aviso y vuelta a Wi-Fi.
            await pilot.click("#ble")
            await _until(pilot, lambda: bool(app.screen.query("#yes")))
            await pilot.click("#yes")
            await _until(pilot, lambda: tcp.value)
            # Instalación correcta: queda en Bluetooth.
            monkeypatch.setattr(
                "ecusdk.tui.ble_install_argv",
                lambda: [sys.executable, "-c", "pass"],
            )
            monkeypatch.setattr("ecusdk.tui.ble_available", lambda: installed[-1])
            installed.append(False)
            await pilot.click("#ble")
            await _until(pilot, lambda: bool(app.screen.query("#yes")))
            installed.append(True)
            await pilot.click("#yes")
            await _until(pilot, lambda: not connection.query_one("#next").disabled)
            await pilot.pause(0.2)
            assert ble.value
            await pilot.click("#next")
            await pilot.pause()
            assert connection.setup.ble

    asyncio.run(scenario())
