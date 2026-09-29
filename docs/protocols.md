# Protocolos de la Fase 2

ECUSDK implementa CAN clásico e ISO-TP con direccionamiento normal, IDs CAN
estándar o extendidos y payloads de hasta 4095 bytes. No incluye CAN FD ni
extended/mixed addressing ISO-TP en esta fase. Un ID CAN de 29 bits se selecciona
con `is_extended_id=True`; esto es independiente del modo de direccionamiento
ISO-TP.

## ISO-TP programable

`IsoTpTransport` combina codecs, máquinas RX/TX y timers basados en `Clock`.
No abre interfaces, no crea threads y no duerme. El usuario controla la entrega
de frames y llama a `poll()` para obtener los frames que ya pueden enviarse.
También puede llamar a `pump(bus)` sobre un `CanBus` que tenga una cola de
entrada independiente de sus propios envíos.

```python
from ecusdk import IsoTpTransport, VirtualClock

clock = VirtualClock()
client = IsoTpTransport(0x700, 0x701, clock=clock)
ecu = IsoTpTransport(0x701, 0x700, clock=clock, block_size=2, separation_time=20)
client.send(b"diagnostic payload")

for frame in client.poll():  # inicialmente sólo FF
    ecu.receive(frame)
for control in ecu.poll():  # CTS con BS=2 y STmin=20 ms
    client.receive(control)

payload = None
for frame in client.poll():  # primer CF habilitado
    payload = ecu.receive(frame)
clock.advance(0.020)
for frame in client.poll():  # siguiente CF, ya separado por STmin
    payload = ecu.receive(frame)
assert payload == b"diagnostic payload"
```

`receive(frame)` devuelve el payload completo o `None` mientras espera más
frames. Las direcciones ajenas se ignoran. SF/FF/CF validan longitud, secuencia y
consistencia de dirección; el padding del último frame no forma parte del
payload. `segment` y `reassemble` continúan disponibles como codecs offline;
`segment` no representa un envío al bus y por tanto no realiza Flow Control.

El emisor espera CTS después del FF y de cada bloque. BS=0 habilita todos los CF
restantes. STmin admite 0–127 ms y 100–900 microsegundos; los valores reservados
se rechazan. WAIT renueva el deadline hasta `max_wait_frames` (por defecto 3);
OVERFLOW cancela el envío. El receptor anuncia OVERFLOW cuando la longitud
supera `max_payload`. `timeout` (por defecto 1 segundo) limita la espera de FC
y la espera entre frames recibidos.

Las malformaciones y OVERFLOW remoto producen `IsoTpError`; la expiración de
un timer produce `IsoTpTimeoutError`. Ambos derivan de `ECUSDKError`.
`poll()` y `receive()` comprueban timers; se deben llamar periódicamente incluso
cuando no llega tráfico. Ante un error, el endpoint descarta la transferencia y
puede reutilizarse. `reset()` cancela explícitamente RX/TX y frames pendientes.

`ECU.handle_frames()` ahora emite sólo FF para una respuesta larga y espera FC.
`Vehicle.exchange_frames()` y `exchange_isotp()` mantienen la API de respuesta
completa: realizan el intercambio CTS internamente sobre el bus. ELM327 sigue
utilizando esa API para VIN y DTCs. El transporte usa el reloj del vehículo.

Los parámetros BS/STmin describen el envío de CF conforme a la
[documentación ISO-TP del kernel Linux](https://kernel.org/doc/html/latest/networking/iso15765-2.html).
Las pruebas verifican bloques, tiempos, WAIT/OVERFLOW, timeout, errores,
secuencias, padding, límites y transferencias con IDs estándar/extendidos sin
necesitar hardware.

## OBD-II y DTCs

`RPM_PID` y `SPEED_PID` son codecs independientes e inmutables, también usados
por el runtime. Cada uno declara identificador, longitud, unidad, rango, escala
y señal fuente. `encode()` recibe un valor físico y devuelve sus bytes;
`decode()` recibe sólo los bytes del valor, sin el encabezado Mode/PID.

```python
from ecusdk import RPM_PID

assert RPM_PID.encode(850) == bytes.fromhex("0D 48")
assert RPM_PID.decode(bytes.fromhex("0D 48")) == 850
assert RPM_PID.unit == "rpm"
```

Valores fuera de rango/no finitos y longitudes inválidas producen `ObdError`,
que también es `ValueError` para preservar la captura de validaciones existente.
RPM cuantiza en pasos de 0,25 rpm; velocidad en pasos de 1 km/h. `PidCodec`
permite definir otros codecs numéricos con escala positiva y rango no negativo
cuyos extremos sean representables con esa escala;
añadir uno no lo registra automáticamente en la ECU.

La base OBD incluye Mode 01 PIDs 0C/0D, Mode 03 (leer DTCs), Mode 04 (limpiar)
y Mode 09 PID 02 (VIN de 17 caracteres desde TOML). `DtcStore` mantiene los
códigos de cada ECU y permite añadir, eliminar, listar y limpiar. Las pruebas
incluyen vectores conocidos y propiedades de round-trip para CAN, ISO-TP,
PID y DTC.
