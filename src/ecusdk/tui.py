# pyright: reportUnknownMemberType=false
"""Launcher interactivo de ECUSDK (``ecusdk tui``, requiere ``ecusdk[tui]``).

Elige un vehículo TOML, lo edita como árbol, configura la conexión y lanza
``ecusdk run`` como subproceso, controlándolo por el shell de control.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

import tomli_w
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Log,
    Static,
    Tree,
)
from textual.widgets.tree import TreeNode

from ecusdk.config import load_vehicle
from ecusdk.shell import describe, send_command

HOME = Path.home() / ".ecusdk" / "vehicles"
TEMPLATE = """\
[vehicle]
name = "nuevo-auto"
vin = "ECUSDK00000000001"

[buses.powertrain]
type = "can"

[ecus.ecm]
bus = "powertrain"

[ecus.ecm.can]
request_id = 0x7E0
response_id = 0x7E8

[ecus.ecm.signals.rpm]
initial = 850

[ecus.ecm.obd]
"01:0C" = "rpm"
"""

Key = str | int


def vehicle_files(extra: list[Path], home: Path) -> list[Path]:
    """TOMLs de ``extra``, ``./examples`` y ``home``, sin duplicados."""
    found = list(extra)
    for folder in (Path("examples"), home):
        if folder.is_dir():
            found += sorted(folder.glob("*.toml"))
    return list(dict.fromkeys(path.resolve() for path in found))


def parse_value(text: str) -> Any:
    """Interpreta un valor TOML (``0x7E0``, ``"rpm"``, ``[1, 2]``); si no, texto."""
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


def nested(value: Any) -> bool:
    """Tablas y listas de tablas se muestran como ramas; lo demás es un valor."""
    return isinstance(value, dict) or (
        isinstance(value, list)
        and any(isinstance(item, dict) for item in cast(list[Any], value))
    )


def preview(path: Path) -> str:
    try:
        return describe(load_vehicle(path))
    except Exception as error:  # cualquier TOML del usuario; mostrar, no caer
        return f"error: {error}"


def save_validated(path: Path, document: dict[str, Any]) -> str | None:
    """Guarda ``document`` sólo si ``load_vehicle`` lo acepta; devuelve el error."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(tomli_w.dumps(document), encoding="utf-8")
    try:
        load_vehicle(temporary)
    except Exception as error:  # load_vehicle aún deja pasar TypeError
        temporary.unlink()
        return str(error)
    os.replace(temporary, path)
    return None


class ConfirmScreen(ModalScreen[bool]):
    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question)
            with Horizontal():
                yield Button("Sí", id="yes", variant="error")
                yield Button("No", id="no")

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class VehiclesScreen(Screen[None]):
    BINDINGS = [
        ("l", "launch", "Lanzar"),
        ("e", "edit", "Editar"),
        ("n", "new", "Nuevo"),
        ("c", "copy", "Duplicar"),
        ("d", "delete", "Borrar"),
        ("q", "app.quit", "Salir"),
    ]

    def __init__(self, extra: list[Path], home: Path) -> None:
        super().__init__()
        self.extra = extra
        self.home = home
        self.files: list[Path] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield ListView(id="files")
            yield Static(id="preview")
        yield Footer()

    def on_screen_resume(self) -> None:
        self.reload()

    def reload(self, select: Path | None = None) -> None:
        self.files = vehicle_files(self.extra, self.home)
        files = self.query_one("#files", ListView)
        files.clear()
        files.extend(ListItem(Label(path.name)) for path in self.files)
        if self.files:
            files.index = self.files.index(select) if select in self.files else 0
        else:
            self.query_one("#preview", Static).update("sin vehículos: pulsa n")

    @property
    def selected(self) -> Path | None:
        index = self.query_one("#files", ListView).index
        return self.files[index] if index is not None and self.files else None

    @on(ListView.Highlighted)
    def show(self) -> None:
        if self.selected is not None:
            self.query_one("#preview", Static).update(Text(preview(self.selected)))

    @on(ListView.Selected)
    def action_launch(self) -> None:
        if self.selected is not None:
            self.app.push_screen(RunScreen(self.selected))

    def action_edit(self) -> None:
        if self.selected is not None:
            self.app.push_screen(EditorScreen(self.selected))

    def _free(self, stem: str) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        path, number = self.home / f"{stem}.toml", 1
        while path.exists():
            number += 1
            path = self.home / f"{stem}-{number}.toml"
        return path

    def action_new(self) -> None:
        path = self._free("nuevo-auto")
        path.write_text(TEMPLATE, encoding="utf-8")
        self.app.push_screen(EditorScreen(path))

    def action_copy(self) -> None:
        if self.selected is not None:
            path = self._free(f"{self.selected.stem}-copia")
            shutil.copyfile(self.selected, path)
            self.reload(path)

    def action_delete(self) -> None:
        path = self.selected
        if path is None:
            return

        def remove(confirmed: bool | None) -> None:
            if confirmed:
                path.unlink()
                self.reload()

        self.app.push_screen(ConfirmScreen(f"¿Borrar {path}?"), remove)


class EditorScreen(Screen[None]):
    """Editor en árbol: cualquier tabla o valor del TOML del vehículo."""

    BINDINGS = [("ctrl+s", "save", "Guardar"), ("escape", "back", "Volver")]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.document: dict[str, Any] = tomllib.loads(path.read_text("utf-8"))
        self.selected: tuple[Key, ...] = ()

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Tree[tuple[Key, ...]](self.path.name, data=(), id="tree")
            with Vertical(id="form"):
                yield Label("", id="where")
                yield Input(placeholder='valor TOML: 850, 0x7E0, "rpm"', id="value")
                yield Input(placeholder="clave nueva (para Añadir)", id="key")
                with Horizontal():
                    yield Button("Aplicar", id="apply")
                    yield Button("Añadir", id="add")
                    yield Button("Borrar", id="remove", variant="error")
                    yield Button("Guardar", id="save", variant="primary")
                yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.rebuild()

    def rebuild(self) -> None:
        tree = cast(Tree[tuple[Key, ...]], self.query_one("#tree", Tree))
        tree.clear()
        self._fill(tree.root, self.document, ())
        tree.root.expand_all()

    def _fill(
        self, node: TreeNode[tuple[Key, ...]], value: Any, path: tuple[Key, ...]
    ) -> None:
        items: list[tuple[Key, Any]] = (
            list(cast(dict[str, Any], value).items())
            if isinstance(value, dict)
            else list(enumerate(cast(list[Any], value)))
        )
        for key, child in items:
            here = (*path, key)
            if nested(child):
                self._fill(node.add(str(key), data=here), child, here)
            else:
                label = Text(f"{key} = {json.dumps(child, ensure_ascii=False)}")
                node.add_leaf(label, data=here)

    def _get(self, path: tuple[Key, ...]) -> Any:
        value: Any = self.document
        for key in path:
            value = value[key]
        return value

    @on(Tree.NodeHighlighted)
    def pick(self, event: Tree.NodeHighlighted[tuple[Key, ...]]) -> None:
        self.selected = event.node.data or ()
        value = self._get(self.selected)
        where = ".".join(str(key) for key in self.selected) or "(raíz)"
        self.query_one("#where", Label).update(Text(where))
        self.query_one("#value", Input).value = (
            "" if nested(value) else json.dumps(value, ensure_ascii=False)
        )

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text))

    @on(Button.Pressed, "#apply")
    @on(Input.Submitted, "#value")
    def apply(self) -> None:
        if not self.selected or nested(self._get(self.selected)):
            self._status("elige un valor para editar")
            return
        *parent, key = self.selected
        self._get(tuple(parent))[key] = parse_value(
            self.query_one("#value", Input).value
        )
        self.rebuild()

    @on(Button.Pressed, "#add")
    @on(Input.Submitted, "#key")
    def add(self) -> None:
        target = self._get(self.selected)
        if not nested(target) and self.selected:
            target = self._get(self.selected[:-1])
        raw = self.query_one("#value", Input).value
        value: Any = parse_value(raw) if raw else {}
        if isinstance(target, list):
            cast(list[Any], target).append(value)
        else:
            key = self.query_one("#key", Input).value.strip()
            if not key:
                self._status("indica la clave nueva")
                return
            cast(dict[str, Any], target)[key] = value
        self.query_one("#key", Input).value = ""
        self.rebuild()

    @on(Button.Pressed, "#remove")
    def delete_node(self) -> None:
        if self.selected:
            *parent, key = self.selected
            del self._get(tuple(parent))[key]
            self.selected = ()
            self.rebuild()

    @on(Button.Pressed, "#save")
    def action_save(self) -> None:
        error = save_validated(self.path, self.document)
        self._status(f"no guardado: {error}" if error else f"guardado {self.path}")

    def action_back(self) -> None:
        self.app.pop_screen()


class RunScreen(Screen[None]):
    """Conexión (TCP, control, BLE, grabación) y vehículo en ejecución."""

    BINDINGS = [("escape", "back", "Volver")]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.process: asyncio.subprocess.Process | None = None

    def compose(self) -> ComposeResult:
        ble = importlib.util.find_spec("bless") is not None
        yield Header()
        with Horizontal(classes="connection"):
            yield Input("127.0.0.1", id="host")
            yield Input("35000", id="port", type="integer")
            yield Input("35001", id="control", type="integer")
            yield Button("Lanzar", id="toggle", variant="success")
        with Horizontal(classes="connection"):
            yield Input(placeholder="vacío: sin grabar", id="record")
            yield Checkbox(
                "BLE" if ble else Text("BLE (instala ecusdk[ble])"),
                id="ble",
                disabled=not ble,
            )
            yield Input("OBDII ecusdk", id="ble_name")
        with Horizontal():
            yield Log(id="log")
            yield Static(id="live")
        yield Input(
            placeholder="comando: set ecm.rpm 3000, fault ecm offline 2s…", id="cmd"
        )
        with Horizontal(id="controls"):
            for command in ("pause", "resume", "reset"):
                yield Button(command, id=command)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.path.name
        for widget, title in (
            ("host", "host"),
            ("port", "puerto ELM327 TCP"),
            ("control", "puerto de control"),
            ("record", "grabar .eculog"),
            ("ble_name", "nombre BLE"),
        ):
            self.query_one(f"#{widget}", Input).border_title = title
        self.set_interval(1, self.refresh_live)

    def _value(self, widget: str) -> str:
        return self.query_one(f"#{widget}", Input).value.strip()

    def argv(self) -> list[str]:
        argv = [sys.executable, "-m", "ecusdk.cli", "run", str(self.path)]
        argv += ["--host", self._value("host"), "--port", self._value("port")]
        argv += ["--control-port", self._value("control")]
        if self._value("record"):
            argv += ["--record", self._value("record")]
        if self.query_one("#ble", Checkbox).value:
            argv += ["--ble", "--ble-name", self._value("ble_name")]
        return argv

    @on(Button.Pressed, "#toggle")
    def toggle(self) -> None:
        if self.process is None:
            self.launch()
        else:
            self.stop()

    @work(exclusive=True, group="process")
    async def launch(self) -> None:
        log = self.query_one(Log)
        button = self.query_one("#toggle", Button)
        process = await asyncio.create_subprocess_exec(
            *self.argv(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.process = process
        button.label, button.variant = "Detener", "error"
        assert process.stdout is not None
        async for line in process.stdout:
            log.write_line(line.decode(errors="replace").rstrip())
        log.write_line(f"proceso terminado ({await process.wait()})")
        self.process = None
        button.label, button.variant = "Lanzar", "success"

    def stop(self) -> None:
        if self.process is not None and self.process.returncode is None:
            # SIGTERM: ``ecusdk run`` cierra servidores y grabación en orden.
            self.process.terminate()

    def refresh_live(self) -> None:
        if self.process is not None:
            self.remote("inspect", self._value("host"), int(self._value("control")))

    @on(Input.Submitted, "#cmd")
    def command(self, event: Input.Submitted) -> None:
        if event.value.strip() and self.process is not None:
            self.remote(
                event.value.strip(), self._value("host"), int(self._value("control"))
            )
            event.input.value = ""

    @on(Button.Pressed, "#pause, #resume, #reset")
    def control(self, event: Button.Pressed) -> None:
        if self.process is not None and event.button.id is not None:
            self.remote(
                event.button.id, self._value("host"), int(self._value("control"))
            )

    @work(thread=True)
    def remote(self, line: str, host: str, port: int) -> None:
        try:
            response = send_command(host, port, line, timeout=1.0)
        except OSError as error:
            response = f"sin shell de control: {error}"
        if line == "inspect":
            self.app.call_from_thread(
                self.query_one("#live", Static).update, Text(response)
            )
        else:
            self.app.call_from_thread(
                self.query_one(Log).write_line, f"> {line}\n{response}"
            )

    def action_back(self) -> None:
        self.stop()
        self.app.pop_screen()

    def on_unmount(self) -> None:
        self.stop()


class EcusdkApp(App[None]):
    TITLE = "ECUSDK"
    CSS = """
    #files { width: 32; }
    #preview, #live { padding: 0 1; }
    #tree { width: 1fr; }
    #form { width: 1fr; padding: 0 1; }
    #form Horizontal, #controls, .connection { height: auto; }
    #form Button { width: 1fr; min-width: 6; }
    .connection Input { width: 1fr; }
    .connection Button { width: 16; margin-top: 1; }
    .connection Checkbox { margin-top: 1; }
    #log { width: 1fr; }
    #live { width: 1fr; border-left: solid $primary; }
    ConfirmScreen { align: center middle; }
    #dialog { width: 60; height: auto; border: thick $error; padding: 1; }
    #dialog Horizontal { height: auto; }
    """

    def __init__(self, extra: list[Path] | None = None, home: Path = HOME) -> None:
        super().__init__()
        self.extra = extra or []
        self.home = home

    def on_mount(self) -> None:
        self.push_screen(VehiclesScreen(self.extra, self.home))


def run_tui(paths: list[str]) -> None:
    EcusdkApp([Path(path) for path in paths]).run()
