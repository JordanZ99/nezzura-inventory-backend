from typing import Optional
from pydantic import BaseModel, Field


class NuevaTerminal(BaseModel):
    nombre: str = Field(..., min_length=1, description="Nombre identificador (ej. 'Banorte móvil')")
    banco: Optional[str] = None
    comision_debito_pct: float = Field(0, ge=0, description="% de comisión sobre débito")
    comision_credito_pct: float = Field(0, ge=0, description="% de comisión sobre crédito")
    comision_fija: float = Field(0, ge=0, description="Cuota fija por transacción")


class ActualizarTerminal(BaseModel):
    nombre: Optional[str] = None
    banco: Optional[str] = None
    comision_debito_pct: Optional[float] = Field(None, ge=0)
    comision_credito_pct: Optional[float] = Field(None, ge=0)
    comision_fija: Optional[float] = Field(None, ge=0)
    activo: Optional[bool] = None
