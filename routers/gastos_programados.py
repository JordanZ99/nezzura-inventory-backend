# ==============================================================================
# backend/routers/gastos_programados.py
# Endpoints para gastos programados (suscripciones recurrentes).
#
# Bugs corregidos:
#   6. Validación de tipo/frecuencia con Enum (rechaza strings inválidos)
#   7. Añadidos endpoints DELETE y PUT para editar/eliminar reglas
# ==============================================================================

from typing import Optional
from enum import Enum
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from dependencies import get_tenant_id
from database.conexion import query
from database.gastos_programados import (
    crear_gasto_programado, ejecutar_gasto_programado,
    eliminar_gasto_programado, actualizar_gasto_programado
)

router = APIRouter(prefix="/gastos_programados", tags=["Gastos Programados"])


# ── Enums para validación de entrada (Bug 6) ──
# Pydantic valida automáticamente que el string coincida con uno de estos
# valores. Si el frontend envía "porcentual" en vez de "porcentaje",
# FastAPI devuelve 422 antes de llegar a la lógica de negocio.
class TipoGasto(str, Enum):
    fijo = "fijo"
    porcentaje = "porcentaje"


class FrecuenciaGasto(str, Enum):
    semanal = "semanal"
    mensual = "mensual"
    anual = "anual"


class GastoProgramadoOut(BaseModel):
    id: str
    tenant_id: str
    nombre: str
    tipo: str
    valor: float
    frecuencia: str
    proxima_fecha: str
    created_at: Optional[str] = None


class NuevoGastoProgramado(BaseModel):
    nombre: str
    tipo: TipoGasto                    # Enum: valida "fijo" | "porcentaje"
    valor: float
    frecuencia: FrecuenciaGasto        # Enum: valida "semanal" | "mensual" | "anual"
    proxima_fecha: str


class ActualizarGastoProgramado(BaseModel):
    """
    Modelo para edición parcial (PATCH).
    Todos los campos son opcionales — solo se actualizan los que vengan.
    """
    nombre: Optional[str] = None
    tipo: Optional[TipoGasto] = None
    valor: Optional[float] = None
    frecuencia: Optional[FrecuenciaGasto] = None
    proxima_fecha: Optional[str] = None


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
        tipo=data.tipo.value,           # .value extrae el string del Enum
        valor=data.valor,
        frecuencia=data.frecuencia.value,
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
    Calcula el monto (fijo o porcentaje sobre ganancia neta del período),
    inserta un gasto y actualiza ultima_ejecucion + proxima_fecha.

    Idempotencia: rechaza la ejecución si proxima_fecha > hoy.
    """
    return ejecutar_gasto_programado(regla_id, tenant_id)


@router.put("/{regla_id}")
def actualizar_regla_endpoint(
    regla_id: str,
    data: ActualizarGastoProgramado,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Edita una regla de gasto programado (PATCH parcial).
    Solo actualiza los campos que vengan en el body.
    """
    return actualizar_gasto_programado(
        regla_id=regla_id,
        tenant_id=tenant_id,
        nombre=data.nombre,
        tipo=data.tipo.value if data.tipo else None,
        valor=data.valor,
        frecuencia=data.frecuencia.value if data.frecuencia else None,
        proxima_fecha=data.proxima_fecha,
    )


@router.delete("/{regla_id}")
def eliminar_regla_endpoint(
    regla_id: str,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Elimina una regla de gasto programado.
    Los gastos ya generados por esta regla NO se eliminan (quedan como
    historial en la tabla gastos).
    """
    return eliminar_gasto_programado(regla_id, tenant_id)
