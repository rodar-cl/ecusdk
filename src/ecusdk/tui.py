# pyright: reportUnknownMemberType=false
"""Asistente interactivo de ECUSDK (``ecusdk tui``, requiere ``ecusdk[tui]``).

Guía en cuatro pasos: elegir (o crear) un auto, elegir cómo se conecta la app,
revisar e iniciar, y un panel en vivo para cambiar valores y simular fallas.
``ecusdk run`` corre como subproceso y se controla por el shell de control.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import shutil
import socket
import sys
import time
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from signal import SIGABRT
from typing import Any, ClassVar, Literal, cast

import tomli_w
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    OptionList,
    RadioButton,
    RadioSet,
    Select,
    Static,
    Tree,
)
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from ecusdk.config import load_vehicle
from ecusdk.obd import PID_CATALOG
from ecusdk.shell import send_command
from ecusdk.simulation import Vehicle

HOME = Path.home() / ".ecusdk" / "vehicles"
MACOS = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"  # bless no soporta Windows
TEMPLATE = """\
[vehicle]
name = "Mi auto"
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

[ecus.ecm.signals.speed]
initial = 0

[ecus.ecm.obd]
"01:0C" = "rpm"
"01:0D" = "speed"
"""

STEPS = ("Auto", "Conexión", "Iniciar", "En marcha")
MODULES = {
    "ecm": "Motor",
    "pcm": "Motor",
    "tcm": "Transmisión",
    "abs": "Frenos ABS",
    "bcm": "Carrocería",
    "srs": "Airbags",
    "ipc": "Tablero",
}
SENSORS = {
    "rpm": "Revoluciones",
    "speed": "Velocidad",
    "engine_load": "Carga del motor",
    "coolant_temperature": "Temperatura del motor",
    "intake_manifold_pressure": "Presión de admisión",
    "intake_air_temperature": "Temperatura de admisión",
    "maf": "Flujo de aire (MAF)",
    "throttle_position": "Acelerador",
    "fuel_level": "Combustible",
    "battery_voltage": "Batería",
    "ambient_air_temperature": "Temperatura exterior",
    "oil_temperature": "Temperatura del aceite",
}
CATALOG = {codec.source: codec for codec in PID_CATALOG}

Key = str | int


def module_label(name: str) -> str:
    return f"{MODULES.get(name.lower(), 'Módulo')} ({name})"


def sensor_label(name: str) -> str:
    return SENSORS.get(name, name.replace("_", " ").capitalize())


def unit(name: str) -> str:
    codec = CATALOG.get(name)
    return codec.unit if codec else ""


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


def load(path: Path) -> Vehicle | str:
    try:
        return load_vehicle(path)
    except Exception as error:  # cualquier TOML del usuario; mostrar, no caer
        return str(error)


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


def ble_available() -> bool:
    importlib.invalidate_caches()
    return importlib.util.find_spec("bless") is not None


def ble_install_argv() -> list[str]:
    """Instala ``bless`` en el mismo entorno que ejecuta ecusdk (uv o pip)."""
    if uv := shutil.which("uv"):
        return [uv, "pip", "install", "--python", sys.executable, "bless>=0.3"]
    return [sys.executable, "-m", "pip", "install", "bless>=0.3"]


def ble_denied(code: int, ble: bool) -> bool:
    """macOS (TCC) aborta el proceso si la terminal no declara uso de Bluetooth."""
    return ble and MACOS and code == -SIGABRT


def lan_ip() -> str:
    """IP de este equipo en la red local (UDP ``connect`` no envía paquetes)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def stepper(current: int) -> Text:
    text = Text(justify="center")
    for index, name in enumerate(STEPS):
        if index:
            text.append("  ───  ", style="dim")
        if index < current:
            text.append(f"✓ {name}", style="green")
        elif index == current:
            text.append(f" {index + 1}. {name} ", style="bold reverse")
        else:
            text.append(f"{index + 1}. {name}", style="dim")
    return text


def car_card(vehicle: Vehicle) -> Text:
    text = Text()
    text.append(f"{vehicle.name}\n", style="bold")
    if vehicle.ecus and vehicle.ecus[0].vin:
        text.append(f"VIN {vehicle.ecus[0].vin}\n", style="dim")
    for ecu in vehicle.ecus:
        text.append(f"\n{module_label(ecu.name)}\n", style="bold cyan")
        for signal, value in ecu.signals.items():
            text.append(f"  {sensor_label(signal):<24}")
            text.append(f"{value:g} {unit(signal)}".rstrip())
            if signal in ecu.generators:
                text.append("   ~ cambia sola", style="dim italic")
            text.append("\n")
    if vehicle.scenario is not None:
        events = len(vehicle.scenario.events)
        text.append(f"\nGuion: {events} eventos programados\n", style="yellow")
    return text


@dataclass
class Live:
    """Estado del vehículo en marcha, leído de ``inspect``."""

    state: str
    signals: list[tuple[str, str, float, bool]]
    dtcs: list[str]
    faults: list[str]


_SIGNAL = re.compile(r"signal (\S+) = (\S+)( \(generator\))?")


def parse_inspect(text: str) -> Live:
    live, ecu = Live("", [], [], []), ""
    for raw in text.splitlines():
        line = raw.strip()
        word, _, rest = line.partition(" ")
        if word == "vehicle":
            live.state = rest.rpartition("(")[2].rstrip(")")
        elif word == "ecu":
            ecu = rest.partition(":")[0]
        elif word == "signal" and (match := _SIGNAL.fullmatch(line)):
            value = float(match[2])
            live.signals.append((ecu, match[1], value, match[3] is not None))
        elif word == "dtcs":
            live.dtcs += [f"{code} · {module_label(ecu)}" for code in rest.split()]
        elif word == "fault":
            live.faults.append(f"{rest} · {module_label(ecu)}")
    return live


@dataclass
class Setup:
    """Lo que el asistente va reuniendo paso a paso."""

    path: Path | None = None
    ble: bool = False
    share: bool = False
    port: int = 35000
    control: int = 35001
    record: str = ""
    ble_name: str = "OBDII ecusdk"

    @property
    def address(self) -> str:
        return f"{lan_ip() if self.share else '127.0.0.1'}:{self.port}"

    def argv(self) -> list[str]:
        argv = [sys.executable, "-m", "ecusdk.cli", "run", str(self.path)]
        argv += ["--host", "0.0.0.0" if self.share else "127.0.0.1"]
        argv += ["--port", str(self.port), "--control-port", str(self.control)]
        if self.record:
            argv += ["--record", self.record]
        if self.ble:
            argv += ["--ble", "--ble-name", self.ble_name]
        return argv


class ConfirmScreen(ModalScreen[bool]):
    def __init__(self, question: str, yes: str = "Sí, borrar") -> None:
        super().__init__()
        self.question = question
        self.yes = yes
        self.variant: Literal["primary", "error"] = (
            "error" if yes == "Sí, borrar" else "primary"
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.question)
            with Horizontal():
                yield Button(self.yes, id="yes", variant=self.variant)
                yield Button("Cancelar", id="no")

    @on(Button.Pressed)
    def answer(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class Step(Screen[None]):
    """Página del asistente: pasos arriba, título, contenido y navegación."""

    STEP: ClassVar[int] = 0
    HEADING: ClassVar[str] = ""
    HINT: ClassVar[str] = ""
    BACK: ClassVar[str] = "← Atrás"
    NEXT: ClassVar[str] = "Siguiente →"
    BINDINGS = [("escape", "back", "Atrás")]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(stepper(self.STEP), id="stepper")
        with Vertical(id="page"):
            if self.HEADING:
                yield Static(Text(self.HEADING), classes="heading")
            if self.HINT:
                yield Static(Text(self.HINT), classes="hint")
            yield from self.body()
        with Horizontal(id="nav"):
            yield Button(self.BACK, id="back")
            yield Static(classes="spacer")
            yield Button(self.NEXT, id="next", variant="primary")
        yield Footer()

    def body(self) -> Iterable[Any]:
        return ()

    @property
    def setup(self) -> Setup:
        return cast(EcusdkApp, self.app).setup

    @on(Button.Pressed, "#back")
    def action_back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#next")
    def next_pressed(self) -> None:
        self.action_next()

    def action_next(self) -> None:
        raise NotImplementedError


class CarsScreen(Step):
    STEP = 0
    HEADING = "¿Qué auto quieres simular?"
    HINT = "Elige uno de la lista o crea uno nuevo. A la derecha ves qué trae."
    BACK = "Salir"
    BINDINGS = [
        ("escape", "back", "Salir"),
        ("n", "new", "Nuevo"),
        ("p", "customize", "Personalizar"),
        ("c", "copy", "Duplicar"),
        ("d", "delete", "Borrar"),
    ]

    def __init__(self, extra: list[Path], home: Path) -> None:
        super().__init__()
        self.extra = extra
        self.home = home
        self.files: list[Path] = []

    def body(self) -> Iterable[Any]:
        with Horizontal():
            yield OptionList(id="cars")
            with Vertical(id="side"):
                yield Static(id="card")
                with Horizontal(classes="buttons"):
                    yield Button("Personalizar", id="customize")
                    yield Button("Duplicar", id="copy")
                    yield Button("Borrar", id="delete", variant="error")

    def on_screen_resume(self) -> None:
        self.reload(self.setup.path)

    def reload(self, select: Path | None = None) -> None:
        self.files = vehicle_files(self.extra, self.home)
        cars = self.query_one("#cars", OptionList)
        cars.clear_options()
        cars.add_option(Option(Text("+  Crear un auto nuevo", style="bold green")))
        for path in self.files:
            vehicle = load(path)
            prompt = Text()
            if isinstance(vehicle, str):
                prompt.append(path.stem, style="bold")
                prompt.append("\n   ⚠ tiene un error", style="red")
            else:
                sensors = sum(len(ecu.signals) for ecu in vehicle.ecus)
                prompt.append(vehicle.name, style="bold")
                prompt.append(
                    f"\n   {len(vehicle.ecus)} módulos · {sensors} sensores",
                    style="dim",
                )
            cars.add_option(Option(prompt))
        cars.highlighted = self.files.index(select) + 1 if select in self.files else 1
        if not self.files:
            cars.highlighted = 0
        cars.focus()

    @property
    def selected(self) -> Path | None:
        index = self.query_one("#cars", OptionList).highlighted
        return self.files[index - 1] if index else None

    @on(OptionList.OptionHighlighted)
    def show(self) -> None:
        path = self.selected
        card = self.query_one("#card", Static)
        for button in ("#customize", "#copy", "#delete"):
            self.query_one(button, Button).disabled = path is None
        self.query_one("#next", Button).disabled = path is None
        if path is None:
            card.update(
                Text.assemble(
                    ("Crear un auto nuevo\n\n", "bold"),
                    "Parte con un motor que informa revoluciones y velocidad. "
                    "Luego puedes darle nombre, cambiar valores y agregar sensores.",
                )
            )
            return
        vehicle = load(path)
        card.update(
            Text(f"Este archivo tiene un problema:\n\n{vehicle}", style="red")
            if isinstance(vehicle, str)
            else car_card(vehicle)
        )

    @on(OptionList.OptionSelected)
    def chosen(self) -> None:
        if self.selected is None:
            self.action_new()
        else:
            self.action_next()

    def action_back(self) -> None:
        self.app.exit()

    def action_next(self) -> None:
        path = self.selected
        if path is None:
            return
        if isinstance(load(path), str):
            self.notify("Corrige el auto antes de continuar.", severity="error")
            return
        self.setup.path = path
        self.app.push_screen(ConnectionScreen())

    def _free(self, stem: str) -> Path:
        self.home.mkdir(parents=True, exist_ok=True)
        path, number = self.home / f"{stem}.toml", 1
        while path.exists():
            number += 1
            path = self.home / f"{stem}-{number}.toml"
        return path

    def action_new(self) -> None:
        path = self._free("mi-auto")
        path.write_text(TEMPLATE, encoding="utf-8")
        self.setup.path = path
        self.app.push_screen(CustomizeScreen(path))

    @on(Button.Pressed, "#customize")
    def action_customize(self) -> None:
        path = self.selected
        if path is None:
            return
        if path.parent == Path("examples").resolve():
            # Los ejemplos no se reescriben: se personaliza una copia propia.
            copy = self._free(path.stem)
            shutil.copyfile(path, copy)
            self.notify(f"Los ejemplos no se modifican: editas una copia en {copy}")
            path = copy
        self.setup.path = path
        self.app.push_screen(CustomizeScreen(path))

    @on(Button.Pressed, "#copy")
    def action_copy(self) -> None:
        if self.selected is not None:
            path = self._free(f"{self.selected.stem}-copia")
            shutil.copyfile(self.selected, path)
            self.reload(path)
            self.notify(f"Copia creada en {path}")

    @on(Button.Pressed, "#delete")
    def action_delete(self) -> None:
        path = self.selected
        if path is None:
            return

        def remove(confirmed: bool | None) -> None:
            if confirmed:
                path.unlink()
                self.reload()

        self.app.push_screen(ConfirmScreen(f"¿Borrar {path.name}?"), remove)


class CustomizeScreen(Step):
    STEP = 0
    HEADING = "Personaliza tu auto"
    HINT = (
        "Cambia el nombre y los valores con que arranca cada sensor, o agrega "
        "sensores nuevos. Tu app los leerá por OBD-II."
    )
    NEXT = "Guardar ✓"
    BINDINGS = [("escape", "back", "Volver"), ("ctrl+s", "next", "Guardar")]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.document: dict[str, Any] = tomllib.loads(path.read_text("utf-8"))
        self.rows: list[tuple[str, str]] = []
        self.modules: list[str] = []

    def _ecus(self) -> dict[str, Any]:
        return cast(dict[str, Any], self.document.setdefault("ecus", {}))

    def body(self) -> Iterable[Any]:
        vehicle = cast(dict[str, Any], self.document.setdefault("vehicle", {}))
        self.rows, self.modules = [], []
        with VerticalScroll(id="form"):
            with Horizontal(classes="row"):
                yield Label("Nombre", classes="field")
                yield Input(str(vehicle.get("name", "")), id="name")
            with Horizontal(classes="row"):
                yield Label("VIN (17 caracteres)", classes="field")
                yield Input(str(vehicle.get("vin", "")), id="vin", max_length=17)
            for ecu, data in self._ecus().items():
                signals = cast(dict[str, Any], data.setdefault("signals", {}))
                yield Static(Text(module_label(ecu)), classes="section")
                for signal, definition in signals.items():
                    index = len(self.rows)
                    self.rows.append((ecu, signal))
                    with Horizontal(classes="row"):
                        yield Label(sensor_label(signal), classes="field")
                        yield Input(
                            str(definition.get("initial", 0)),
                            id=f"value-{index}",
                            type="number",
                            classes="number",
                        )
                        yield Label(unit(signal), classes="unit")
                        yield Label(
                            "~ cambia sola" if "generator" in definition else "",
                            classes="auto",
                        )
                        yield Button("Quitar", id=f"drop-{index}", classes="small")
                options = [
                    (f"{sensor_label(name)} ({codec.unit})", name)
                    for name, codec in CATALOG.items()
                    if name not in signals
                ]
                if options:
                    yield Select(
                        options,
                        prompt="+ Agregar sensor…",
                        id=f"add-{len(self.modules)}",
                        classes="add",
                    )
                self.modules.append(ecu)
        yield Button("Modo avanzado: editar todo el archivo", id="advanced")

    def sync(self) -> str | None:
        """Pasa los campos al documento; devuelve un error legible si lo hay."""
        vehicle = cast(dict[str, Any], self.document["vehicle"])
        vehicle["name"] = self.query_one("#name", Input).value.strip()
        vehicle["vin"] = self.query_one("#vin", Input).value.strip()
        if not vehicle["name"]:
            return "El auto necesita un nombre."
        for index, (ecu, signal) in enumerate(self.rows):
            raw = self.query_one(f"#value-{index}", Input).value
            try:
                value = float(raw)
            except ValueError:
                return f"{sensor_label(signal)}: «{raw}» no es un número."
            definition = self._ecus()[ecu]["signals"][signal]
            definition["initial"] = int(value) if value.is_integer() else value
        return None

    @on(Select.Changed, ".add")
    async def add_sensor(self, event: Select.Changed) -> None:
        if isinstance(event.value, str) and event.select.id is not None:
            await self.add(int(event.select.id.removeprefix("add-")), event.value)

    async def add(self, module: int, sensor: str) -> None:
        """Agrega ``sensor`` del catálogo OBD al módulo ``module``."""
        self.sync()
        codec = CATALOG[sensor]
        data = cast(dict[str, Any], self._ecus()[self.modules[module]])
        data["signals"][sensor] = {"initial": max(codec.minimum, 0)}
        data.setdefault("obd", {})[f"01:{codec.identifier:02X}"] = sensor
        await self.recompose()

    @on(Button.Pressed, ".small")
    async def drop_sensor(self, event: Button.Pressed) -> None:
        assert event.button.id is not None
        await self.drop(int(event.button.id.removeprefix("drop-")))

    async def drop(self, row: int) -> None:
        """Quita el sensor de la fila ``row`` y lo que lo referencia."""
        self.sync()
        ecu, signal = self.rows[row]
        data = cast(dict[str, Any], self._ecus()[ecu])
        del data["signals"][signal]
        obd = cast(dict[str, Any], data.get("obd", {}))
        for key in [key for key, value in obd.items() if value == signal]:
            del obd[key]
        dids = cast(dict[str, Any], data.get("uds", {}).get("dids", {}))
        for key in [
            k
            for k, v in dids.items()
            if isinstance(v, dict) and v.get("signal") == signal
        ]:
            del dids[key]
        scenario = cast(dict[str, Any], self.document.get("scenario", {}))
        if "events" in scenario:
            scenario["events"] = [
                event
                for event in cast(list[dict[str, Any]], scenario["events"])
                if not (event.get("ecu") == ecu and event.get("signal") == signal)
            ]
        await self.recompose()

    @on(Button.Pressed, "#advanced")
    def advanced(self) -> None:
        if self.sync() is None:  # conserva lo editado si es válido
            save_validated(self.path, self.document)
        self.app.switch_screen(EditorScreen(self.path))

    def action_next(self) -> None:
        error = self.sync() or save_validated(self.path, self.document)
        if error:
            self.notify(f"No se pudo guardar: {error}", severity="error", timeout=8)
            return
        self.notify("Auto guardado ✓")
        self.app.pop_screen()


class EditorScreen(Step):
    """Modo avanzado: cualquier tabla o valor del TOML como árbol."""

    STEP = 0
    HEADING = "Modo avanzado"
    HINT = (
        'Edita cualquier valor del archivo. Escríbelo como en TOML: 850, 0x7E0, "rpm".'
    )
    NEXT = "Guardar ✓"
    BINDINGS = [("escape", "back", "Volver"), ("ctrl+s", "next", "Guardar")]

    def __init__(self, path: Path) -> None:
        super().__init__()
        self.path = path
        self.document: dict[str, Any] = tomllib.loads(path.read_text("utf-8"))
        self.selected: tuple[Key, ...] = ()

    def body(self) -> Iterable[Any]:
        with Horizontal():
            yield Tree[tuple[Key, ...]](self.path.name, data=(), id="tree")
            with Vertical(id="tree-form"):
                yield Label("", id="where")
                yield Input(placeholder="valor", id="value")
                yield Input(placeholder="clave nueva (para Añadir)", id="key")
                with Horizontal(classes="buttons"):
                    yield Button("Aplicar", id="apply")
                    yield Button("Añadir", id="add")
                    yield Button("Quitar", id="remove", variant="error")
                yield Static(id="status")

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
        value = parse_value(self.query_one("#value", Input).value)
        self._get(tuple(parent))[key] = value
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

    def action_next(self) -> None:
        error = save_validated(self.path, self.document)
        if error:
            self._status(f"no guardado: {error}")
            self.notify(f"No se pudo guardar: {error}", severity="error", timeout=8)
        else:
            self._status(f"guardado {self.path}")
            self.notify("Auto guardado ✓")


class ConnectionScreen(Step):
    STEP = 1
    HEADING = "¿Cómo se conecta tu app?"
    HINT = "Tu app (o escáner) verá al auto simulado como un adaptador OBD-II ELM327."
    ble_ready: bool | None = None  # resultado de la última instalación de bless
    MODE_HELP: ClassVar[dict[str, str]] = {
        "tcp": (
            "Como un adaptador ELM327 Wi-Fi. Sirve para Torque, Car Scanner o tu "
            "propia app en este computador. Para usarlo desde el teléfono, "
            "activa la opción de red de abajo."
        ),
        "ble": (
            "Se anuncia como un adaptador OBDII Bluetooth. Necesitas un teléfono "
            "físico: los simuladores de iOS y Android no tienen Bluetooth. En "
            "macOS, da permiso de Bluetooth a tu terminal."
        ),
    }

    def body(self) -> Iterable[Any]:
        setup = self.setup
        with VerticalScroll():
            with RadioSet(id="mode"):
                yield RadioButton("Wi-Fi / red local", value=not setup.ble, id="tcp")
                yield RadioButton(
                    "Bluetooth (BLE) — no disponible en Windows"
                    if WINDOWS
                    else "Bluetooth (BLE)",
                    value=setup.ble,
                    id="ble",
                    disabled=WINDOWS,
                )
            yield Static(
                Text(self.MODE_HELP["ble" if setup.ble else "tcp"]), id="mode-help"
            )
            yield Checkbox(
                "Permitir conexiones desde otros dispositivos de mi red (mi teléfono)",
                setup.share,
                id="share",
            )
            yield Checkbox(
                "Grabar la sesión para revisarla después",
                bool(setup.record),
                id="record",
            )
            with Collapsible(title="Opciones avanzadas", id="advanced"):
                with Horizontal(classes="row"):
                    yield Input(str(setup.port), id="port", type="integer")
                    yield Input(str(setup.control), id="control", type="integer")
                    yield Input(
                        setup.record, placeholder="sesion.eculog", id="record-path"
                    )
                    yield Input(setup.ble_name, id="ble-name")

    def on_mount(self) -> None:
        for widget, title in (
            ("port", "Puerto de la app"),
            ("control", "Puerto de control"),
            ("record-path", "Archivo de grabación"),
            ("ble-name", "Nombre Bluetooth"),
        ):
            self.query_one(f"#{widget}", Input).border_title = title

    @on(RadioSet.Changed)
    def mode_changed(self, event: RadioSet.Changed) -> None:
        mode = "ble" if event.pressed.id == "ble" else "tcp"
        self.query_one("#mode-help", Static).update(Text(self.MODE_HELP[mode]))
        if mode == "ble" and not ble_available():

            def answer(install: bool | None) -> None:
                if install:
                    self.install_ble()
                else:
                    self.query_one("#tcp", RadioButton).value = True

            self.app.push_screen(
                ConfirmScreen(
                    "Bluetooth necesita un componente extra (bless). "
                    "¿Lo instalo ahora?",
                    yes="Sí, instalar",
                ),
                answer,
            )

    @work(exclusive=True, group="install")
    async def install_ble(self) -> None:
        help_text = self.query_one("#mode-help", Static)
        next_button = self.query_one("#next", Button)
        help_text.update(Text("Instalando soporte Bluetooth… (puede tardar un poco)"))
        next_button.disabled = True
        self.ble_ready = None
        try:
            process = await asyncio.create_subprocess_exec(
                *ble_install_argv(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            output, _ = await process.communicate()
            ok = process.returncode == 0 and ble_available()
        except OSError as error:
            output, ok = str(error).encode(), False
        next_button.disabled = False
        self.ble_ready = ok
        if ok:
            help_text.update(Text(self.MODE_HELP["ble"]))
            self.notify("Bluetooth listo ✓")
            return
        self.query_one("#tcp", RadioButton).value = True
        detail = output.decode(errors="replace").strip().splitlines()[-1:] or [""]
        self.notify(
            f"No se pudo instalar Bluetooth: {detail[0]}\n"
            "Prueba a mano: pip install 'ecusdk[ble]'",
            severity="error",
            timeout=12,
        )

    def action_next(self) -> None:
        setup = self.setup
        try:
            port = int(self.query_one("#port", Input).value)
            control = int(self.query_one("#control", Input).value)
        except ValueError:
            self.notify("Los puertos deben ser números.", severity="error")
            return
        setup.port, setup.control = port, control
        setup.ble = self.query_one("#ble", RadioButton).value
        setup.share = self.query_one("#share", Checkbox).value
        setup.ble_name = (
            self.query_one("#ble-name", Input).value.strip() or "OBDII ecusdk"
        )
        setup.record = ""
        if self.query_one("#record", Checkbox).value:
            assert setup.path is not None
            setup.record = self.query_one("#record-path", Input).value.strip() or (
                f"{setup.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.eculog"
            )
        self.app.push_screen(SummaryScreen())


class SummaryScreen(Step):
    STEP = 2
    HEADING = "Todo listo"
    HINT = "Revisa la configuración y arranca la simulación."
    NEXT = "▶ Iniciar simulación"

    def body(self) -> Iterable[Any]:
        setup = self.setup
        assert setup.path is not None
        vehicle = load_vehicle(setup.path)
        rows = [
            ("Auto", vehicle.name),
            ("Conexión", "Bluetooth + Wi-Fi" if setup.ble else "Wi-Fi / red local"),
            ("Dirección para tu app", setup.address),
        ]
        if setup.ble:
            rows.append(("Nombre Bluetooth", setup.ble_name))
        rows.append(("Grabación", setup.record or "no"))
        text = Text()
        for label, value in rows:
            text.append(f"{label:<24}", style="dim")
            text.append(f"{value}\n", style="bold")
        text.append(
            "\nCuando arranque, conecta tu app a esa dirección. En el panel podrás "
            "cambiar valores y simular fallas en vivo.",
            style="italic",
        )
        yield Static(text, id="summary")

    def action_next(self) -> None:
        self.app.push_screen(DashboardScreen())


class DashboardScreen(Step):
    STEP = 3
    HEADING = ""
    BACK = "← Inicio"
    NEXT = "■ Detener"
    BINDINGS = [("escape", "back", "Inicio")]

    def __init__(self) -> None:
        super().__init__()
        self.process: asyncio.subprocess.Process | None = None
        self.stopping = False
        self.gone = False
        self.keys: list[str] = []
        self.live = Live("", [], [], [])

    def body(self) -> Iterable[Any]:
        assert self.setup.path is not None
        modules = [
            (module_label(ecu.name), ecu.name)
            for ecu in load_vehicle(self.setup.path).ecus
        ]
        yield Static(Text("Iniciando simulación…"), id="banner")
        yield Button(
            "Seguir sin Bluetooth (sólo Wi-Fi)", id="no-ble", variant="warning"
        )
        with Horizontal(id="dash"):
            with Vertical(id="sensors"):
                yield Static("Sensores en vivo", classes="section")
                yield DataTable(id="signals", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="row"):
                    yield Input(
                        placeholder="Elige un sensor y escribe su nuevo valor",
                        id="new-value",
                        type="number",
                    )
                    yield Button("Aplicar", id="set-value", variant="primary")
            with VerticalScroll(id="actions"):
                yield Static("Simular en", classes="section")
                yield Select(
                    modules, value=modules[0][1], allow_blank=False, id="module"
                )
                yield Static(id="problems")
                yield Button("⚠ Encender check engine", id="dtc-add", variant="warning")
                yield Button("✓ Borrar códigos de falla", id="dtc-clear")
                yield Button("Desconectar módulo 5 s", id="offline")
                yield Button("Respuesta lenta 10 s", id="latency")
                with Horizontal(classes="buttons"):
                    yield Button("⏸ Pausar", id="pause")
                    yield Button("↺ Reiniciar", id="reset")
        with Collapsible(title="Registro técnico", id="techlog"):
            yield Log(id="log")
            yield Input(placeholder="Comando avanzado (escribe help)", id="cmd")

    def on_mount(self) -> None:
        table = self._table()
        table.add_column("Módulo", key="module")
        table.add_column("Sensor", key="sensor")
        table.add_column("Valor", key="value")
        table.add_column("", key="auto")
        self.launch()
        self.set_interval(1, self.poll)

    @work(exclusive=True, group="process")
    async def launch(self) -> None:
        log = self.query_one(Log)
        self.stopping = False
        self._banner(Text("Iniciando simulación…"), "starting")
        self.query_one("#next", Button).label = "■ Detener"
        process = await asyncio.create_subprocess_exec(
            *self.setup.argv(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.process = process
        assert process.stdout is not None
        async for line in process.stdout:
            log.write_line(line.decode(errors="replace").rstrip())
        code = await process.wait()
        self.process = None
        if self.gone:  # se salió del panel mientras se detenía
            return
        log.write_line(f"proceso terminado ({code})")
        self.query_one("#next", Button).label = "▶ Iniciar de nuevo"
        if self.stopping:
            self._banner(Text("■ Simulación detenida"), "stopped")
        elif ble_denied(code, self.setup.ble):
            terminal = os.environ.get("TERM_PROGRAM", "tu terminal")
            self._banner(Text(f"✗ macOS no deja a {terminal} usar Bluetooth"), "failed")
            self.query_one("#no-ble", Button).display = True
            self.notify(
                f"macOS cerró la simulación porque {terminal} no declara permiso "
                "de Bluetooth. Abre ecusdk tui desde Terminal o iTerm y acepta el "
                "permiso, o sigue sin Bluetooth.",
                severity="error",
                timeout=20,
            )
        else:
            self._banner(
                Text("✗ La simulación se cerró sola. Revisa el registro técnico."),
                "failed",
            )
            self.query_one("#techlog", Collapsible).collapsed = False

    @on(Button.Pressed, "#no-ble")
    def without_ble(self) -> None:
        self.setup.ble = False
        self.query_one("#no-ble", Button).display = False
        self.launch()

    def _banner(self, text: Text, state: str) -> None:
        banner = self.query_one("#banner", Static)
        banner.update(text)
        banner.set_classes(state)

    def stop(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.stopping = True
            # SIGTERM: ``ecusdk run`` cierra servidores y grabación en orden.
            self.process.terminate()

    def action_next(self) -> None:
        if self.process is None:
            self.launch()
        else:
            self.stop()

    def action_back(self) -> None:
        self.gone = True  # antes de detener: el worker puede terminar primero
        self.stop()
        cast(EcusdkApp, self.app).home_screen()

    def on_unmount(self) -> None:
        self.gone = True
        self.stop()

    def poll(self) -> None:
        if self.process is not None:
            self.inspect(self.setup.control)

    @work(thread=True, exclusive=True, group="inspect")
    def inspect(self, port: int) -> None:
        try:
            text = send_command("127.0.0.1", port, "inspect", timeout=1.0)
        except OSError:
            return
        self.app.call_from_thread(self.show, parse_inspect(text))

    def show(self, live: Live) -> None:
        if self.gone:  # respuesta tardía de un hilo
            return
        self.live = live
        setup = self.setup
        paused = live.state == "paused"
        banner = Text("⏸ En pausa" if paused else "● En marcha", style="bold")
        banner.append(f"   ·   conecta tu app a {setup.address}")
        if setup.ble:
            banner.append(f"   ·   Bluetooth «{setup.ble_name}»")
        if setup.record:
            banner.append(f"   ·   grabando en {setup.record}")
        self._banner(banner, "paused" if paused else "running")
        self.query_one("#pause", Button).label = "▶ Reanudar" if paused else "⏸ Pausar"
        table = self._table()
        keys = [f"{ecu}.{signal}" for ecu, signal, _, _ in live.signals]
        values = [
            f"{value:g} {unit(signal)}".rstrip() for _, signal, value, _ in live.signals
        ]
        if keys != self.keys:
            table.clear()
            for key, (ecu, signal, _, auto), value in zip(
                keys, live.signals, values, strict=True
            ):
                table.add_row(
                    module_label(ecu),
                    sensor_label(signal),
                    value,
                    "~ cambia sola" if auto else "",
                    key=key,
                )
            self.keys = keys
        else:
            for key, value in zip(keys, values, strict=True):
                table.update_cell(key, "value", value)
        problems = Text()
        for code in live.dtcs:
            problems.append(f"⚠ Código {code}\n", style="yellow")
        for fault in live.faults:
            problems.append(f"✗ Falla {fault}\n", style="red")
        self.query_one("#problems", Static).update(
            problems or Text("Sin códigos ni fallas ✓", style="green")
        )

    def _table(self) -> DataTable[str]:
        return cast(DataTable[str], self.query_one("#signals", DataTable))

    def _module(self) -> str:
        return str(cast(Select[str], self.query_one("#module", Select)).value)

    @on(Button.Pressed, "#set-value")
    @on(Input.Submitted, "#new-value")
    def set_value(self) -> None:
        table = self._table()
        raw = self.query_one("#new-value", Input).value.strip()
        if not self.keys or not raw:
            self.notify(
                "Elige un sensor en la tabla y escribe un valor.", severity="warning"
            )
            return
        self.command(f"set {self.keys[table.cursor_row]} {raw}")
        self.query_one("#new-value", Input).value = ""

    @on(Button.Pressed, "#actions Button")
    def quick_action(self, event: Button.Pressed) -> None:
        module = self._module()
        line = {
            "dtc-add": f"dtc {module} add P0300",
            "dtc-clear": f"dtc {module} clear",
            "offline": f"fault {module} offline 5s",
            "latency": f"fault {module} latency 10s delay=0.3",
            "pause": "resume" if self.live.state == "paused" else "pause",
            "reset": "reset",
        }[str(event.button.id)]
        self.command(line)

    @on(Input.Submitted, "#cmd")
    def typed(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.command(event.value.strip())
            event.input.value = ""

    def command(self, line: str) -> None:
        if self.process is None:
            self.notify("La simulación no está en marcha.", severity="warning")
            return
        self.send(line, self.setup.control)

    @work(thread=True)
    def send(self, line: str, port: int) -> None:
        try:
            response = send_command("127.0.0.1", port, line, timeout=1.0)
        except OSError as error:
            response = f"ERROR: sin conexión: {error}"
        self.app.call_from_thread(self.answered, line, response)

    def answered(self, line: str, response: str) -> None:
        if self.gone:
            return
        self.query_one(Log).write_line(f"> {line}\n{response}")
        if response.startswith("ERROR"):
            self.notify(response.removeprefix("ERROR: "), severity="error")
        self.poll()


class EcusdkApp(App[None]):
    TITLE = "ECUSDK"
    ENABLE_COMMAND_PALETTE = False
    SUB_TITLE = "simulador de autos"
    CSS = """
    #stepper { height: 3; content-align: center middle; background: $boost; }
    #page { padding: 1 3 0 3; }
    .heading { text-style: bold; color: $accent; }
    .hint { color: $text-muted; margin-bottom: 1; }
    .section { text-style: bold; color: $secondary; margin-top: 1; }
    #nav { height: auto; padding: 0 3 1 3; }
    .spacer { width: 1fr; }
    .buttons, .row { height: auto; }
    #cars { width: 42; height: 1fr; }
    #side { width: 1fr; padding-left: 2; }
    #card { height: 1fr; border: round $primary; padding: 1 2; }
    #form { height: 1fr; }
    .field { width: 26; padding: 1 0 0 1; }
    .number { width: 16; }
    .unit { width: 6; padding: 1 1 0 1; color: $text-muted; }
    .auto { width: 16; padding: 1 0 0 0; color: $text-muted; text-style: italic; }
    .small { min-width: 8; }
    .add { width: 50; margin-left: 1; }
    #name, #vin { width: 50; }
    #mode-help { color: $text-muted; padding: 0 0 1 2; }
    #advanced Input { width: 1fr; }
    #summary { border: round $success; padding: 1 2; height: auto; }
    #banner { height: 3; content-align: center middle; text-style: bold; }
    #banner.running { background: $success 25%; }
    #banner.paused { background: $warning 25%; }
    #banner.starting { background: $boost; }
    #banner.stopped, #banner.failed { background: $error 25%; }
    #dash { height: 1fr; }
    #no-ble { display: none; width: 100%; }
    #sensors { width: 1fr; }
    #signals { height: 1fr; }
    #new-value { width: 1fr; }
    #actions { width: 38; padding-left: 2; }
    #actions Button, #actions Select { width: 1fr; }
    #actions > Button { width: 100%; }
    #problems { padding: 1 0; }
    #techlog { height: auto; max-height: 16; }
    #log { height: 8; }
    #tree { width: 1fr; }
    #tree-form { width: 1fr; padding-left: 2; }
    ToastRack { align: right top; margin-top: 4; }
    ConfirmScreen { align: center middle; }
    #dialog { width: 60; height: auto; border: thick $error; padding: 1 2; }
    #dialog Horizontal { height: auto; margin-top: 1; }
    """

    def __init__(self, extra: list[Path] | None = None, home: Path = HOME) -> None:
        super().__init__()
        self.extra = extra or []
        self.home = home
        self.setup = Setup()

    def on_mount(self) -> None:
        self.theme = "tokyo-night"
        self.push_screen(CarsScreen(self.extra, self.home))

    def home_screen(self) -> None:
        while len(self.screen_stack) > 2:
            self.pop_screen()


def run_tui(paths: list[str]) -> None:
    EcusdkApp([Path(path) for path in paths]).run()
