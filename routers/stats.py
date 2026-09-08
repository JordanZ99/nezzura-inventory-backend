# ==============================================================================
# backend/routers/stats.py
# Endpoints de agregados para el panel de Estadísticas (respuestas de la BDD,
# no dumps de filas). Todos aceptan ?desde/?hasta ('YYYY-MM-DD'); sin rango
# devuelven el mes contable actual; con ?todo=true el histórico completo.
# ==============================================================================

from datetime import date
from typing import Optional

from database import stats
from database.helpers import hoy_negocio, resolver_rango_q, ventana_ts_de_rango
from dependencies import get_tenant_id
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/stats", tags=["Estadísticas"])

_GRANULARIDADES = ("dia", "semana", "mes")
_LIMITE_TOP_MAX = 25


def _rango_o_todo(tenant_id: str, desde: Optional[str], hasta: Optional[str], todo: bool) -> tuple[date, Optional[date]]:
    """Rango [inicio, fin_exclusiva) pedido; ?todo=true abre el histórico."""
    if todo:
        return date(1970, 1, 1), None
    try:
        return resolver_rango_q(tenant_id, desde, hasta)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _granularidad(tenant_id: str, desde_d: Optional[date], hasta_d: Optional[date], pedida: str) -> str:
    """Valida la granularidad pedida; 'auto' la elige por el ancho del rango
    (≤2 meses → día, ≤2 años → semana, resto → mes)."""
    if pedida in _GRANULARIDADES:
        return pedida
    if pedida != "auto":
        raise HTTPException(status_code=422, detail=f"granularidad inválida: usa auto|{'|'.join(_GRANULARIDADES)}")
    if desde_d is None:
        return "mes"  # histórico abierto → cubos mensuales
    fin = hasta_d if hasta_d else hoy_negocio(tenant_id)
    dias = (fin - desde_d).days
    if dias <= 62:
        return "dia"
    if dias <= 730:
        return "semana"
    return "mes"


@router.get("/resumen")
def resumen(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    todo: bool = False,
    limite_top: int = 10,
    tenant_id: str = Depends(get_tenant_id),
):
    """KPIs del período calculados en SQL (respuestas, no renglones)."""
    limite_top = max(1, min(limite_top, _LIMITE_TOP_MAX))
    d, h = _rango_o_todo(tenant_id, desde, hasta, todo)
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, d, h)
    return stats.resumen_periodo(tenant_id, desde_ts, hasta_ts, d, h, limite_top)


@router.get("/serie")
def serie(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    todo: bool = False,
    granularidad: str = "auto",
    tenant_id: str = Depends(get_tenant_id),
):
    """Serie temporal en cubos (día/semana/mes) para las gráficas: la granularidad
    'auto' mantiene las gráficas fluidas sin importar la edad del negocio."""
    d, h = _rango_o_todo(tenant_id, desde, hasta, todo)
    g = _granularidad(tenant_id, d, h, granularidad)
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, d, h)
    return stats.serie_periodo(tenant_id, g, desde_ts, hasta_ts, d, h)


@router.get("/productos")
def productos_agregados(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    todo: bool = False,
    tenant_id: str = Depends(get_tenant_id),
):
    """Totales por producto del período (un renglón por producto vendido)."""
    d, h = _rango_o_todo(tenant_id, desde, hasta, todo)
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, d, h)
    return stats.stats_productos_periodo(tenant_id, desde_ts, hasta_ts)


@router.get("/ventas-producto")
def ventas_de_producto(
    producto: str,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    todo: bool = False,
    tenant_id: str = Depends(get_tenant_id),
):
    """Renglones de venta de UN producto en el período (modal de detalle)."""
    d, h = _rango_o_todo(tenant_id, desde, hasta, todo)
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, d, h)
    return stats.ventas_producto_periodo(tenant_id, producto, desde_ts, hasta_ts)


@router.get("/clientes")
def resumen_clientes(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    todo: bool = False,
    granularidad: str = "auto",
    tenant_id: str = Depends(get_tenant_id),
):
    """KPIs de la cartera de clientes + programa de puntos (tab "Clientes"):
    incluye sala de honor (top_clientes) y serie temporal de cómo influye en
    las ventas (granularidad 'auto' según el ancho del rango)."""
    d, h = _rango_o_todo(tenant_id, desde, hasta, todo)
    g = _granularidad(tenant_id, d, h, granularidad)
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, d, h)
    return stats.resumen_clientes(tenant_id, desde_ts, hasta_ts, granularidad=g)
