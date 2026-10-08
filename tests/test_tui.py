"""Tests del asistente ``ecusdk tui``.

Los runners de CI (sobre todo Windows) son lentos: nunca se asume que un clic
o un cambio de pantalla ya se procesó. ``_on`` espera a que una pantalla esté
activa y montada, y ``_until`` espera un estado concreto. Las acciones que se
repiten sobre el mismo botón se llaman directamente para no chocar con el
anti-rebote de ``Button``.
"""

import asyncio
import shutil
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar, cast

import pytest
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Checkbox, DataTable, Input, Log, RadioButton, Static, Tree

from ecusdk.cli import main
from ecusdk.config import load_vehicle
from ecusdk.tui import (
    CarsScreen,
    ConfirmScreen,
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
S = TypeVar("S", bound=Screen[Any])


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _until(
    pilot: Any,
    condition: Callable[[], object],
    what: str = "",
    detail: Callable[[], str] | None = None,
) -> None:
    """Espera hasta 15 s a que ``condition`` sea verdadera."""
    for _ in range(150):
        if condition():
            return
        await pilot.pause(0.1)
    extra = f"\n{detail()}" if detail else ""
    raise AssertionError(f"la UI no llegó al estado esperado: {what}{extra}")


async def _on(pilot: Any, app: EcusdkApp, kind: type[S]) -> S:
    """Espera a que la pantalla activa sea ``kind`` con todos sus widgets montados.

    Que un widget exista en el DOM no significa que haya procesado ``Mount``:
    RadioSet, por ejemplo, normaliza sus botones en ``_on_mount`` y pisa
    cualquier cambio hecho antes. En CI lento esa ventana es visible.
    """

    def ready() -> bool:
        screen = app.screen
        return isinstance(screen, kind) and all(
            node.is_mounted for node in screen.walk_children(Widget, with_self=True)
        )

    await _until(pilot, ready, f"pantalla {kind.__name__}")
    return cast(S, app.screen)


async def _click(pilot: Any, selector: str) -> None:
    """Clic que falla claro si no cae sobre el widget (layout o scroll)."""
    assert await pilot.click(selector), f"el clic no cayó en {selector}"


def _car(tmp_path: Path) -> Path:
    path = tmp_path / "car.toml"
    shutil.copyfile(DEMO, path)
    return path


def _walk(node: Any) -> list[Any]:
    return [node, *(found for child in node.children for found in _walk(child))]


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
            cars = await _on(pilot, app, CarsScreen)
            card = cars.query_one("#card", Static)
            assert "Crear" in str(card.content)
            await pilot.press("enter")
            await _on(pilot, app, CustomizeScreen)
            await _click(pilot, "#next")
            assert await _on(pilot, app, CarsScreen) is cars
            assert load_vehicle(home / "mi-auto.toml").name == "Mi auto"
            await pilot.press("c")
            await _until(pilot, (home / "mi-auto-copia.toml").exists)
            await pilot.press("d")
            await _on(pilot, app, ConfirmScreen)
            await _click(pilot, "#yes")
            await _until(pilot, lambda: not (home / "mi-auto-copia.toml").exists())
            (home / "roto.toml").write_text("[vehicle]\n")
            cars.reload(home / "roto.toml")
            await _until(pilot, lambda: "problema" in str(card.content))
            cars.action_next()  # un auto con errores no avanza
            assert app.screen is cars
            await pilot.press("escape")

    asyncio.run(scenario())


def test_customize_edits_adds_and_drops(tmp_path: Path) -> None:
    path = _car(tmp_path)

    async def scenario() -> None:
        app = EcusdkApp([path], home=tmp_path / "home")
        async with app.run_test(size=(130, 40)) as pilot:
            await _on(pilot, app, CarsScreen)
            await pilot.press("p")
            screen = await _on(pilot, app, CustomizeScreen)
            screen.query_one("#name", Input).value = "Mi demo"
            screen.query_one("#value-0", Input).value = "3000"
            await screen.add(0, "fuel_level")
            await screen.drop(1)  # speed
            assert [signal for _, signal in screen.rows] == ["rpm", "fuel_level"]
            screen.query_one("#value-0", Input).value = "abc"
            screen.action_next()
            assert app.screen is screen  # valor inválido: no guarda ni sale
            screen.query_one("#value-0", Input).value = "3000"
            screen.action_next()
            await _on(pilot, app, CarsScreen)
        vehicle = load_vehicle(path)
        ecm = vehicle.ecu("ecm")
        assert vehicle.name == "Mi demo"
        assert ecm.get_signal("rpm") == 3000
        assert "speed" not in ecm.signals and "fuel_level" in ecm.signals
        assert ecm.obd.bindings()[0x2F] == "fuel_level"

    asyncio.run(scenario())


def test_customize_example_edits_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Personalizar un ejemplo no lo reescribe: trabaja sobre una copia en home."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "examples").mkdir()
    example = tmp_path / "examples" / "demo-car.toml"
    shutil.copyfile(DEMO, example)
    original = example.read_text()
    home = tmp_path / "home"

    async def scenario() -> None:
        app = EcusdkApp([], home=home)
        async with app.run_test(size=(130, 40)) as pilot:
            await _on(pilot, app, CarsScreen)
            await pilot.press("p")
            screen = await _on(pilot, app, CustomizeScreen)
            screen.query_one("#value-0", Input).value = "1234"
            await _click(pilot, "#next")
            await _on(pilot, app, CarsScreen)

    asyncio.run(scenario())
    assert example.read_text() == original
    assert load_vehicle(home / "demo-car.toml").ecu("ecm").get_signal("rpm") == 1234


def test_advanced_editor(tmp_path: Path) -> None:
    path = _car(tmp_path)
    rpm = ("ecus", "ecm", "signals", "rpm", "initial")
    signals = ("ecus", "ecm", "signals")

    async def scenario() -> None:
        app = EcusdkApp([path], home=tmp_path / "home")
        async with app.run_test(size=(130, 40)) as pilot:
            await _on(pilot, app, CarsScreen)
            await pilot.press("p")
            await _on(pilot, app, CustomizeScreen)
            await _click(pilot, "#advanced")
            editor = await _on(pilot, app, EditorScreen)
            tree = cast(Tree[Any], editor.query_one("#tree", Tree))
            value = editor.query_one("#value", Input)

            async def select(path: tuple[Any, ...]) -> None:
                # El árbol se llena al montar y se reconstruye tras cada cambio.
                def node() -> Any:
                    return next((n for n in _walk(tree.root) if n.data == path), None)

                await _until(pilot, node)
                tree.move_cursor(node())
                await _until(pilot, lambda: editor.selected == path)

            await select(rpm)
            await _until(pilot, lambda: value.value == "850")
            value.value = "3000"
            await _click(pilot, "#apply")
            await _until(
                pilot,
                lambda: (
                    editor.document["ecus"]["ecm"]["signals"]["rpm"]["initial"] == 3000
                ),
            )
            await select(signals)
            editor.query_one("#key", Input).value = "temp"
            value.value = "{ initial = 90 }"
            await _click(pilot, "#add")
            await _until(
                pilot, lambda: "temp" in editor.document["ecus"]["ecm"]["signals"]
            )
            editor.action_next()
            ecm = load_vehicle(path).ecu("ecm")
            assert ecm.get_signal("rpm") == 3000
            assert ecm.get_signal("temp") == 90
            editor.document["ecus"]["ecm"]["can"]["request_id"] = "bad"
            editor.action_next()
            assert "no guardado" in str(editor.query_one("#status", Static).content)
            assert load_vehicle(path).ecu("ecm").request_id == 0x7E0

    asyncio.run(scenario())


def test_wizard_launches_and_controls(tmp_path: Path) -> None:
    record = tmp_path / "s.eculog"

    async def scenario() -> None:
        app = EcusdkApp([DEMO], home=tmp_path)
        async with app.run_test(size=(130, 44)) as pilot:
            await _on(pilot, app, CarsScreen)
            await _click(pilot, "#next")
            connection = await _on(pilot, app, ConnectionScreen)
            await _until(pilot, lambda: connection.query("#port"))  # en Collapsible
            assert connection.query_one("#port", Input).border_title
            connection.query_one("#port", Input).value = "x"
            connection.action_next()
            assert app.screen is connection  # puerto inválido
            connection.query_one("#port", Input).value = str(_free_port())
            connection.query_one("#control", Input).value = str(_free_port())
            connection.query_one("#record", Checkbox).value = True
            connection.query_one("#record-path", Input).value = str(record)
            connection.action_next()
            summary = await _on(pilot, app, SummaryScreen)
            assert "demo-car" in str(summary.query_one("#summary", Static).content)
            await _click(pilot, "#next")
            dash = await _on(pilot, app, DashboardScreen)
            table = cast(DataTable[str], dash.query_one("#signals", DataTable))
            await _until(
                pilot,
                lambda: table.row_count == 2,
                "dos sensores en la tabla",
                lambda: f"log={dash.log_view.lines}",
            )
            dash.query_one("#new-value", Input).value = "4000"
            await _click(pilot, "#set-value")
            await _click(pilot, "#dtc-add")
            await _click(pilot, "#pause")
            problems = dash.query_one("#problems", Static)
            await _until(
                pilot,
                lambda: (
                    "P0300" in str(problems.content)
                    and dash.live.state == "paused"
                    and ("ecm", "rpm", 4000, False) in dash.live.signals
                ),
                "set 4000, DTC P0300 y pausa reflejados en el panel",
                lambda: f"live={dash.live}\nlog={dash.log_view.lines}",
            )
            process = dash.process
            assert process is not None
            await _click(pilot, "#next")
            await asyncio.wait_for(process.wait(), 10)
            banner = dash.query_one("#banner", Static)
            await _until(pilot, lambda: "detenida" in str(banner.content))
            assert "4000" in "\n".join(dash.query_one(Log).lines)
            await pilot.press("escape")
            await _on(pilot, app, CarsScreen)

    asyncio.run(scenario())
    assert record.exists()


def test_leave_dashboard_while_running(tmp_path: Path) -> None:
    """Salir del panel con la simulación en marcha la detiene sin romper la app."""

    async def scenario() -> None:
        app = EcusdkApp([DEMO], home=tmp_path)
        app.setup.path = DEMO.resolve()
        app.setup.port, app.setup.control = _free_port(), _free_port()
        async with app.run_test(size=(130, 44)) as pilot:
            await _on(pilot, app, CarsScreen)
            app.push_screen(DashboardScreen())
            dash = await _on(pilot, app, DashboardScreen)
            await _until(pilot, lambda: dash.process is not None and dash.live.signals)
            process = dash.process
            assert process is not None
            await pilot.press("escape")
            await asyncio.wait_for(process.wait(), 10)
            await _on(pilot, app, CarsScreen)
            await pilot.pause(0.5)  # el worker termina después de salir

    asyncio.run(scenario())


def test_ble_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Elegir Bluetooth sin ``bless`` ofrece instalarlo; si falla vuelve a Wi-Fi."""
    marker = tmp_path / "bless-instalado"
    monkeypatch.setattr("ecusdk.tui.WINDOWS", False)
    monkeypatch.setattr("ecusdk.tui.ble_available", marker.exists)
    install = [[sys.executable, "-c", "import sys; print('sin red'); sys.exit(1)"]]
    monkeypatch.setattr("ecusdk.tui.ble_install_argv", lambda: install[0])

    async def scenario() -> None:
        app = EcusdkApp([DEMO], home=tmp_path)
        async with app.run_test(size=(130, 40)) as pilot:
            await _on(pilot, app, CarsScreen)
            await _click(pilot, "#next")
            connection = await _on(pilot, app, ConnectionScreen)
            ble = connection.query_one("#ble", RadioButton)
            tcp = connection.query_one("#tcp", RadioButton)
            # Cancelar vuelve a Wi-Fi.
            ble.value = True  # emite RadioSet.Changed como un clic
            await _on(pilot, app, ConfirmScreen)
            await _click(pilot, "#no")
            await _until(pilot, lambda: app.screen is connection and tcp.value)
            # La instalación falla: aviso y vuelta a Wi-Fi.
            ble.value = True  # emite RadioSet.Changed como un clic
            await _on(pilot, app, ConfirmScreen)
            await _click(pilot, "#yes")
            await _until(
                pilot,
                lambda: (
                    app.screen is connection
                    and connection.ble_ready is False
                    and tcp.value
                ),
            )
            # La instalación funciona: queda en Bluetooth.
            install[0] = [sys.executable, "-c", f"open({str(marker)!r}, 'w')"]
            ble.value = True  # emite RadioSet.Changed como un clic
            await _on(pilot, app, ConfirmScreen)
            await _click(pilot, "#yes")
            await _until(
                pilot, lambda: app.screen is connection and connection.ble_ready
            )
            assert ble.value
            connection.action_next()
            await _on(pilot, app, SummaryScreen)
            assert app.setup.ble

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform == "win32", reason="SIGABRT es de POSIX")
def test_ble_denied_by_macos_offers_wifi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS aborta el proceso si la terminal no declara Bluetooth (TCC)."""
    monkeypatch.setattr("ecusdk.tui.MACOS", True)
    abort = [sys.executable, "-c", "import os; os.abort()"]

    def argv(_: Setup) -> list[str]:
        return abort

    monkeypatch.setattr(Setup, "argv", argv)

    async def scenario() -> None:
        app = EcusdkApp([DEMO], home=tmp_path)
        app.setup.path, app.setup.ble = DEMO.resolve(), True
        async with app.run_test(size=(130, 44)) as pilot:
            await _on(pilot, app, CarsScreen)
            app.push_screen(DashboardScreen())
            dash = await _on(pilot, app, DashboardScreen)
            no_ble = dash.query_one("#no-ble")
            banner = dash.query_one("#banner", Static)
            await _until(pilot, lambda: no_ble.display)
            assert "Bluetooth" in str(banner.content)
            await _click(pilot, "#no-ble")
            await _until(pilot, lambda: "cerró sola" in str(banner.content))
            assert not app.setup.ble and not no_ble.display

    asyncio.run(scenario())
