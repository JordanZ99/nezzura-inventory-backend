# ==============================================================================
# backend/database/imagenes.py
# Capa de imágenes: integración con Cloudinary y lógica de la galería (Plan Plus).
#
# Refactor de routers/inventario.py (Fase 1/2): la capa de Cloudinary y los
# helpers de imágenes viven aquí, fuera del router. El router mapea los dicts
# de dominio a HTTPException.
# ==============================================================================

import os
import re
import uuid
from urllib.parse import unquote

from psycopg2.extras import RealDictCursor

from database.conexion import query, execute, get_conn, release_conn


class _ErrorGaleria(Exception):
    """Error de dominio de la galería: status HTTP + mensaje para el usuario.
    Las funciones de dominio lo lanzan internamente y devuelven
    {"ok": False, "status": ..., "mensaje": ...}; el router lo mapea a
    HTTPException."""

    def __init__(self, status: int, mensaje: str):
        self.status = status
        self.mensaje = mensaje

# ── Integración con Cloudinary ──────────────────────────────────────────────
# Cloudinary reemplaza el almacenamiento local en disco.
# Motivo: Render tiene filesystem efímero que borra los archivos en cada
# deploy, lo que provocaba que las fotos de productos desaparecieran y
# se mostraran como iconos de imagen rota en el frontend.
# Cloudinary sirve las imágenes desde su CDN global con URL persistente.
import cloudinary
import cloudinary.uploader
import cloudinary.api

# Configuración con credenciales desde variables de entorno.
# En Render estas variables deben estar definidas:
#   CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET")
)


_TRANSFORM_RE = re.compile(r"^[a-z]{1,4}_[\w:.%+-]+$", re.IGNORECASE)


def _es_segmento_transformacion(seg: str) -> bool:
    """
    True si el segmento es una cadena de transformación de Cloudinary:
    una o más claves "param_valor" separadas por coma (ej. "w_600,c_fill,q_auto").

    IMPORTANTE: un public_id cuyo nombre contiene comas (ej. "Pizza, especial_ab12")
    NO se considera transformación. Antes se saltaba cualquier segmento con coma
    y destroy() recibía un public_id incompleto, fallando en silencio y dejando
    la foto para siempre en Cloudinary.
    """
    if not seg:
        return False
    return all(bool(_TRANSFORM_RE.match(p)) for p in seg.split(","))


def _es_imagen_valida(contenido: bytes) -> bool:
    """
    Valida los "magic bytes" del archivo para aceptar solo imágenes reales
    (JPG, PNG, GIF, WebP, BMP, AVIF/HEIC). El frontend siempre comprime a
    JPEG antes de subir; esto es una red de seguridad contra llamadas directas
    a la API con archivos que no son imágenes.
    """
    if not contenido or len(contenido) < 12:
        return False
    if contenido[:3] == b"\xff\xd8\xff":                 # JPEG
        return True
    if contenido[:8] == b"\x89PNG\r\n\x1a\n":            # PNG
        return True
    if contenido[:6] in (b"GIF87a", b"GIF89a"):          # GIF
        return True
    if contenido[:4] == b"RIFF" and contenido[8:12] == b"WEBP":  # WebP
        return True
    if contenido[:2] == b"BM":                            # BMP
        return True
    if contenido[4:8] == b"ftyp":                        # AVIF / HEIC (ISO-BMFF)
        marca = contenido[8:16]
        if any(b in marca for b in (b"avif", b"avis", b"heic", b"heix", b"mif1", b"heim", b"heis")):
            return True
    return False


def _extraer_public_id(url: str) -> str | None:
    """
    Extrae el public_id de una URL de Cloudinary, manejando prefijos de
    versión (v123/) y cadenas de transformación (w_600,f_auto,q_auto/).

    Formato esperado:
      .../image/upload/[transformaciones/][v{version}/]{folder}/{public_id}.{ext}

    Devuelve None si la URL no es de Cloudinary o no se puede parsear.
    """
    try:
        if "res.cloudinary.com" not in url:
            return None
        partes = url.split("/upload/")
        if len(partes) < 2:
            return None
        ruta = partes[1]
        # Quitar query string si existe
        if "?" in ruta:
            ruta = ruta.split("?", 1)[0]
        segmentos = ruta.split("/")
        # Saltar prefijos de transformación y de versión (v123/). El último
        # segmento (public_id.ext) nunca se salta: es la foto en sí.
        i = 0
        while i < len(segmentos) - 1:
            seg = segmentos[i]
            if re.match(r"^v\d+$", seg) or _es_segmento_transformacion(seg):
                i += 1
            else:
                break
        public_id = "/".join(segmentos[i:]).rsplit(".", 1)[0]
        # Decodificar caracteres URL-encoded: los public_id se generan con el
        # nombre del producto (ej. 'Tortilla de maíz'), así que la URL guardada
        # trae %20 / %C3%AD en vez de espacios/acentos. Sin este decode,
        # destroy() recibe un public_id que no existe y falla en silencio.
        return unquote(public_id) or None
    except Exception:
        return None


def borrar_imagen_cloudinary(url: str | None) -> None:
    """
    Borra una imagen de Cloudinary por su URL (best-effort).
    Si la URL no es de Cloudinary o el borrado falla, no lanza excepción:
    el flujo principal de la app nunca debe romperse por limpieza de fotos.
    """
    if not url or "res.cloudinary.com" not in url:
        return
    public_id = _extraer_public_id(url)
    if not public_id:
        return
    try:
        resultado = cloudinary.uploader.destroy(public_id)
        estado = (resultado or {}).get("result")
        # 'ok' = borrada. Cualquier otro estado ('not_found', 'error', ...) se
        # loguea: así dejamos de fallar en silencio y detectamos public_ids mal
        # extraídos o fotos ya borradas (refs duplicadas). Nunca bloquea el flujo.
        if estado and estado != "ok":
            print(f"Cloudinary: destroy devolvió '{estado}' para {public_id} (url: {url})")
    except Exception as e:
        print(f"Error borrando imagen de Cloudinary ({public_id}): {e}")


def _url_sigue_referenciada(url: str, tenant_id: str) -> bool:
    """
    True si la URL todavía está referenciada en alguna tabla del tenant
    (producto, galería, variación, logo o banner).

    Se consulta ANTES de destruir un asset en Cloudinary: nunca se borra una
    imagen que sigue en uso. Esto evita la clase de bug donde una misma URL
    compartida entre productos.imagen y producto_imagenes se destruía y
    dejaba fotos rotas (negras) en el catálogo público.

    Ante un error de BD devuelve True (ante la duda, no borrar).
    """
    if not url or "res.cloudinary.com" not in url:
        return False
    try:
        fila = query(
            "SELECT 1 FROM productos WHERE Imagen=%s AND tenant_id=%s "
            "UNION SELECT 1 FROM producto_imagenes WHERE url=%s AND tenant_id=%s "
            "UNION SELECT 1 FROM producto_variaciones WHERE foto=%s AND tenant_id=%s "
            "UNION SELECT 1 FROM tenants WHERE id=%s AND logo=%s "
            "UNION SELECT 1 FROM catalogo_config WHERE tenant_id=%s AND (banner_url=%s OR banner_url_movil=%s) "
            "LIMIT 1",
            (url, tenant_id, url, tenant_id, url, tenant_id, tenant_id, url, tenant_id, url, url)
        )
        return bool(fila)
    except Exception as e:
        print(f"Error comprobando si la URL sigue referenciada: {e}")
        return True


def _sincronizar_principal_galeria(producto_id: int, nueva_url: str, tenant_id: str) -> None:
    """
    Mantiene la foto principal (productos.imagen) reflejada en la galería
    (producto_imagenes, orden 1) para tenants Plus.

    Así la principal es UNA SOLA fila con id dentro de la galería: el editor
    no la duplica y el reordenamiento no la pisa con una URL vieja.

    Casos:
      - nueva_url vacía o "No hay foto": se elimina la fila de orden 1 (la
        galería promueve la siguiente al borrarla).
      - La URL ya existe en OTRA posición (el usuario arrastró una foto al
        primer puesto): se intercambian (swap) la fila de orden 1 y la que ya
        tenía esa URL, para que ninguna foto se pierda.
      - No existe fila de orden 1: se inserta; si existe, se actualiza.
    """
    if not nueva_url or nueva_url == "No hay foto":
        execute(
            "DELETE FROM producto_imagenes WHERE producto_id=%s AND tenant_id=%s AND orden=1",
            (producto_id, tenant_id)
        )
        return

    fila1 = query(
        "SELECT id, url FROM producto_imagenes "
        "WHERE producto_id=%s AND tenant_id=%s AND orden=1",
        (producto_id, tenant_id)
    )
    if fila1 and fila1[0].get("url") == nueva_url:
        return  # ya sincronizada

    fila_otra = query(
        "SELECT id FROM producto_imagenes "
        "WHERE producto_id=%s AND tenant_id=%s AND url=%s AND orden != 1",
        (producto_id, tenant_id, nueva_url)
    )
    if fila_otra:
        if fila1:
            # Swap: la fila 1 pasa a la URL nueva; la fila que la tenía
            # recupera la URL anterior de la principal (rotación, sin perder
            # ninguna foto).
            execute(
                "UPDATE producto_imagenes SET url=%s WHERE id=%s",
                (nueva_url, fila1[0]["id"])
            )
            execute(
                "UPDATE producto_imagenes SET url=%s WHERE id=%s",
                (fila1[0]["url"], fila_otra[0]["id"])
            )
        else:
            # No hay fila 1: la fila que ya tiene la URL sube a orden 1
            execute(
                "UPDATE producto_imagenes SET orden=1 WHERE id=%s",
                (fila_otra[0]["id"],)
            )
        return

    if fila1:
        execute(
            "UPDATE producto_imagenes SET url=%s WHERE id=%s",
            (nueva_url, fila1[0]["id"])
        )
    else:
        execute(
            "INSERT INTO producto_imagenes (producto_id, tenant_id, url, orden) "
            "VALUES (%s, %s, %s, 1)",
            (producto_id, tenant_id, nueva_url)
        )


def _get_tenant_plan(tenant_id: str) -> str:
    """
    Helper interno: obtiene el plan del tenant desde la tabla tenants.
    Devuelve 'basico' por defecto si el tenant no tiene plan asignado
    o si ocurre algún error consultando la base de datos.
    Esto nunca debe bloquear la app — si falla, se asume plan básico
    (menos privilegios) por seguridad.
    """
    try:
        resultado = query("SELECT plan FROM tenants WHERE id = %s", (tenant_id,))
        if resultado and resultado[0].get("plan"):
            return resultado[0]["plan"]
    except Exception as e:
        print(f"Error obteniendo plan del tenant: {e}")
    return "basico"


def _get_producto_id(producto_nombre: str, tenant_id: str) -> int:
    """
    Helper interno: obtiene el ID numérico de un producto por su nombre.
    Las tablas lotes y ventas referencian productos por nombre (text),
    pero producto_imagenes usa FK a productos.id (integer).
    Este helper hace el puente entre ambos sistemas.
    """
    resultado = query(
        "SELECT id FROM productos WHERE Producto = %s AND tenant_id = %s",
        (producto_nombre, tenant_id)
    )
    if not resultado:
        raise _ErrorGaleria(404, "Producto no encontrado")
    return resultado[0]["id"]


# ==============================================================================
# ── Lógica de negocio de imágenes (movida desde routers/inventario.py) ───────
# Las funciones devuelven dicts de dominio: {"ok": True, ...} en éxito o
# {"ok": False, "status": <código HTTP>, "mensaje": <detalle>} en error.
# El router mapea los errores a HTTPException; aquí nunca se lanza FastAPI.
# ==============================================================================


def subir_foto(producto: str, contents: bytes, tenant_id: str) -> dict:
    """
    Sube la foto de un producto a Cloudinary y devuelve la URL segura.

    Validaciones de seguridad:
    - El archivo no debe exceder 1 MB (1024 KB). Esto es una red de seguridad,
      ya que el frontend ya comprime las imágenes a ~80 KB como máximo.
    - Se pasa la opción quality=auto a Cloudinary para que optimice aún más
      el peso del archivo sin pérdida de calidad visible.
    """
    try:
        # Validar que el contenido no esté vacío
        if not contents or len(contents) == 0:
            return {"ok": False, "status": 400, "mensaje": "La imagen recibida está vacía (0 bytes)"}

        # Validar magic bytes: solo se aceptan imágenes reales
        if not _es_imagen_valida(contents):
            return {
                "ok": False, "status": 400,
                "mensaje": "El archivo no es una imagen válida (solo JPG, PNG, GIF, WebP, BMP, AVIF o HEIC)."
            }

        # Obtenemos el tamaño en kilobytes para validación
        tamano_kb = len(contents) / 1024

        # Límite de seguridad: rechazamos archivos mayores a 1 MB.
        # El frontend comprime a ~80 KB, pero validamos en backend por si
        # alguien llama la API directamente sin pasar por el frontend.
        MAX_TAMANO_KB = 1024  # 1 MB
        if tamano_kb > MAX_TAMANO_KB:
            return {
                "ok": False, "status": 413,
                "mensaje": f"La imagen es demasiado grande ({tamano_kb:.0f} KB). "
                           f"El máximo permitido es {MAX_TAMANO_KB} KB. "
                           "El frontend comprime automáticamente las imágenes "
                           "antes de subirlas para evitar este error."
            }

        # Los logos y banners se suben con nombres clave _logo_{id} / _banner_{id}:
        # van a su propia carpeta para no ensuciar la de fotos de producto.
        carpeta = "productos"
        if producto.startswith("_logo_") or producto.startswith("_banner"):
            carpeta = "branding"

        # Subimos a Cloudinary con optimización automática de calidad.
        # folder="productos" agrupa todas las fotos en una carpeta en Cloudinary.
        # public_id usa el nombre del producto + un hex aleatorio para unicidad.
        # quality="auto:best" permite que Cloudinary optimice el peso del
        # archivo automáticamente sin pérdida de calidad visible.
        # fetch_format="auto" convierte automáticamente al formato más
        # eficiente (WebP en navegadores modernos, JPEG en el resto).
        resultado = cloudinary.uploader.upload(
            contents,
            folder=carpeta,
            public_id=f"{producto}_{uuid.uuid4().hex[:8]}",
            quality="auto:best",
            fetch_format="auto"
        )
        # Cloudinary devuelve una URL HTTPS persistente que se guarda en la DB.
        # Esta URL sobrevive a deploys de Render porque vive en el CDN de Cloudinary.
        return {"ok": True, "ruta": resultado.get("secure_url")}

    except Exception as e:
        return {"ok": False, "status": 500, "mensaje": f"Error subiendo a Cloudinary: {str(e)}"}


def subir_foto_variacion(variacion_id: int, contents: bytes, tenant_id: str) -> dict:
    """
    Sube (o reemplaza) la foto propia de una variación a Cloudinary.
    - Valida que la variación pertenezca al tenant.
    - Sube a la carpeta "variaciones" y borra la foto anterior de Cloudinary.
    - Devuelve {ok, url}.
    """
    fila = query(
        "SELECT foto FROM producto_variaciones WHERE id=%s AND tenant_id=%s",
        (variacion_id, tenant_id)
    )
    if not fila:
        return {"ok": False, "status": 404, "mensaje": "Variación no encontrada"}

    if not contents or len(contents) == 0:
        return {"ok": False, "status": 400, "mensaje": "La imagen recibida está vacía (0 bytes)"}
    if not _es_imagen_valida(contents):
        return {
            "ok": False, "status": 400,
            "mensaje": "El archivo no es una imagen válida (solo JPG, PNG, GIF, WebP, BMP, AVIF o HEIC)."
        }
    tamano_kb = len(contents) / 1024
    if tamano_kb > 1024:
        return {
            "ok": False, "status": 413,
            "mensaje": f"La imagen es demasiado grande ({tamano_kb:.0f} KB). Máximo 1024 KB."
        }

    resultado = cloudinary.uploader.upload(
        contents,
        folder="variaciones",
        public_id=f"var_{variacion_id}_{uuid.uuid4().hex[:8]}",
        quality="auto:best",
        fetch_format="auto"
    )
    url = resultado.get("secure_url")
    execute(
        "UPDATE producto_variaciones SET foto=%s WHERE id=%s AND tenant_id=%s",
        (url, variacion_id, tenant_id)
    )

    old = fila[0].get("foto") or ""
    if old and old != url:
        borrar_imagen_cloudinary(old)

    return {"ok": True, "url": url}


def borrar_imagen_url(url: str, tenant_id: str) -> dict:
    """
    Borra una imagen de Cloudinary tras ser reemplazada o eliminada por el
    tenant (logo, banners de escritorio/móvil). Best-effort: si falla, no
    rompe el flujo (la app ya no depende de la foto vieja).

    Guardia de propiedad multitenant: solo borra si el public_id incluye el
    prefijo del tenant (el logo/banner se suben con _logo_{id}/_banner_{id})
    o si la URL está referenciada en las tablas del propio tenant.
    """
    url = (url or "").strip()
    if not url or "res.cloudinary.com" not in url:
        return {"ok": True, "mensaje": "Nada que borrar"}

    public_id = _extraer_public_id(url) or ""
    corto = tenant_id[:8]
    # El logo/banner se suben con public_id _logo_{id}/_banner_{id}_{hex} → prefijo _{corto}_
    pertenece = f"_{corto}_" in public_id
    if not pertenece:
        fila = query(
            "SELECT 1 FROM productos WHERE Imagen=%s AND Tenant_ID=%s "
            "UNION SELECT 1 FROM producto_imagenes WHERE url=%s AND tenant_id=%s "
            "UNION SELECT 1 FROM producto_variaciones WHERE foto=%s AND tenant_id=%s "
            "UNION SELECT 1 FROM tenants WHERE id=%s AND logo=%s "
            "UNION SELECT 1 FROM catalogo_config WHERE tenant_id=%s AND (banner_url=%s OR banner_url_movil=%s) "
            "LIMIT 1",
            (url, tenant_id, url, tenant_id, url, tenant_id, tenant_id, url, tenant_id, url, url)
        )
        if not fila:
            return {"ok": False, "mensaje": "La imagen no pertenece a este tenant"}

    borrar_imagen_cloudinary(url)
    return {"ok": True, "mensaje": "Imagen borrada de Cloudinary"}


def listar_imagenes_producto(producto: str, tenant_id: str) -> list:
    """
    Devuelve la galería completa de imágenes de un producto.
    Para Plan Plus: hasta 5 imágenes unificadas (la orden 1 es la principal).
    Para Plan básico: devuelve una lista vacía (el frontend usa ImagePicker simple).
    """
    producto_id = _get_producto_id(producto, tenant_id)
    resultado = query(
        "SELECT id, url, orden FROM producto_imagenes "
        "WHERE producto_id = %s AND tenant_id = %s "
        "ORDER BY orden ASC",
        (producto_id, tenant_id)
    )
    return resultado


def subir_imagen_extra(producto: str, contents: bytes, orden_target, tenant_id: str) -> dict:
    """
    Sube una imagen a la galería de un producto (Plan Plus).
    - Si orden_target es None: añade una imagen nueva (siguiente slot disponible).
    - Si orden_target es 1-5: reemplaza la imagen en esa posición (borra la anterior de Cloudinary).
    Máximo 5 imágenes por producto.
    La orden 1 se sincroniza automáticamente con productos.imagen.
    """
    # 1. Validar plan plus
    plan = _get_tenant_plan(tenant_id)
    if plan != "plus":
        return {"ok": False, "status": 403, "mensaje": "La galería de imágenes es exclusiva del plan Plus."}

    # 2. Obtener el ID del producto
    try:
        producto_id = _get_producto_id(producto, tenant_id)
    except _ErrorGaleria as e:
        return {"ok": False, "status": e.status, "mensaje": e.mensaje}

    # 3. Validar el archivo
    if not contents or len(contents) == 0:
        return {"ok": False, "status": 400, "mensaje": "La imagen recibida está vacía (0 bytes)"}
    if not _es_imagen_valida(contents):
        return {
            "ok": False, "status": 400,
            "mensaje": "El archivo no es una imagen válida (solo JPG, PNG, GIF, WebP, BMP, AVIF o HEIC)."
        }

    tamano_kb = len(contents) / 1024
    MAX_TAMANO_KB = 1024  # 1 MB
    if tamano_kb > MAX_TAMANO_KB:
        return {
            "ok": False, "status": 413,
            "mensaje": f"La imagen es demasiado grande ({tamano_kb:.0f} KB). Máximo {MAX_TAMANO_KB} KB."
        }

    # 4. Determinar el orden y si es reemplazo o inserción nueva (PRE-CHECK
    #    optimista: sirve para el public_id y para fallar rápido antes de subir;
    #    el cómputo definitivo se revalida bajo lock en el paso 6).
    if orden_target is not None:
        # Reemplazo: validar que el orden esté en rango
        if orden_target < 1 or orden_target > 5:
            return {"ok": False, "status": 400, "mensaje": "orden_target debe estar entre 1 y 5"}
        nueva_orden = orden_target

        # La imagen que ocupa ese orden (si existe) se borra SOLO después de
        # subir la nueva (paso 7): si la subida falla, la foto vieja se
        # conserva en Cloudinary y en la BD (no se pierde nada).
        existente = query(
            "SELECT id, url FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s AND orden = %s",
            (producto_id, tenant_id, nueva_orden)
        )
        reemplazo_id = existente[0]["id"] if existente else None
        reemplazo_url_anterior = existente[0]["url"] if existente else None
    else:
        # Inserción nueva: calcular siguiente slot disponible.
        # El slot 1 está RESERVADO para la foto principal: si el producto ya
        # tiene foto en productos.imagen, las extras solo ocupan slots 2-5.
        # (Antes la primera extra tomaba el slot 1 y SOBREESCRIBÍA la foto
        #  principal — el bug por el que "la foto 2 reemplazaba a la 1".)
        tiene_principal = False
        fila_prod = query(
            "SELECT Imagen FROM productos WHERE id = %s AND tenant_id = %s",
            (producto_id, tenant_id)
        )
        if fila_prod:
            img = fila_prod[0].get("Imagen") or ""
            tiene_principal = bool(img) and img != "No hay foto"

        conteo = query(
            "SELECT COUNT(*) as total FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s",
            (producto_id, tenant_id)
        )
        total_actual = conteo[0]["total"] if conteo else 0
        # Un producto está lleno cuando la galería completa (incluyendo la
        # principal, que vive en productos.imagen) suma 5.
        total_fotos = total_actual + (1 if tiene_principal else 0)
        if total_fotos >= 5:
            return {
                "ok": False, "status": 403,
                "mensaje": "Este producto ya tiene 5 imágenes. Elimina una antes de subir otra."
            }

        ordenes = query(
            "SELECT orden FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s ORDER BY orden",
            (producto_id, tenant_id)
        )
        ordenes_usadas = {r["orden"] for r in ordenes}
        inicio = 2 if tiene_principal else 1
        nueva_orden = next((i for i in range(inicio, 6) if i not in ordenes_usadas), None)
        if nueva_orden is None:
            return {
                "ok": False, "status": 403,
                "mensaje": "Este producto ya tiene 5 imágenes. Elimina una antes de subir otra."
            }
        reemplazo_id = None
        reemplazo_url_anterior = None

    # 5. Subir a Cloudinary en la carpeta "productos/galeria"
    resultado = cloudinary.uploader.upload(
        contents,
        folder="productos/galeria",
        public_id=f"{producto}_{uuid.uuid4().hex[:8]}_img{nueva_orden}",
        quality="auto:best",
        fetch_format="auto"
    )
    url = resultado.get("secure_url")

    # 6. Guardar en la BD en UNA transacción con bloqueo de la fila del producto
    #    (SELECT ... FOR UPDATE). Esto serializa las escrituras concurrentes de
    #    la galería por producto: dos requests simultáneos ya no pueden elegir el
    #    mismo slot y violar UNIQUE(producto_id, orden). El slot y la capacidad
    #    se RECOMPUTAN bajo el lock; el paso 4 fue solo un pre-check optimista.
    descartar_url = None
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id FROM productos WHERE id=%s AND tenant_id=%s FOR UPDATE",
                (producto_id, tenant_id)
            )
            if reemplazo_id is not None:
                # Reemplazo: actualizar la fila existente (mismo id, mismo orden)
                cur.execute(
                    "UPDATE producto_imagenes SET url=%s WHERE id=%s AND tenant_id=%s",
                    (url, reemplazo_id, tenant_id)
                )
            else:
                # Inserción nueva: recomputar slot libre y capacidad bajo el lock
                cur.execute(
                    "SELECT Imagen FROM productos WHERE id=%s AND tenant_id=%s",
                    (producto_id, tenant_id)
                )
                fila_lock = cur.fetchone() or {}
                img_lock = fila_lock.get("Imagen") or ""
                tiene_principal_lock = bool(img_lock) and img_lock != "No hay foto"

                cur.execute(
                    "SELECT COUNT(*) AS total FROM producto_imagenes "
                    "WHERE producto_id=%s AND tenant_id=%s",
                    (producto_id, tenant_id)
                )
                total_lock = (cur.fetchone() or {}).get("total") or 0
                if total_lock + (1 if tiene_principal_lock else 0) >= 5:
                    # El producto se llenó entre el pre-check y aquí (race): no
                    # queda slot. Se descarta la imagen recién subida a Cloudinary.
                    descartar_url = url
                    raise _ErrorGaleria(
                        403,
                        "Este producto ya tiene 5 imágenes. Elimina una antes de subir otra."
                    )

                cur.execute(
                    "SELECT orden FROM producto_imagenes "
                    "WHERE producto_id=%s AND tenant_id=%s ORDER BY orden",
                    (producto_id, tenant_id)
                )
                usadas_lock = {r["orden"] for r in cur.fetchall()}
                inicio_lock = 2 if tiene_principal_lock else 1
                nueva_orden = next((i for i in range(inicio_lock, 6) if i not in usadas_lock), None)
                if nueva_orden is None:
                    descartar_url = url
                    raise _ErrorGaleria(
                        403,
                        "Este producto ya tiene 5 imágenes. Elimina una antes de subir otra."
                    )
                cur.execute(
                    "INSERT INTO producto_imagenes (producto_id, tenant_id, url, orden) "
                    "VALUES (%s, %s, %s, %s)",
                    (producto_id, tenant_id, url, nueva_orden)
                )
            # Sincronizar productos.imagen si la orden es 1 (la principal)
            if nueva_orden == 1:
                cur.execute(
                    "UPDATE productos SET Imagen=%s WHERE id=%s AND tenant_id=%s",
                    (url, producto_id, tenant_id)
                )
        conn.commit()
    except _ErrorGaleria as e:
        conn.rollback()
        if descartar_url:
            borrar_imagen_cloudinary(descartar_url)
        return {"ok": False, "status": e.status, "mensaje": e.mensaje}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "status": 500, "mensaje": f"Error guardando la imagen: {str(e)}"}
    finally:
        release_conn(conn)

    # 7. Borrar la imagen anterior de Cloudinary SOLO tras subir la nueva
    if reemplazo_url_anterior and reemplazo_url_anterior != url:
        borrar_imagen_cloudinary(reemplazo_url_anterior)

    return {"ok": True, "url": url, "orden": nueva_orden}


def eliminar_imagen_extra(imagen_id: int, tenant_id: str) -> dict:
    """
    Elimina una imagen de la galería de un producto (Plan Plus).
    - Borra la imagen de Cloudinary.
    - Si era la orden 1 (principal), promueve la siguiente imagen a principal
      y sincroniza productos.imagen.
    - Reordena las imágenes restantes para que no quien huecos.
    """
    # 1. Validar plan plus
    plan = _get_tenant_plan(tenant_id)
    if plan != "plus":
        return {"ok": False, "status": 403, "mensaje": "La gestión de galería es exclusiva del plan Plus."}

    # 2. Obtener la imagen antes de borrar
    resultado = query(
        "SELECT id, producto_id, url, orden FROM producto_imagenes "
        "WHERE id = %s AND tenant_id = %s",
        (imagen_id, tenant_id)
    )
    if not resultado:
        return {"ok": False, "status": 404, "mensaje": "Imagen no encontrada"}

    img = resultado[0]
    url = img["url"]
    orden_eliminada = img["orden"]
    producto_id = img["producto_id"]

    # 3. Borrar de Cloudinary
    borrar_imagen_cloudinary(url)

    # 4. Borrar de la base de datos
    execute(
        "DELETE FROM producto_imagenes WHERE id = %s AND tenant_id = %s",
        (imagen_id, tenant_id)
    )

    # 5. Si era la principal (orden 1), promover la siguiente y reordenar
    if orden_eliminada == 1:
        # Buscar la siguiente imagen disponible (menor orden)
        siguiente = query(
            "SELECT id, url FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s "
            "ORDER BY orden ASC LIMIT 1",
            (producto_id, tenant_id)
        )
        if siguiente:
            # La siguiente asciende a orden 1
            execute(
                "UPDATE producto_imagenes SET orden = 1 WHERE id = %s",
                (siguiente[0]["id"],)
            )
            # Sincronizar productos.imagen con la nueva principal
            execute(
                "UPDATE productos SET Imagen = %s WHERE id = %s AND tenant_id = %s",
                (siguiente[0]["url"], producto_id, tenant_id)
            )
        else:
            # No quedan imágenes: resetear productos.imagen
            execute(
                "UPDATE productos SET Imagen = 'No hay foto' WHERE id = %s AND tenant_id = %s",
                (producto_id, tenant_id)
            )
    else:
        # No era la principal, pero reordenamos para compactar slots
        # Mover las imágenes con orden > orden_eliminada un paso atrás
        posteriores = query(
            "SELECT id, orden FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s AND orden > %s "
            "ORDER BY orden ASC",
            (producto_id, tenant_id, orden_eliminada)
        )
        for p in posteriores:
            nuevo_orden = p["orden"] - 1
            execute(
                "UPDATE producto_imagenes SET orden = %s WHERE id = %s",
                (nuevo_orden, p["id"])
            )

    return {"ok": True, "id": imagen_id}


def reordenar_imagenes(producto: str, ids: list, tenant_id: str) -> dict:
    """
    Reordena las imágenes de la galería de un producto (Plan Plus).
    Recibe un array de IDs en el nuevo orden (posición 0 = orden 1 = principal).
    El backend asigna orden 1, 2, 3... secuencialmente según el orden del array.
    Si el ID en orden 1 es diferente al anterior, actualiza productos.imagen
    automáticamente con la URL de la nueva imagen principal.
    """
    # 1. Validar plan plus
    plan = _get_tenant_plan(tenant_id)
    if plan != "plus":
        return {"ok": False, "status": 403, "mensaje": "La gestión de galería es exclusiva del plan Plus."}

    if not ids:
        return {"ok": False, "status": 400, "mensaje": "El array de IDs no puede estar vacío"}

    try:
        producto_id = _get_producto_id(producto, tenant_id)
    except _ErrorGaleria as e:
        return {"ok": False, "status": e.status, "mensaje": e.mensaje}

    # 2. Verificar que todos los IDs pertenecen a este producto y tenant
    existentes = query(
        "SELECT id, url, orden FROM producto_imagenes "
        "WHERE producto_id = %s AND tenant_id = %s AND id = ANY(%s)",
        (producto_id, tenant_id, ids)
    )
    ids_validos = {r["id"] for r in existentes}
    ids_enviados = set(ids)
    if ids_enviados - ids_validos:
        return {
            "ok": False, "status": 400,
            "mensaje": "Algunos IDs no pertenecen a este producto o no existen."
        }

    # 3. Actualizar orden secuencialmente según la posición en el array
    for idx, img_id in enumerate(ids):
        nuevo_orden = idx + 1
        execute(
            "UPDATE producto_imagenes SET orden = %s WHERE id = %s AND tenant_id = %s",
            (nuevo_orden, img_id, tenant_id)
        )

    # 4. Sincronizar productos.imagen con la nueva orden 1
    primera = query(
        "SELECT url FROM producto_imagenes WHERE id = %s AND tenant_id = %s",
        (ids[0], tenant_id)
    )
    if primera:
        execute(
            "UPDATE productos SET Imagen = %s WHERE id = %s AND tenant_id = %s",
            (primera[0]["url"], producto_id, tenant_id)
        )

    return {"ok": True, "mensaje": "Imágenes reordenadas correctamente"}
