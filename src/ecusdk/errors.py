"""Excepciones públicas de ECUSDK.

Las subclases que heredan también de ``ValueError`` o ``RuntimeError`` conservan
la compatibilidad con código que ya capturaba esas excepciones estándar.
"""


class ECUSDKError(Exception):
    """Error base del SDK."""


class ConfigurationError(ECUSDKError, ValueError):
    """Configuración TOML o de API inválida."""


class AdapterError(ECUSDKError):
    """Error al abrir o utilizar un adaptador físico."""


class ProtocolError(ECUSDKError):
    """Mensaje de protocolo inválido."""


class CanError(ProtocolError, ValueError):
    """Frame o identificador CAN inválido."""


class IsoTpError(ProtocolError):
    """Error de ISO-TP."""


class IsoTpTimeoutError(IsoTpError, TimeoutError):
    """Se agotó un timeout de ISO-TP."""


class ObdError(ProtocolError, ValueError):
    """Error de OBD-II."""


class UdsError(ProtocolError):
    """Error de UDS."""


class SimulationError(ECUSDKError, ValueError):
    """Definición de simulación inválida: señal, escenario o fallo."""


class VehicleStateError(ECUSDKError, RuntimeError):
    """Operación no permitida en el estado actual del vehículo."""


class RecordingError(ECUSDKError, ValueError):
    """Archivo ``.eculog`` inválido o de una versión no soportada."""
