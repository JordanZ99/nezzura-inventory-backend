# ==============================================================================
# backend/schemas/cobros.py
# Modelos Pydantic de cobros (giro 'servicios', migración 059): cuotas
# mensuales, cobros extra (trabajos puntuales) y abonos.
# ==============================================================================

from datetime import date
from typing import Literal, Optional, Union
from pydantic import BaseModel, Field

# Métodos de abono: libre, pero la UI sugiere estos (igual que el POS permite
# contar cualquier método de pago en ordenes.pagos).
MetodoPago = Literal["efectivo", "transferencia", "tarjeta", "otro"]


class EntradaPrograma(BaseModel):
    """Una cuota del plan de pagos comprometido (el ACUERDO, no el dinero).
    Ej.: {etiqueta: 'Anticipo 50%', monto: 7500, fecha: '2026-11-15'}."""
    etiqueta: str = Field(..., min_length=1, description="Nombre de la parte (ej. 'Anticipo 50%')")
    monto  : float = Field(..., gt=0)
    fecha  : str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="Fecha comprometida (YYYY-MM-DD)")


class GenerarPeriodo(BaseModel):
    """Alta de cobros del mes desde las cuotas activas (idempotente)."""
    periodo: str = Field(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="Periodo a generar (YYYY-MM)")


class CobroExtraNuevo(BaseModel):
    """Alta de un trabajo puntual: el cliente, qué fue, cuánto y cuándo vence.
    programa: plan de pagos opcional (anticipos/entregas)."""
    cliente_id  : str = Field(..., min_length=1)
    concepto    : str = Field(..., min_length=1)
    monto       : float = Field(..., gt=0)
    vencimiento : Optional[date] = None
    programa    : Optional[list[EntradaPrograma]] = None
    notas       : Optional[str] = None


class CobroActualizar(BaseModel):
    """Edición de deuda. pagos NO se edita aquí: es ledger (solo alta/baja de
    abonos vía /pagos). vencimiento: None = sin cambio, '' = quitar la fecha."""
    concepto    : Optional[str] = None
    monto       : Optional[float] = Field(None, gt=0)
    vencimiento : Optional[Union[date, Literal[""]]] = None
    programa    : Optional[list[EntradaPrograma]] = None
    notas       : Optional[str] = None


class AbonoNuevo(BaseModel):
    """Dinero entrante (parcial o total). Append-only: el saldo se recalcula
    en el backend; nunca se persiste el 'estado'.""" 
    monto  : float = Field(..., gt=0)
    metodo : Optional[MetodoPago] = None
    fecha  : Optional[date] = None


class CuotaNueva(BaseModel):
    """Suscripción mensual: cliente + cuánto + qué día del mes vence (1..28,
    para no pelear con fechas inexistentes)."""
    cliente_id : str = Field(..., min_length=1)
    concepto   : str = Field(..., min_length=1)
    monto      : float = Field(..., gt=0)
    dia_cobro  : int = Field(1, ge=1, le=28)
    notas      : Optional[str] = None


class CuotaActualizar(BaseModel):
    """Todas las columnas son opcionales; el backend valida cada one que llegue."""
    cliente_id : Optional[str] = None
    concepto   : Optional[str] = None
    monto      : Optional[float] = Field(None, gt=0)
    dia_cobro  : Optional[int] = Field(None, ge=1, le=28)
    activo     : Optional[bool] = None
    notas      : Optional[str] = None
