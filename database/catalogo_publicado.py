# ==============================================================================
# backend/database/catalogo_publicado.py
# Snapshot del catálogo publicado desde el Modo Local (migración 069).
#
# Los tenants del plan local no tienen datos en la nube: su catálogo público
# vive aquí, en filas subidas por DELTA desde el dispositivo (solo filas que
# cambiaron; borrados llegan con data=None). La lectura la hace
# database/catalogo.py (obtener_catalogo_publico) al detectar snapshot.
#
# El tenant_id SIEMPRE sale del JWT de licencia (guard en el router) —
# nunca del body.
# ==============================================================================

import json
import uuid

import cloudinary

from database.conexion import execute, query
from database.catalogo import _asegurar_config


# Tamaño máximo de foto para el catálogo (mismo límite de seguridad que
# subir_foto: el frontend local comprime a ~80 KB antes de subir).
MAX_FOTO_KB = 1024


def aplicar_delta(tenant_id: str, filas: list) -> dict:
    """
    Aplica el delta de publicación del dispositivo: upsert de filas nuevas/
    modificadas y DELETE de las que llegan con data=None (producto borrado
    u oculto del catálogo localmente).

    Cada fila: {"clave": str, "hash": str, "data": dict|None, "orden": int}.

    En el primer publish asegura catalogo_config y deja activo=TRUE: sin eso
    GET /p/{slug} respondería 404 aunque el snapshot exista (la respuesta
    pública exige activo=true).
    """
    if not isinstance(filas, list):
        return {"ok": False, "status": 422, "mensaje": "Se esperaba una lista de filas."}

    aplicadas = 0
    eliminadas = 0
    for fila in filas:
        clave = (fila.get("clave") or "").strip()
        if not clave:
            return {"ok": False, "status": 422, "mensaje": "Fila sin clave."}

        data = fila.get("data")
        if data is None:
            execute(
                "DELETE FROM catalogo_publicado WHERE tenant_id = %s AND clave = %s",
                (tenant_id, clave),
            )
            eliminadas += 1
            continue

        if not isinstance(data, dict):
            return {"ok": False, "status": 422, "mensaje": f"Data inválida en '{clave}'."}

        hash_nuevo = str(fila.get("hash") or "")
        orden = int(fila.get("orden") or 0)
        execute(
            """
            INSERT INTO catalogo_publicado (tenant_id, clave, hash, data, orden)
            VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (tenant_id, clave) DO UPDATE
               SET hash = EXCLUDED.hash,
                   data = EXCLUDED.data,
                   orden = EXCLUDED.orden,
                   publicado_en = now()
            """,
            (tenant_id, clave, hash_nuevo, json.dumps(data), orden),
        )
        aplicadas += 1

    # El publish explícito habilita el catálogo público del tenant (si el
    # dueño lo quiere ocultar, lo hace desde el gestor; no con un delta).
    _asegurar_config(tenant_id)
    execute(
        "UPDATE catalogo_config SET activo = TRUE WHERE tenant_id = %s AND activo IS NOT TRUE",
        (tenant_id,),
    )

    slug = None
    filas_cfg = query("SELECT slug FROM catalogo_config WHERE tenant_id = %s", (tenant_id,))
    if filas_cfg:
        slug = filas_cfg[0].get("slug")

    return {"ok": True, "aplicadas": aplicadas, "eliminadas": eliminadas, "slug": slug}


def obtener_snapshot_publico(tenant_id: str) -> list:
    """Filas del snapshot del tenant (o [] si nunca publicó): la consume
    obtener_catalogo_publico para servir el catálogo dibujando desde datos."""
    return query(
        "SELECT clave, hash, data, orden, publicado_en "
        "FROM catalogo_publicado WHERE tenant_id = %s ORDER BY orden ASC, clave ASC",
        (tenant_id,),
    )


def subir_foto_catalogo(tenant_id: str, contents: bytes, nombre: str = "") -> dict:
    """
    Sube una foto del catálogo local (dataURL decodificado en el cliente) a
    Cloudinary y devuelve la URL segura. En la fila publicada viaja SOLO la
    URL: el catálogo queda ligero para quien lo mira (CDN) y la DB local
    nunca sube dataURLs de MBs.

    Carpeta dedicada por tenant: catalogo_publicado/{tenant_id} — el audit de
    imágenes y un futuro borrado por tenant son triviales.
    """
    if not contents or len(contents) == 0:
        raise ErrorLicencia(400, "La imagen recibida está vacía (0 bytes).", "FOTO_INVALIDA")

    if len(contents) / 1024 > MAX_FOTO_KB:
        raise ErrorLicencia(
            413,
            f"La imagen es demasiado grande ({len(contents) / 1024:.0f} KB). "
            f"El máximo permitido es {MAX_FOTO_KB} KB.",
            "FOTO_GRANDE",
        )

    from database.imagenes import _es_imagen_valida
    from database.almacenamiento import reservar, liberar_reserva, confirmar

    if not _es_imagen_valida(contents):
        raise ErrorLicencia(
            400,
            "El archivo no es una imagen válida (solo JPG, PNG, GIF, WebP, BMP, AVIF o HEIC).",
            "FOTO_INVALIDA",
        )

    # Cuota de almacenamiento (migración 071): el Modo Local es un MODO dentro
    # de básico/plus, y las fotos que publica pesan igual en el plan. Sin
    # crédito de reemplazo: el cliente reutiliza la URL cuando el hash no
    # cambió, así que cada subida aquí es peso nuevo.
    reserva = reservar(tenant_id, len(contents))
    if not reserva.get("ok"):
        raise ErrorLicencia(
            reserva.get("status", 422),
            reserva.get("mensaje", "Tu espacio está lleno. Libera espacio o mejora a Plus."),
            "CUOTA_LLENA",
        )

    # public_id legible a partir del nombre (producto/variación), saneado.
    base = "".join(c for c in (nombre or "foto") if c.isalnum() or c in " -_").strip()
    base = (base[:60] or "foto").replace(" ", "_")

    try:
        resultado = cloudinary.uploader.upload(
            contents,
            folder=f"catalogo_publicado/{tenant_id}",
            public_id=f"{base}_{uuid.uuid4().hex[:8]}",
            quality="auto:best",
            fetch_format="auto",
        )
    except Exception as e:
        liberar_reserva(tenant_id, len(contents))
        raise ErrorLicencia(500, f"Error subiendo a Cloudinary: {str(e)}", "FOTO_CLOUDINARY")

    confirmar(
        tenant_id,
        resultado.get("secure_url"),
        len(contents),
        resultado.get("bytes"),
        "Catálogo publicado (Modo Local)",
    )

    return {"ok": True, "ruta": resultado.get("secure_url")}
