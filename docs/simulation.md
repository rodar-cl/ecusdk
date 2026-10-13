# Simulación, escenarios y fallos

## Tiempo y determinismo

`Vehicle` usa un `Clock`. `RealClock` sigue `time.monotonic`; `VirtualClock`
sólo avanza con `advance` o `sleep`, así que los tests pueden simular minutos
de forma instantánea. `Vehicle.advance(segundos, step)` duerme el reloj por
pasos y llama a `tick` en cada uno.

`Vehicle.elapsed()` cuenta los segundos en ejecución sin las pausas. Los
generadores y los escenarios usan ese tiempo, redondeado a nanosegundos, para
que repetir una ejecución tras `reset()` dé exactamente los mismos resultados.
Toda la aleatoriedad recibe una seed: `Vehicle(seed=...)` para `frame_drop` y
`Random(seed=...)` para señales.

```python
from ecusdk import ECU, Ramp, Vehicle, VirtualCanBus, VirtualClock

clock = VirtualClock()
ecm = ECU("ecm", 0x7E0, 0x7E8, {"rpm": 850}, signal_limits={"rpm": (0, 7000)})
ecm.drive("rpm", Ramp(850, 3000, duration=10))
vehicle = Vehicle("demo", {"can": VirtualCanBus()}, [ecm], clock, seed=1)
vehicle.start()
vehicle.advance(5)
assert ecm.get_signal("rpm") == 1925
```

## Señales

| Generador | Valor en `t` segundos |
| --- | --- |
| `Constant(value)` | `value` |
| `Linear(start, rate)` | `start + rate·t` |
| `Ramp(start, end, duration, at=0)` | de `start` a `end` entre `at` y `at + duration` |
| `Sine(offset, amplitude, period, phase=0)` | `offset + amplitude·sin(2πt/period + phase)` |
| `Random(minimum, maximum, seed=0, interval=1)` | uniforme, nuevo cada `interval` |
| custom | cualquier `Callable[[float], int \| float]` |

Cada `tick` escribe el valor del generador, acotado a `min`/`max` de la señal.
`ecu.drive(nombre, None)` libera la señal. En TOML se declara con
`generator = { type = "sine", offset = 60, amplitude = 20, period = 30 }`.

## Escenarios

Un `Scenario` es una lista ordenada de `ScenarioEvent(at, ecu, action, ...)`.
`action` puede ser `set` (con `signal` y `value`), `clear_fault` o cualquier
tipo de fallo. En un fallo, `duration = 0` lo mantiene hasta `clear_fault`, y
los parámetros extra (`delay`, `probability`, `sequence`, `period`) van en
`options`, o directamente en la tabla TOML:

```toml
[scenario]
events = [
    { at = 5, ecu = "ecm", action = "set", signal = "rpm", value = 3000 },
    { at = 10, ecu = "ecm", action = "frame_drop", probability = 0.5, duration = 4 },
    { at = 20, ecu = "ecm", action = "clear_fault" },
]
```

Los eventos se aplican en el siguiente `tick`, pero la ventana de un fallo
empieza en el instante programado (`at`), no en el del tick.
`Vehicle.pause/resume` congelan escenario y generadores; `reset` vuelve a las
señales iniciales, quita los fallos y reinicia la seed y el escenario.
`Scenario.to_json/from_json` serializa los eventos.

## Fallos

`vehicle.inject_fault(kind, ecu, duration=..., **parámetros)` devuelve el
`Fault` activo; `clear_fault(ecu, kind=None)` lo retira y `faults(ecu)` lista
los vigentes.

| Tipo | Efecto | Parámetros |
| --- | --- | --- |
| `ecu_offline` | la ECU desaparece del bus | |
| `timeout` | la ECU recibe pero no responde | |
| `latency` | retrasa todos sus frames | `delay` |
| `late_flow_control` | retrasa sólo sus Flow Control | `delay` |
| `frame_drop` | pierde frames | `probability` |
| `duplicate_frame` | emite cada frame dos veces | |
| `malformed_frame` | PCI ISO-TP inválido (`0xF`) | |
| `invalid_length` | trunca el frame tras el PCI | |
| `incorrect_pid` | responde Mode 01 con otro PID | |
| `missing_consecutive_frame` | omite el CF con ese número de secuencia | `sequence` |
| `wrong_sequence_number` | numera mal ese CF | `sequence` |
| `isotp_interruption` | omite todos los CF | |
| `sensor_frozen` | congela el valor observable | `signal`, `value` opcional |
| `sensor_out_of_range` | publica un valor fuera de límites | `signal`, `value` |
| `intermittent_signal` | alterna valor real y `value` (0) cada `period` | `signal`, `period` |

Los fallos de frame actúan sobre lo que la ECU emite, antes del bus. Los de
sensor cambian lo que leen OBD y `get_signal`, sin perder el valor real que
siguen escribiendo los generadores o `set_signal`. Si el valor fuera de rango
no cabe en el codec del PID, la ECU no responde.

`exchange`, `exchange_frames` y `exchange_isotp` aceptan `timeout`. Esperan con
`clock.sleep` una respuesta retrasada o incompleta; por defecto (`0`) devuelven
lo disponible de inmediato. Una respuesta que llega tarde puede aparecer en la
siguiente transacción, como en un bus real.

## ELM327 y shell de control

Sin `ATSH` (o con `ATSH 7DF`), una consulta es funcional: se envía a cada ECU
del bus y se imprimen todas las respuestas. `ATSH 7E1` la dirige a una sola
ECU, y un dígito final (`010C1`) corta tras esa cantidad de ECUs. La espera por
ECU es `Elm327Emulator(timeout=0.2)`, el valor por defecto de `ATST`; si no hay
respuesta, se espera sin bloquear al resto de clientes. Un error ISO-TP se
informa como `CAN ERROR`.

Las respuestas siguen el formato de un adaptador real con CAN 11 bits: con
`ATH1` cada frame va en su línea con cabecera y byte PCI (`7E8 04 41 0C 0D 48`);
sin cabeceras, una respuesta de varios frames se imprime como `014` y luego
`0: …`, `1: …`. `ATS0/ATS1` quita o pone los espacios. `ATSP`/`ATDP`/`ATDPN`
fijan e informan el protocolo (el bus simulado es siempre
`ISO 15765-4 (CAN 11/500)`), `ATRV` devuelve `12.6V`, y `ATL`, `ATST`, `ATAT`,
`ATM`, `ATCAF`, `ATAL` y `ATNL` se aceptan sin efecto. Se prueba con
[python-OBD](https://github.com/brendan-w/python-OBD) como cliente real.

`TcpServer` atiende hasta 16 clientes simultáneos (`max_clients`) y corta las
líneas de más de 1024 bytes.

`VehicleShell(vehicle).execute("set ECM.rpm 3500")` interpreta los comandos de
`help`. `ecusdk run` lo sirve por TCP en `--control-port` (35001; `0` lo
desactiva), y `ecusdk get/set/dtc/fault/faults/inspect/shell` se conectan a él.
El shell no tiene autenticación: escucha en `--control-host` (por defecto
`127.0.0.1`) aunque `--host` abra el ELM327 a la red.
