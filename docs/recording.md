# Grabación y reproducción (`.eculog` v1)

## Grabar

```python
from ecusdk import Recorder, load_vehicle

vehicle = load_vehicle("examples/demo-car.toml")
with Recorder(vehicle, "session.eculog"):
    vehicle.start()
    ...  # consultas, cambios de señales, fallos, escenarios
    vehicle.stop()
```

```bash
ecusdk run examples/demo-car.toml --record session.eculog
```

`Recorder` se engancha a `vehicle.recorder`: un vehículo admite un recorder a
la vez. Captura tras cada `tick` y cada cambio de lifecycle, y al inyectar o
retirar fallos. Un cambio de señal o DTC hecho entre ticks (por API o shell)
queda registrado con el instante del siguiente `tick`. Con `VirtualClock`, la
misma sesión produce el mismo archivo byte a byte.

Los frames se graban en los buses virtuales. Un bus SocketCAN no se graba a
nivel de frame, pero sí sus cambios de estado.

## Formato

JSON Lines en UTF-8, un objeto por línea. La primera línea es la cabecera:

```json
{"format":"eculog","version":1,"vehicle":"demo-car","seed":0,
 "buses":["powertrain"],
 "ecus":[{"name":"ecm","request_id":2016,"response_id":2024,"bus":"powertrain"}]}
```

Cada línea siguiente es un registro con `t` (segundos desde el inicio de la
grabación, no decreciente) y `type`:

| `type` | Campos | Significado |
| --- | --- | --- |
| `lifecycle` | `state` | `stopped`, `running` o `paused` |
| `state` | `ecu`, `signals`, `dtcs` | estado inicial de una ECU en `t = 0` |
| `frame` | `bus`, `id`, `ext`, `data` (hex) | frame CAN visto en un bus virtual |
| `diag` | `ecu`, `direction` (`request`/`response`), `protocol` (`obd`/`uds`), `data` (hex) | payload ISO-TP reensamblado |
| `signal` | `ecu`, `signal`, `value` | nuevo valor observable de una señal |
| `dtc` | `ecu`, `action` (`add`/`remove`), `code` | cambio de DTCs |
| `fault` | `ecu`, `action` (`start`/`end`), `kind`, `start`, `until` (o `null`) y los parámetros del `Fault` | fallo que empieza o termina |
| `scenario` | los campos de `ScenarioEvent` | evento de escenario aplicado |

Un `fault` que expira solo se registra con `t` igual a su `until`. Una
respuesta negativa `7F` se clasifica por el servicio que rechaza.

**Compatibilidad:** un lector de la versión 1 rechaza cualquier otra versión.
Añadir campos a un registro no cambia la versión; cambiar el significado de
uno existente o añadir un `type` nuevo sí la cambia.

## Reproducir

```bash
ecusdk replay session.eculog                     # en tiempo real
ecusdk replay session.eculog --speed 10          # 10 veces más rápido
ecusdk replay session.eculog --fast --type diag  # sin esperas, sólo OBD/UDS
ecusdk replay session.eculog --socketcan vcan0   # reinyecta los frames
```

`--socketcan` envía los frames grabados a esa interfaz con sus tiempos
relativos. Hay que indicarla explícitamente, igual que en la configuración de
buses físicos.

Desde Python:

```python
from ecusdk import read_log, replay

log = read_log("session.eculog")
log.state_at(12.5)  # {"ecm": {"signals": {...}, "dtcs": [...], "faults": [...]}}
for t, bus, frame in log.frames("powertrain"):
    ...
replay(log, print, speed=2)  # entrega cada registro respetando el orden y el tiempo
```

`replay` acepta un `clock`; con `VirtualClock` la espera es instantánea y
determinista.
