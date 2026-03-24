# ==============================================================================
# backend/routers/gastos.py
# Endpoints de gastos del negocio.
# ==============================================================================

from fastapi import APIRouter
from pydantic import BaseModel

from database.gastos import get_gastos, insertar_gasto, eliminar_gasto

router = APIRouter(prefix="/gastos", tags=["Gastos"])


class NuevoGasto(BaseModel):
    fecha      : str
    categoria  : str
    descripcion: str
    monto      : float


@router.get("/")
def listar_gastos():
    """Historial completo de gastos."""
    return get_gastos()


@router.post("/")
def crear_gasto(data: NuevoGasto):
    """Registra un nuevo gasto."""
    return insertar_gasto(data.fecha, data.categoria, data.descripcion, data.monto)


@router.delete("/{gasto_id}")
def borrar_gasto(gasto_id: int):
    """Elimina un gasto por id."""
    return eliminar_gasto(gasto_id)