# ==============================================================================
# backend/routers/analitica.py
# Análisis Inteligente: reporte determinista del negocio del tenant.
# Exclusivo del plan Plus. Solo lectura. El período sigue ?desde/?hasta/?todo,
# igual que /stats.
# ==============================================================================

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from analisis.reglas import evaluar
from database.analitica import generar
from database.helpers import resolver_rango_q, ventana_ts_de_rango
from database.imagenes import _get_tenant_plan
from dependencies import get_tenant_id

router = APIRouter(prefix="/analitica", tags=["Análisis Inteligente"])


@router.get("/resumen")
def resumen(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    todo: bool = False,
    tenant_id: str = Depends(get_tenant_id),
):
    """Métricas + hallazgos del período. 403 si el plan no es Plus."""
    if _get_tenant_plan(tenant_id) != "plus":
        raise HTTPException(
            status_code=403,
            detail="El Análisis Inteligente es exclusivo del plan Plus.",
        )
    if todo:
        desde_d, hasta_d = None, None
    else:
        try:
            desde_d, hasta_d = resolver_rango_q(tenant_id, desde, hasta)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    desde_ts, hasta_ts = ventana_ts_de_rango(tenant_id, desde_d, hasta_d)
    paquete = generar(tenant_id, desde_ts, hasta_ts, desde_d, hasta_d)
    hechos = paquete.pop("hechos")
    paquete["hallazgos"] = evaluar(hechos)
    return paquete
