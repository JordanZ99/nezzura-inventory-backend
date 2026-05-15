# ==============================================================================
# backend/routers/gastos.py
# Endpoints de gastos del negocio.
# ==============================================================================

from database.gastos import get_gastos, insertar_gasto, eliminar_gasto
from dependencies import validar_sesion
from fastapi import APIRouter, Depends
from pydantic import BaseModel

router = APIRouter(prefix="/gastos", tags=["Gastos"])


class NuevoGasto(BaseModel):
    fecha      : str
    categoria  : str
    descripcion: str
    monto      : float


@router.get("/")
def listar_gastos(_: bool = Depends(validar_sesion)):
    """Historial completo de gastos."""
    resultado = get_gastos()
    return resultado


@router.post("/")
def crear_gasto(data: NuevoGasto, _: bool = Depends(validar_sesion)):
    """Registra un nuevo gasto."""
    return insertar_gasto(data.fecha, data.categoria, data.descripcion, data.monto)


@router.delete("/{gasto_id}")
def borrar_gasto(gasto_id: int, _: bool = Depends(validar_sesion)):
    """Elimina un gasto por id."""
    return eliminar_gasto(gasto_id)