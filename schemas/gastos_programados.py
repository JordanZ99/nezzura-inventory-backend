from enum import Enum
from typing import Optional
from pydantic import BaseModel


class TipoGasto(str, Enum):
    fijo = "fijo"
    porcentaje = "porcentaje"


class FrecuenciaGasto(str, Enum):
    semanal = "semanal"
    mensual = "mensual"
    anual = "anual"


class GastoProgramadoOut(BaseModel):
    id: str
    tenant_id: str
    nombre: str
    tipo: str
    valor: float
    frecuencia: str
    proxima_fecha: str
    created_at: Optional[str] = None


class NuevoGastoProgramado(BaseModel):
    nombre: str
    tipo: TipoGasto
    valor: float
    frecuencia: FrecuenciaGasto
    proxima_fecha: str


class ActualizarGastoProgramado(BaseModel):
    nombre: Optional[str] = None
    tipo: Optional[TipoGasto] = None
    valor: Optional[float] = None
    frecuencia: Optional[FrecuenciaGasto] = None
    proxima_fecha: Optional[str] = None
