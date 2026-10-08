# Changelog

## Unreleased

- UDS (ISO 14229-1) propio en cada ECU (`ecu.uds`): 0x10, 0x11, 0x19
  (0x01, 0x02, 0x0A), 0x22 y 0x3E, con suppressPosRspMsgIndication, NRCs,
  timeout S3 y DIDs configurables por API o TOML (`[ecus.<ecu>.uds.dids]`),
  incluidos `DidSignal` calculados desde señales. `F190` toma el VIN.
- `ECU.diagnostic_payload` despacha OBD (SID ≤ 0x0A) o UDS.
- ELM327 envía cualquier petición hexadecimal de 1 a 7 bytes; las peticiones
  no soportadas reciben su respuesta negativa en vez de `?`.
- Shell y CLI: `request ECU HEX`; `inspect` muestra los DIDs.

## 0.1.0 — 2026-10-08

Primera versión con contrato de API probado. Cumple los criterios de
aceptación de v0.1 del PRD salvo la validación con Rodar, que queda pendiente.

- Tests de contrato público: nombres exportados, firmas clave, jerarquía de
  errores y docstrings obligatorios en toda la API pública.
- Prueba de rendimiento de 20 ECUs en un bus (~21.700 frames/s en un M4) en
  tests y en `examples/bench.py`.
- `load_vehicle` exportado desde `ecusdk`.
- Añadidos generadores de señales deterministas (constant, linear, ramp, sine,
  random con seed y custom) en API y TOML, y `Vehicle.advance` / `elapsed`.
- Escenarios TOML (`[scenario] events`) con cambios de señal y fallos; la
  ventana de cada fallo empieza en su instante programado y las repeticiones
  tras `reset()` son idénticas.
- Inyección de fallos por API, escenario y CLI: ECU offline, timeout, latencia,
  Flow Control tardío, frame drop, duplicado, malformado, longitud inválida,
  PID incorrecto, CF perdido, secuencia errónea, interrupción ISO-TP, sensor
  congelado, fuera de rango e intermitente.
- `exchange*` aceptan `timeout` para esperar respuestas retrasadas; `Clock`
  incorpora `sleep`. Los clientes que implementan `Clock` deben añadirlo.
- Catálogo OBD ampliado (carga, temperaturas, presión, MAF, acelerador,
  combustible, tensión) con offset en `PidCodec`, y bitmaps de PIDs soportados
  (`0100`, `0120`…).
- ELM327 acepta cualquier consulta OBD, direccionamiento funcional a varias
  ECUs, `ATSH`, `ATS0/1`, `ATI`, `ATD` y responde `CAN ERROR` ante errores
  ISO-TP.
- CLI: `inspect`, `shell`, `get`, `set`, `dtc`, `fault` y `faults` sobre un
  shell de control TCP que `ecusdk run` sirve en el puerto 35001.
- Jerarquía de errores del PRD: `ConfigurationError`, `CanError`, `UdsError`,
  `SimulationError` y `VehicleStateError`, compatibles con `ValueError` /
  `RuntimeError`.

- Añadidos nodos CAN independientes con broadcast, filtros por ID/máscara,
  timestamps de Clock y cierre explícito; Vehicle usa nodos para cada ECU
  y conserva las transacciones OBD/ISO-TP completas.
- Añadido registro de PIDs por ECU con asociaciones de señales vía API y
  TOML, registro de codecs personalizados y validación de configuraciones.

- Completado el transporte ISO-TP con CTS/WAIT/OVERFLOW, block size, STmin,
  timeouts y reloj inyectable; las respuestas largas de ECU esperan Flow Control.
- Corregido el reensamblado de payloads con padding y la validación de CF
  truncados o de otra dirección.
- Extraídos codecs PID tipados de RPM/velocidad y ampliadas pruebas de
  propiedades CAN/ISO-TP/PID/DTC y vectores de protocolo.

- Añadidos buses CAN virtual y SocketCAN explícito, ISO-TP, OBD-II básico,
  simulación determinista y emulador ELM327.
- CI valida Python 3.11–3.14 en Linux, macOS y Windows, cobertura global mínima
  del 90% y contenido del wheel instalado en un entorno limpio.
