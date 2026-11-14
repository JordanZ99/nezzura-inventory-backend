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
import os
import tempfile
import time
import urllib.request
import zipfile
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Response
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from dependencies import get_tenant_id
from database.exportacion import get_datos_tenant, generar_xlsx, _limpiar_para_json
from database.conexion import query
from database.almacenamiento import listar_imagenes

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


# ── Respaldo completo (datos + fotos) — migración 071, Fase B ────────────────
# El tenant al llegar a su límite debe poder descargar SU copia de seguridad
# y liberar espacio; sin las fotos, el respaldo está a medias.
#
# ZIP con:
#   datos.json        — el mismo envelope versionado de /export/json, más el
#                       snapshot del catálogo publicado (tenants Modo Local).
#   fotos/NNNN_*.ext  — cada imagen del tenant (peso real desde Cloudinary).
#   LEEME.txt         — qué contiene y qué fotos se omitieron (si las hubo).
#
# Seguro: el tenant_id sale SOLO del JWT; el ZIP se escribe a un archivo
# temporal del disco efímero de Render y se borra al terminar la respuesta
# (BackgroundTask). Tope de seguridad: 512 MB de fotos por descarga.

_TOPE_FOTOS_BYTES = 512 * 1024 * 1024
_TIMEOUT_FOTO = 20


def _slug(nombre: str) -> str:
    """Nombre de archivo legible a partir del contexto (producto/variación)."""
    base = "".join(c for c in (nombre or "foto") if c.isalnum() or c in " -_").strip()
    return (base[:60] or "foto").replace(" ", "_") or "foto"


def _extension_de_url(url: str) -> str:
    try:
        ruta = url.split("?", 1)[0].split("upload/")[-1]
        ext = ruta.rsplit(".", 1)[-1].lower() if "." in ruta else ""
    except Exception:
        ext = ""
    return f".{ext}" if ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp", "avif") else ".jpg"


@router.get("/respaldo")
def exportar_respaldo(tenant_id: str = Depends(get_tenant_id)):
    """
    Descarga el respaldo COMPLETO del tenant en un ZIP: datos (JSON) + todas
    sus fotos (Cloudinary). Es la "descarga tu copia de seguridad" del flujo
    de límite de almacenamiento.
    """
    if not _permitir_descarga(tenant_id):
        raise HTTPException(
            status_code=429,
            detail="Demasiadas descargas en la última hora. Inténtalo de nuevo más tarde.",
        )

    datos = get_datos_tenant(tenant_id)

    # El snapshot del catálogo publicado también va (tenants Modo Local:
    # su "nube" es esta tabla)
    try:
        filas_pub = query(
            "SELECT clave, hash, data, orden FROM catalogo_publicado "
            "WHERE tenant_id = %s ORDER BY orden ASC",
            (tenant_id,),
        )
        if filas_pub:
            datos["catalogo_publicado"] = _limpiar_para_json(filas_pub)
    except Exception as e:
        print(f"Respaldo: catálogo publicado omitido ({e})")

    inventario = listar_imagenes(tenant_id)
    fotos = [(i["url"], i.get("contexto") or "foto")
             for i in (inventario.get("imagenes") or [])]

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    omitidas: list[str] = []
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("datos.json", json.dumps(
                {
                    "version": 1,
                    "app": "nezzura-digital",
                    "exportado_en": datetime.datetime.now(
                        datetime.timezone.utc).isoformat(),
                    "tenant_id": tenant_id,
                    "data": datos,
                },
                ensure_ascii=False, indent=2,
            ))
            total = 0
            for indice, (url, contexto) in enumerate(fotos, start=1):
                if total >= _TOPE_FOTOS_BYTES:
                    omitidas.append(f"{url} (tope de {_TOPE_FOTOS_BYTES // (1024 * 1024)} MB alcanzado)")
                    continue
                try:
                    with urllib.request.urlopen(urllib.request.Request(url),
                                                timeout=_TIMEOUT_FOTO) as resp:
                        contenido = resp.read()
                except Exception as e:
                    omitidas.append(f"{url} ({e})")
                    continue
                if total + len(contenido) > _TOPE_FOTOS_BYTES:
                    omitidas.append(url)
                    continue
                nombre_foto = f"fotos/{indice:04d}_{_slug(contexto)}{_extension_de_url(url)}"
                # Las fotos ya vienen comprimidas: STORED evita recomprimir
                z.writestr(nombre_foto, contenido, compress_type=zipfile.ZIP_STORED)
                total += len(contenido)

            leeme = (
                "Respaldo completo de Nezzura Digital\n"
                "====================================\n\n"
                "datos.json : todos los datos de tu negocio (formato versionado).\n"
                "fotos/     : las imágenes de tus productos, galerías, variaciones,\n"
                "             logo, banners y catálogo publicado.\n\n"
                "Fechas: los nombres de archivo llevan el orden del inventario.\n"
            )
            if omitidas:
                leeme += (
                    "\nFotos omitidas (no se pudieron descargar o superaron el\n"
                    "tope de seguridad del respaldo):\n  - " + "\n  - ".join(omitidas) + "\n"
                )
            z.writestr("LEEME.txt", leeme)
    except Exception:
        # Limpiar el temporal si la construcción del ZIP falló a medias
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise

    fecha = datetime.date.today().isoformat()
    return FileResponse(
        tmp.name,
        media_type="application/zip",
        filename=f"nezzura-respaldo-completo-{fecha}.zip",
        background=BackgroundTask(os.unlink, tmp.name),
    )
