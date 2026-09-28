# ==============================================================================
# backend/schemas/clientes.py
# Modelos Pydantic de la cartera de clientes (Fase A del sistema de puntos).
# ==============================================================================

from typing import Optional
from pydantic import BaseModel, Field


class ClienteNuevo(BaseModel):
    """Alta de cliente. nombre SIEMPRE obligatorio; email/telefono/pin se
    validan contra la config del tenant (clientes_campos)."""
    nombre : str = Field(..., min_length=1, description="Nombre del cliente (siempre obligatorio)")
    email    : Optional[str] = None
    telefono : Optional[str] = None
    pin      : Optional[str] = Field(None, description="Contraseña de identificación (si la config la pide)")
    notas    : Optional[str] = None


class ClienteActualizar(BaseModel):
    """Edición de cliente. pin: None = sin cambio, '' = quitar, valor = re-hashear."""
    nombre : Optional[str] = None
    email    : Optional[str] = None
    telefono : Optional[str] = None
    pin      : Optional[str] = None
    notas    : Optional[str] = None
    activo   : Optional[bool] = None


class AjustePuntos(BaseModel):
    """Ajuste manual del ledger: puntos positivo = dar, negativo = quitar."""
    puntos  : int = Field(..., description="+ dar puntos / − quitar puntos")
    concepto: str = Field(..., min_length=1, description="Motivo (ej. 'Promo 50%')")


class VerificarCliente(BaseModel):
    """Identificación en el POS: número o correo exactos + contraseña si el
    cliente la tiene configurada (se valida con hash en el backend)."""
    identificador: str = Field(..., min_length=1, description="Teléfono o correo del cliente")
    pin      : Optional[str] = None
