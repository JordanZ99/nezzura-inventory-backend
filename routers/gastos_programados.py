# ==============================================================================
# backend/routers/gastos_programados.py
# Endpoints para gastos programados (suscripciones recurrentes).
# ==============================================================================

from fastapi import APIRouter, Depends
from dependencies import get_tenant_id
from database.conexion import query
from database.gastos_programados import (
    crear_gasto_programado, ejecutar_gasto_programado,
    eliminar_gasto_programado, actualizar_gasto_programado,
    estimar_monto
)
from schemas.gastos_programados import (
    TipoGasto, FrecuenciaGasto, GastoProgramadoOut,
    NuevoGastoProgramado, ActualizarGastoProgramado
)

router = APIRouter(prefix="/gastos_programados", tags=["Gastos Programados"])


@router.get("")
def listar_gastos_programados(tenant_id: str = Depends(get_tenant_id)):
    """
    Retorna todas las reglas de gastos programados del tenant.
    Incluye 'ultimo_monto': el monto del último gasto generado por cada regla.
    """
    resultado = query(
        "SELECT gp.id, gp.tenant_id, gp.nombre, gp.tipo, gp.valor, gp.frecuencia, "
        "       gp.proxima_fecha, gp.created_at, "
        "       (SELECT monto FROM gastos "
        "        WHERE gasto_programado_id = gp.id "
        "          AND tenant_id = gp.tenant_id "
        "        ORDER BY id DESC LIMIT 1) as ultimo_monto "
        "FROM gastos_programados gp "
        "WHERE gp.tenant_id = %s "
        "ORDER BY gp.proxima_fecha ASC, gp.created_at DESC",
        (tenant_id,)
    )
    return resultado


@router.post("")
def crear_gasto_programado_endpoint(
    data: NuevoGastoProgramado,
    tenant_id: str = Depends(get_tenant_id)
):
    """Crea una nueva regla de gasto programado."""
    return crear_gasto_programado(
        nombre=data.nombre,
        tipo=data.tipo.value,
        valor=data.valor,
        frecuencia=data.frecuencia.value,
        proxima_fecha=data.proxima_fecha,
        tenant_id=tenant_id
    )


@router.put("/{gasto_prog_id}")
def actualizar_gasto_programado_endpoint(
    gasto_prog_id: str,
    data: ActualizarGastoProgramado,
    tenant_id: str = Depends(get_tenant_id)
):
    """Actualiza una regla de gasto programado existente."""
    return actualizar_gasto_programado(
        gasto_prog_id=gasto_prog_id,
        nombre=data.nombre,
        tipo=data.tipo.value if data.tipo else None,
        valor=data.valor,
        frecuencia=data.frecuencia.value if data.frecuencia else None,
        proxima_fecha=data.proxima_fecha,
        tenant_id=tenant_id
    )


@router.delete("/{gasto_prog_id}")
def eliminar_gasto_programado_endpoint(
    gasto_prog_id: str,
    tenant_id: str = Depends(get_tenant_id)
):
    """Elimina una regla de gasto programado."""
    return eliminar_gasto_programado(gasto_prog_id, tenant_id)


@router.post("/{gasto_prog_id}/ejecutar")
def ejecutar_gasto_programado_endpoint(
    gasto_prog_id: str,
    tenant_id: str = Depends(get_tenant_id)
):
    """Ejecuta inmediatamente una regla de gasto programado."""
    return ejecutar_gasto_programado(gasto_prog_id, tenant_id)


@router.get("/{gasto_prog_id}/estimacion")
def estimar_monto_endpoint(
    gasto_prog_id: str,
    tenant_id: str = Depends(get_tenant_id)
):
    """Calcula el monto estimado que generaría la regla."""
    return estimar_monto(gasto_prog_id, tenant_id)
