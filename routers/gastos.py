# ==============================================================================
# backend/routers/gastos.py
# Endpoints de gastos del negocio.
# ==============================================================================

from database.gastos import get_gastos, insertar_gasto, eliminar_gasto, confirmar_gasto, descartar_gasto, actualizar_gasto
from database.gastos_programados import verificar_y_generar_gastos_programados
from dependencies import get_tenant_id
from fastapi import APIRouter, Depends
from typing import Optional
from pydantic import BaseModel

router = APIRouter(prefix="/gastos", tags=["Gastos"])


class NuevoGasto(BaseModel):
    fecha               : str
    categoria           : str
    descripcion         : str
    monto               : float
    estado              : Optional[str] = "pagado"
    gasto_programado_id : Optional[str] = None


class ActualizarGasto(BaseModel):
    """
    Modelo dedicado para la edición de gastos.
    A diferencia de NuevoGasto, no exige 'fecha' ni 'estado' porque la lógica
    de actualizar_gasto solo modifica monto, categoría y descripción.
    Esto elimina el workaround del frontend que enviaba fecha="" para engañar
    la validación de NuevoGasto reutilizado.
    """
    monto      : float
    categoria  : str
    descripcion: str


@router.get("/")
def listar_gastos(tenant_id: str = Depends(get_tenant_id)):
    """
    Historial completo de gastos.
    Antes de retornar, ejecuta el motor de verificación de gastos programados
    para generar automáticamente los gastos cuya proxima_fecha ya venció.
    """
    verificar_y_generar_gastos_programados(tenant_id)
    resultado = get_gastos(tenant_id)
    return resultado


@router.post("/")
def crear_gasto(data: NuevoGasto, tenant_id: str = Depends(get_tenant_id)):
    """Registra un nuevo gasto."""
    return insertar_gasto(
        fecha=data.fecha,
        categoria=data.categoria,
        descripcion=data.descripcion,
        monto=data.monto,
        tenant_id=tenant_id,
        estado=data.estado,
        gasto_programado_id=data.gasto_programado_id
    )


@router.put("/{gasto_id}/confirmar")
def confirmar_gasto_endpoint(gasto_id: int, tenant_id: str = Depends(get_tenant_id)):
    """Cambia el estado del gasto a 'pagado'."""
    return confirmar_gasto(gasto_id, tenant_id)


@router.put("/{gasto_id}/descartar")
def descartar_gasto_endpoint(gasto_id: int, tenant_id: str = Depends(get_tenant_id)):
    """Cambia el estado del gasto a 'descartado'."""
    return descartar_gasto(gasto_id, tenant_id)


@router.put("/{gasto_id}")
def editar_gasto(gasto_id: int, data: ActualizarGasto, tenant_id: str = Depends(get_tenant_id)):
    """
    Actualiza monto, categoría y descripción de un gasto existente.
    Ahora usa el modelo ActualizarGasto dedicado en lugar de reutilizar
    NuevoGasto, que exigía campos irrelevantes para la edición (fecha, estado).
    """
    return actualizar_gasto(
        gasto_id=gasto_id,
        monto=data.monto,
        categoria=data.categoria,
        descripcion=data.descripcion,
        tenant_id=tenant_id
    )


@router.delete("/{gasto_id}")
def borrar_gasto(gasto_id: int, tenant_id: str = Depends(get_tenant_id)):
    """Elimina un gasto por id."""
    return eliminar_gasto(gasto_id, tenant_id)