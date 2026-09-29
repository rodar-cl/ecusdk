# Changelog

## Unreleased

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
