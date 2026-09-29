# ECUSDK

**Una sandbox automotriz programable para probar CAN, OBD-II y diagnóstico vehicular sin necesitar un auto físico.**

> ECUSDK está en desarrollo activo. Las APIs, formatos de configuración y arquitectura interna pueden cambiar significativamente antes de la primera versión estable.

ECUSDK es un toolkit source-available para crear vehículos virtuales compuestos por ECUs programables.

El proyecto nace inicialmente como entorno de pruebas para [Rodar](https://rodar.cl), permitiendo que software automotriz interactúe con ECUs simuladas usando los mismos protocolos que utilizaría contra un vehículo real.

La idea no es hacer mocks de APIs de aplicación, sino simular la capa de comunicación automotriz.

```text
Aplicación
     │
     │ OBD / UDS
     ▼
 ELM327 virtual
     │
     │ CAN / ISO-TP
     ▼
┌─────────────────────┐
│       ECUSDK        │
│                     │
│  ECM   ABS   TCM    │
│   │     │     │     │
│   └── CAN virtual ──┘
└─────────────────────┘
```

## Objetivo

ECUSDK busca hacer que las pruebas de software automotriz sean reproducibles, programables e independientes de vehículos físicos.

El proyecto está pensado para soportar:

- ECUs virtuales
- redes CAN virtuales
- múltiples ECUs por vehículo
- OBD-II
- ISO-TP
- UDS
- simulación de DTCs
- señales vehiculares programables
- inyección de fallas
- reproducción de escenarios
- SocketCAN / `vcan`
- interfaces ELM327 virtuales
- testing automatizado y entornos CI

## Ejemplo

La definición de un vehículo virtual debería verse aproximadamente así:

```yaml
vehicle:
  name: demo-car
  vin: ECUSDK00000000001

buses:
  powertrain:
    type: can
    bitrate: 500000

ecus:
  ecm:
    bus: powertrain

    can:
      request_id: 0x7E0
      response_id: 0x7E8

    signals:
      rpm:
        initial: 850

      speed:
        initial: 0

      coolant:
        initial: 88

    obd:
      "01:0C": rpm
      "01:0D": speed
      "01:05": coolant
```

Luego:

```bash
ecusdk run demo-car.yaml
```

Una aplicación externa podría conectarse a ECUSDK y enviar solicitudes de diagnóstico estándar como:

```text
01 0C
```

ECUSDK respondería usando el estado actual de la ECU virtual.

## ¿Por qué ECUSDK?

Probar software automotriz contra vehículos físicos tiene varias limitaciones.

Los vehículos son costosos de conseguir, distintas marcas exponen comportamientos diferentes, muchas fallas son difíciles de reproducir deliberadamente y ciertas condiciones no pueden provocarse de manera segura.

Además, un vehículo físico no puede formar parte de un pipeline normal de integración continua.

ECUSDK permite describir de manera determinista el estado que necesita una prueba.

Por ejemplo:

```yaml
scenario:
  name: overheating

timeline:
  - at: 0s
    set:
      rpm: 850
      coolant: 85

  - at: 20s
    ramp:
      coolant:
        to: 125
        duration: 40s

  - at: 45s
    dtc:
      ecu: ecm
      add: P0217

  - at: 60s
    fault:
      ecu: ecm
      type: timeout
```

Esto permite reproducir condiciones como sobrecalentamiento, sensores fuera de rango, timeouts, ECUs desconectadas o errores de comunicación.

## Alcance

ECUSDK es principalmente un simulador de comportamiento de ECUs.

Su objetivo es reproducir lo que software externo puede observar a través de protocolos vehiculares.

No busca inicialmente emular el hardware interno exacto de una ECU Bosch, Continental, Denso u otro fabricante.

Tampoco ejecuta firmware OEM.

```text
ECUSDK

comportamiento ECU
CAN
ISO-TP
OBD-II
UDS
DTCs
señales
fallas

✓ dentro del alcance
```

```text
emulación de CPU
firmware OEM
runtime AUTOSAR completo
implementaciones propietarias específicas

✗ fuera del alcance inicial
```

## Arquitectura prevista

```text
ECUSDK
│
├── core
│   ├── Vehicle
│   ├── ECU
│   ├── State
│   └── VirtualClock
│
├── bus
│   ├── VirtualCAN
│   └── SocketCAN
│
├── protocols
│   ├── OBD-II
│   ├── ISO-TP
│   └── UDS
│
├── simulation
│   ├── Signals
│   ├── Scenarios
│   └── Faults
│
├── adapters
│   ├── ELM327
│   ├── TCP
│   └── Serial
│
└── recording
    ├── Capture
    └── Replay
```

## Estado actual

ECUSDK se encuentra en una etapa temprana de desarrollo.

El primer objetivo es deliberadamente pequeño:

```text
CAN virtual
      ↓
ECM virtual
      ↓
OBD-II Mode 01
      ↓
PID 0C
      ↓
respuesta RPM
```

La primera versión útil debería permitir:

```bash
ecusdk run demo-car
```

y exponer una ECU virtual capaz de responder consultas OBD-II básicas.

## Roadmap

### v0.1

- Vehicle runtime
- ECU runtime
- CAN virtual
- SocketCAN
- Linux `vcan`
- ISO-TP
- OBD-II Modes 01, 03, 04 y 09
- señales
- DTCs
- scenario engine
- fault injection básico
- ELM327 vía TCP / serial
- CLI
- ejecución headless

### v0.2

- soporte UDS más amplio
- importación DBC
- CAN FD
- recording
- replay
- mejores herramientas de inspección

### Más adelante

- múltiples buses
- gateways
- simulación de carga
- arbitraje CAN más preciso
- Bluetooth
- DoIP
- J1939
- perfiles comunitarios de ECUs

## Seguridad

ECUSDK está diseñado principalmente para desarrollo, simulación y testing.

Por defecto no debería enviar tráfico hacia interfaces CAN físicas.

El uso de hardware real debe requerir configuración explícita.

No utilices ECUSDK para enviar tráfico arbitrario a sistemas críticos de seguridad dentro de un vehículo operativo.

## Contribuir

ECUSDK está actualmente en una fase experimental.

Se aceptarán issues, correcciones de protocolo, tests, perfiles vehiculares y contribuciones de implementación a medida que la arquitectura central se estabilice.

Más adelante se añadirá una guía formal de contribución.

## Licencia

ECUSDK se distribuye bajo la **PolyForm Noncommercial License 1.0.0**.

Puedes usar, estudiar, modificar y redistribuir ECUSDK para los usos no comerciales permitidos por esa licencia.

El uso comercial no está autorizado por la licencia pública.

Las organizaciones que quieran utilizar ECUSDK con fines comerciales deberán obtener una licencia comercial independiente.

Consulta [`LICENSE`](./LICENSE).

## Proyecto

ECUSDK es desarrollado por Rodar.

https://rodar.cl

© 2026 Rodar
