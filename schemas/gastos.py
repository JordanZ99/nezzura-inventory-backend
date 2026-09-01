from typing import Optional
from pydantic import BaseModel, Field


class NuevoGasto(BaseModel):
    fecha               : str
    categoria           : str
    descripcion         : str
    monto               : float
    estado              : Optional[str] = "pagado"
    gasto_programado_id : Optional[str] = None


class ActualizarGasto(BaseModel):
    monto      : float
    categoria  : str
    descripcion: str


class CrearCategoriaGasto(BaseModel):
    nombre: str = Field(..., min_length=1, description="Nombre de la categoría")


class RenombrarCategoriaGasto(BaseModel):
    nuevo_nombre: str = Field(..., min_length=1, description="Nuevo nombre para la categoría")
