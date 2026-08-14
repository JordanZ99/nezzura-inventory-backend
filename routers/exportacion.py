# ==============================================================================
# backend/routers/exportacion.py
# Exportación de datos del tenant: respaldo JSON + vista Excel (XLSX).
#
# SEGURIDAD:
#   - Ambos endpoints usan Depends(get_tenant_id): el tenant_id sale SOLO del
#     JWT validado por Supabase. NUNCA se acepta un tenant_id proveniente del
#     cliente (body, query o header), así que aunque alguien manipule la
#     petición no puede descargar datos de otro tenant.
#   - Cada consulta filtra por tenant_id (ver database/exportacion.py).
#   - Rate limit simple en memoria (máx. 5 descargas/hora por tenant) para
#     evitar abuso de descargas.
# ==============================================================================

import datetime
import json
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Response

from dependencies import get_tenant_id
from database.exportacion import get_datos_tenant, generar_xlsx

router = APIRouter(prefix="/export", tags=["Exportación de datos"])

# ── Rate limit por tenant (en memoria) ───────────────────────────────────────
_LIMITE_DESCARGAS = 5      # descargas por ventana
_VENTANA_SEGUNDOS = 3600   # 1 hora

_descargas: dict[str, deque] = defaultdict(deque)


def _permitir_descarga(tenant_id: str) -> bool:
    """True si el tenant aún no superó el límite de descargas por hora."""
    ahora = time.time()
    cola = _descargas[tenant_id]
    # Limpiar descargas fuera de la ventana
    while cola and ahora - cola[0] > _VENTANA_SEGUNDOS:
        cola.popleft()
    if len(cola) >= _LIMITE_DESCARGAS:
        return False
    cola.append(ahora)
    return True


def _nombre_archivo(ext: str) -> str:
    fecha = datetime.date.today().isoformat()
    return f"nezzura-respaldo-{fecha}.{ext}"


@router.get("/json")
def exportar_json(tenant_id: str = Depends(get_tenant_id)):
    """
    Descarga el respaldo JSON con TODOS los datos del tenant autenticado.
    Envelope versionado para poder restaurarlo en el futuro.
    """
    if not _permitir_descarga(tenant_id):
        raise HTTPException(
            status_code=429,
            detail="Demasiadas descargas en la última hora. Inténtalo de nuevo más tarde.",
        )

    payload = {
        "version": 1,
        "app": "nezzura-digital",
        "exportado_en": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "tenant_id": tenant_id,
        "data": get_datos_tenant(tenant_id),
    }
    nombre = _nombre_archivo("json")
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )


@router.get("/xlsx")
def exportar_xlsx(tenant_id: str = Depends(get_tenant_id)):
    """
    Descarga un libro Excel (una hoja por tabla) con los datos del tenant,
    para que el cliente pueda verlos cómodamente.
    """
    if not _permitir_descarga(tenant_id):
        raise HTTPException(
            status_code=429,
            detail="Demasiadas descargas en la última hora. Inténtalo de nuevo más tarde.",
        )

    nombre = _nombre_archivo("xlsx")
    return Response(
        content=generar_xlsx(tenant_id),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
