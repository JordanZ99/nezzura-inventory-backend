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


# Frecuencias de suscripción (mig 063). Semanal: cada 'dia_semana' del mes
# (0=Lunes..6=Domingo). Quincenal: día 1 y día 15 de cada mes.
FrecuenciaCuota = Literal["mensual", "semanal", "quincenal"]


class CuotaNueva(BaseModel):
    """Suscripción recurrente: mensual (día 1..28 del mes), semanal (un día
    de la semana, se genera en cada aparición del mes) o quincenal (días 1
    y 15). monto_meta (opcional, 064) la vuelve CUENTA FINITA: el generador
    deja de emitir al cubrir el total y el último abono sale al residuo."""
    cliente_id  : str = Field(..., min_length=1)
    concepto    : str = Field(..., min_length=1)
    monto       : float = Field(..., gt=0)
    frecuencia  : FrecuenciaCuota = "mensual"
    dia_cobro   : int = Field(1, ge=1, le=28, description="Solo mensual: día del mes")
    dia_semana  : Optional[int] = Field(None, ge=0, le=6, description="Solo semanal: 0=Lunes..6=Domingo")
    monto_meta  : Optional[float] = Field(None, gt=0, description="Total a cubrir (NULL = infinita)")
    notas       : Optional[str] = None


class CuotaActualizar(BaseModel):
    """Todas las columnas son opcionales; el backend valida cada una que llegue.
    monto_meta: None = sin cambio; 0 = quitar la meta (vuelve infinita); >0 = fijar."""
    cliente_id   : Optional[str] = None
    concepto     : Optional[str] = None
    monto        : Optional[float] = Field(None, gt=0)
    frecuencia   : Optional[FrecuenciaCuota] = None
    dia_cobro    : Optional[int] = Field(None, ge=1, le=28)
    dia_semana   : Optional[int] = Field(None, ge=0, le=6)
    monto_meta   : Optional[float] = Field(None, ge=0)
    activo       : Optional[bool] = None
    notas        : Optional[str] = None
