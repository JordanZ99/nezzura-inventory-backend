# ==============================================================================
# backend/routers/gastos_programados.py
# Endpoints para gastos programados (suscripciones recurrentes).
# ==============================================================================

from typing import Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from dependencies import get_tenant_id
from database.conexion import query
from database.gastos_programados import crear_gasto_programado, ejecutar_gasto_programado

router = APIRouter(prefix="/gastos_programados", tags=["Gastos Programados"])


class GastoProgramadoOut(BaseModel):
    id: str
    tenant_id: str
    nombre: str
    tipo: str                 # "fijo" | "porcentaje"
    valor: float
    frecuencia: str           # "semanal" | "mensual" | "anual"
    proxima_fecha: str
    created_at: Optional[str] = None


class NuevoGastoProgramado(BaseModel):
    nombre: str
    tipo: str                          # "fijo" | "porcentaje"
    valor: float
    frecuencia: str                    # "semanal" | "mensual" | "anual"
    proxima_fecha: str


@router.get("")
def listar_gastos_programados(tenant_id: str = Depends(get_tenant_id)):
    """Retorna todas las reglas de gastos programados del tenant."""
    resultado = query(
        "SELECT id, tenant_id, nombre, tipo, valor, frecuencia, "
        "proxima_fecha, created_at "
        "FROM gastos_programados "
        "WHERE tenant_id = %s "
        "ORDER BY proxima_fecha ASC, created_at DESC",
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
        tipo=data.tipo,
        valor=data.valor,
        frecuencia=data.frecuencia,
        proxima_fecha=data.proxima_fecha,
        tenant_id=tenant_id,
    )


@router.post("/{regla_id}/ejecutar")
def ejecutar_regla_endpoint(
    regla_id: str,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Ejecuta manualmente una regla de gasto programado.
    Calcula el monto (fijo o porcentaje sobre ventas), inserta un gasto
    y actualiza ultima_ejecucion + proxima_fecha de la regla.
    """
    return ejecutar_gasto_programado(regla_id, tenant_id)
