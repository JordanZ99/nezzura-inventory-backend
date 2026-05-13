# ==============================================================================
# backend/routers/gastos.py
# Endpoints de gastos del negocio.
# ==============================================================================

from database.gastos import get_gastos, insertar_gasto, eliminar_gasto
from dependencies import get_tenant_id
from fastapi import APIRouter, Depends
from pydantic import BaseModel

router = APIRouter(prefix="/gastos", tags=["Gastos"])


class NuevoGasto(BaseModel):
    fecha      : str
    categoria  : str
    descripcion: str
    monto      : float


@router.get("/")
def listar_gastos(tenant_id: str = Depends(get_tenant_id)):
    """Historial completo de gastos filtrado por tenant."""
    return get_gastos(tenant_id)


@router.post("/")
def crear_gasto(data: NuevoGasto, tenant_id: str = Depends(get_tenant_id)):
    """Registra un nuevo gasto vinculado a un tenant."""
    return insertar_gasto(data.fecha, data.categoria, data.descripcion, data.monto, tenant_id)


@router.delete("/{gasto_id}")
def borrar_gasto(gasto_id: int, tenant_id: str = Depends(get_tenant_id)):
    """Elimina un gasto por id y tenant."""
    return eliminar_gasto(gasto_id, tenant_id)