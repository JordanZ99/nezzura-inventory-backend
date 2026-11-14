# ==============================================================================
# backend/schemas/ordenes.py
# Modelos Pydantic de órdenes de servicio (giro 'servicios', migración 077):
# el registro del TRABAJO — reparaciones, instalaciones, consultoría,
# mantenimiento o lo que salga. El dinero vive en cobros; aquí solo el precio
# informativo y el enlace opcional cobro_id.
# ==============================================================================

from datetime import date
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

TipoOrden = Literal["reparacion", "instalacion", "consultoria", "mantenimiento", "otro"]
EstadoOrden = Literal["recibido", "en_proceso", "esperando", "listo", "entregado", "cancelado"]


class OrdenNueva(BaseModel):
    """Alta de una orden de servicio (intake mínimo: título; el resto opcional)."""
    titulo            : str = Field(..., min_length=1, description="Qué se pide, en una línea")
    tipo              : TipoOrden = "otro"
    cliente_id        : Optional[str] = None
    equipo            : Optional[str] = Field(None, description="Sobre qué se trabaja: modelo/serie/IMEI, dominio, local...")
    detalle_reportado : Optional[str] = Field(None, description="Lo que reportó el cliente, en sus palabras")
    solucion          : Optional[str] = Field(None, description="Lo que encontraste/hiciste (tu versión)")
    estado            : EstadoOrden = "recibido"
    fecha_recibido    : Optional[date] = None
    precio            : Optional[float] = Field(None, ge=0, description="Informativo hasta cobrarse")
    notas             : Optional[str] = None


class OrdenActualizar(BaseModel):
    """PATCH parcial. estado: pasar a 'entregado' sella fecha_entregado=HOY.
    fecha_entregado: None = sin cambio; '' = quitarla; date = fijar.
    precio: 0 = quitarlo; >0 = fijarlo.
    Enlace de cobro (mutuamente excluyentes): cobro_id = cobro PUNTUAL;
    cuota_id = venta a plazos (la cuota que genera sus abonos). Mig 078."""
    cliente_id        : Optional[str] = None
    tipo              : Optional[TipoOrden] = None
    titulo            : Optional[str] = Field(None, min_length=1)
    equipo            : Optional[str] = None
    detalle_reportado : Optional[str] = None
    solucion          : Optional[str] = None
    estado            : Optional[EstadoOrden] = None
    fecha_recibido    : Optional[date] = None
    fecha_entregado   : Optional[Union[date, Literal[""]]] = None
    precio            : Optional[float] = Field(None, ge=0)
    cobro_id          : Optional[str] = None
    cuota_id          : Optional[str] = None
    notas             : Optional[str] = None
