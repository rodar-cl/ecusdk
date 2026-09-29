"""Excepciones públicas de ECUSDK."""


class ECUSDKError(Exception):
    """Error base del SDK."""


class AdapterError(ECUSDKError):
    """Error al abrir o utilizar un adaptador físico."""


class ProtocolError(ECUSDKError):
    """Mensaje de protocolo inválido."""


class IsoTpError(ProtocolError):
    """Error de ISO-TP."""


class IsoTpTimeoutError(IsoTpError, TimeoutError):
    """Se agotó un timeout de ISO-TP."""


class ObdError(ProtocolError):
    """Error de OBD-II."""
