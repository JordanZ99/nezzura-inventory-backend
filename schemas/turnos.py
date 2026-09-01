from typing import Optional
from pydantic import BaseModel, Field


class AbrirTurno(BaseModel):
    monto_apertura: float = Field(0, ge=0, description="Fondo de caja con el que inicia el turno")


class CerrarTurno(BaseModel):
    efectivo_contado: float = Field(..., ge=0, description="Efectivo contado en el cajón")
    notas: Optional[str] = None
