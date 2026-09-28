# ==============================================================================
# backend/routers/gastos.py
# Endpoints de gastos del negocio.
# ==============================================================================

from typing import Optional
from fastapi import APIRouter, HTTPException, Depends

from database.gastos import (
    get_gastos, insertar_gasto, eliminar_gasto, confirmar_gasto,
    descartar_gasto, actualizar_gasto,
    listar_categorias_gasto, crear_categoria_gasto,
    renombrar_categoria_gasto, eliminar_categoria_gasto
)
from database.gastos_programados import verificar_y_generar_gastos_programados
from database.helpers import resolver_rango_q
from dependencies import get_tenant_id
from schemas.gastos import (
    NuevoGasto, ActualizarGasto, CrearCategoriaGasto, RenombrarCategoriaGasto
)

router = APIRouter(prefix="/gastos", tags=["Gastos"])


@router.get("/")
def listar_gastos(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    tenant_id: str = Depends(get_tenant_id),
):
    """
    Gastos en la ventana contable pedida (?desde/?hasta, 'YYYY-MM-DD').
    Sin ?desde ni ?hasta devuelve SOLO el mes contable actual.
    """
    verificar_y_generar_gastos_programados(tenant_id)
    try:
        d_desde, d_hasta = resolver_rango_q(tenant_id, desde, hasta)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    resultado = get_gastos(tenant_id, d_desde, d_hasta)
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
    """Actualiza monto, categoría y descripción de un gasto existente."""
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


# =============================================================================
# Endpoints para categorías de gasto editables
# =============================================================================

@router.get("/categorias")
def obtener_categorias_gasto(tenant_id: str = Depends(get_tenant_id)):
    """Devuelve la lista de categorías de gasto del tenant."""
    return listar_categorias_gasto(tenant_id)


@router.post("/categorias")
def crear_categoria_gasto_endpoint(data: CrearCategoriaGasto, tenant_id: str = Depends(get_tenant_id)):
    """Crea una categoría de gasto nueva."""
    return crear_categoria_gasto(data.nombre, tenant_id)


@router.put("/categorias/{categoria}")
def editar_categoria_gasto_endpoint(categoria: str, data: RenombrarCategoriaGasto, tenant_id: str = Depends(get_tenant_id)):
    """Renombra una categoría de gasto."""
    return renombrar_categoria_gasto(categoria, data.nuevo_nombre, tenant_id)


@router.delete("/categorias/{categoria}")
def borrar_categoria_gasto_endpoint(categoria: str, tenant_id: str = Depends(get_tenant_id)):
    """Elimina una categoría de gasto. Los gastos que la usaban se reasignan a 'Otros'."""
    return eliminar_categoria_gasto(categoria, tenant_id)