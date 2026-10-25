# ==============================================================================
# backend/schemas/clientes.py
# Modelos Pydantic de la cartera de clientes (Fase A del sistema de puntos)
# y de los recursos CRM (Fase B: etiquetas, seguimiento).
# ==============================================================================

from datetime import date
from typing import Literal, Optional, Union
from pydantic import BaseModel, Field


class ClienteNuevo(BaseModel):
    """Alta de cliente. nombre SIEMPRE obligatorio; email/telefono/pin se
    validan contra la config del tenant (clientes_campos)."""
    nombre : str = Field(..., min_length=1, description="Nombre del cliente (siempre obligatorio)")
    email    : Optional[str] = None
    telefono : Optional[str] = None
    pin      : Optional[str] = Field(None, description="Contraseña de identificación (si la config la pide)")
    notas    : Optional[str] = None
    # CRM (migs 054): campo opcional de perfil; el origen dice de qué campaña
    # o canal vino el cliente ('WhatsApp', 'Recomendado', 'Volante'...).
    cumpleanos : Optional[date] = None
    origen     : Optional[str] = None


class ClienteActualizar(BaseModel):
    """Edición de cliente. pin: None = sin cambio, '' = quitar, valor = re-hashear.
    cumpleanos/origen igual: None = sin cambio, '' = borrar el valor."""
    nombre : Optional[str] = None
    email    : Optional[str] = None
    telefono : Optional[str] = None
    pin      : Optional[str] = None
    notas    : Optional[str] = None
    activo   : Optional[bool] = None
    # El Union(date, "") permite al front distinguir 'sin cambio' (null) de
    # 'borrar' (cadena vacía); Pydantic v2 parsea la fecha ISO primero.
    cumpleanos : Optional[Union[date, Literal[""]]] = None
    origen     : Optional[str] = None


class AjustePuntos(BaseModel):
    """Ajuste manual del ledger: puntos positivo = dar, negativo = quitar."""
    puntos  : int = Field(..., description="+ dar puntos / − quitar puntos")
    concepto: str = Field(..., min_length=1, description="Motivo (ej. 'Promo 50%')")


class VerificarCliente(BaseModel):
    """Identificación en el POS: número o correo exactos + contraseña si el
    cliente la tiene configurada (se valida con hash en el backend)."""
    identificador: str = Field(..., min_length=1, description="Teléfono o correo del cliente")
    pin      : Optional[str] = None


# ── CRM (Fase B): etiquetas libres + interacciones de seguimiento ────────────

class EtiquetaNueva(BaseModel):
    """Alta de etiqueta libre del tenant. El color es un hex opcional (con
    fallback a un default en el backend). Máx 40 chars para no romper chips."""
    nombre: str = Field(..., min_length=1, max_length=40)
    color : Optional[str] = Field(
        None, pattern=r"^#[0-9a-fA-F]{6}$",
        description="Hex de 6 dígitos; si no, el backend pone un default",
    )


class EtiquetaActualizar(BaseModel):
    """Edición de etiqueta: None = sin cambio. Para reponer color se manda uno válido."""
    nombre: Optional[str] = Field(None, min_length=1, max_length=40)
    color : Optional[str] = Field(
        None, pattern=r"^#[0-9a-fA-F]{6}$",
        description="'' o inválido cae al color default (no deja etiqueta sin color)",
    )


class SetEtiquetas(BaseModel):
    """Sincroniza las etiquetas de UN cliente (semántica replace-all: la lista
    enviada es la lista final; lo que falte se desasigna)."""
    etiqueta_ids: list[str] = Field(default_factory=list, description="IDs completos de etiquetas del tenant")


class InteraccionNueva(BaseModel):
    """Registro del timeline de seguimiento (mig 055). tipo: nota|llamada|visita|promo."""
    tipo : str = Field(..., min_length=1)
    texto: str = Field(..., min_length=1, description="Qué pasó / qué se acordó")
    creado_por: Optional[str] = Field(None, description="Quién registró (single-owner: texto libre)")
