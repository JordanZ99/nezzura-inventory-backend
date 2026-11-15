# ==============================================================================
# backend/database/mcp_imagenes.py
# Adjuntar imágenes a productos desde el conector MCP (claves nivel 3).
#
# El agente (Muse) recibe/produce imágenes como base64; el gestor NO puede
# exigirle que las comprima antes. Este módulo:
#   1. Decodifica el base64 (acepta data URL "data:image/...;base64,..." o
#      base64 puro, con o sin saltos de línea).
#   2. Pasa la imagen por el MISMO reductor de peso que el frontend:
#      reescala a máx. 1600 px y re-comprime a JPEG progresivo (~80 KB-900 KB)
#      antes de tocar Cloudinary. Si Pillow no puede procesarla, entrega los
#      bytes originales y la validación de magic bytes de imagenes.py decide.
#   3. ENCAJA la imagen a la proporción que usa el catálogo del tenant
#      (relacion_imagen: 1:1 o 4:5) — el default 'marco' sube la imagen
#      COMPLETA y rellena el lado corto con blanco/negro/rojo, igual que el
#      recorte manual del frontend; 'recorte' llena el marco recortando el
#      centro y 'libre' deja la foto tal cual. Sin encaje el catálogo
#      letterbox-ea la foto contra el fondo del tema y se ven franjas raras.
#   4. Reutiliza el pipeline REAL de subida (database/imagenes.py):
#      subir_foto() para la principal y subir_imagen_extra() para la galería
#      (plan Plus), que ya validan magic bytes, cuota de almacenamiento (071),
#      límite de 5 fotos y sincronizan productos.imagen con la galería.
#   5. Cada adjunto queda en el Historial con origen 'agente_ia'.
#
# Efecto directo (sin borrador→confirmar): adjuntar una foto no corrompe
# datos de negocio; el límite de defensa es la cuota de escrituras diarias
# de la clave (contada en server.py antes de llamar aquí) y la cuota de
# almacenamiento del tenant.
# ==============================================================================

import base64
import io
import re

from database.actividad import registrar_actividad
from database.conexion import query
from database.imagenes import (
    asignar_foto_principal,
    subir_foto,
    subir_imagen_extra,
)

# Límite de ENTRADA (bytes decodificados): las fotos de agentes suelen pesar
# 1-2 MB; 10 MB ya es abuso y protege la memoria del server (Render).
MAX_ENTRADA_BYTES = 10 * 1024 * 1024

# Objetivo de salida del reductor: mismo espíritu que el frontend (~80 KB) pero
# sin sacrificar calidad de publicación: se escala la calidad hasta caber.
MAX_SALIDA_KB = 900

_RE_DATA_URL = re.compile(r"^data:image/[a-zA-Z0-9.+-]+;base64,", re.IGNORECASE)


def _decodificar_imagen(valor) -> tuple[bytes | None, str]:
    """
    Decodifica el campo del agente. Devuelve (bytes, mensaje_error).
    bytes es None SOLO cuando hay error (con mensaje orientado al agente).
    """
    if valor is None or not isinstance(valor, str) or not valor.strip():
        return None, "Falta la imagen: envía 'imagen_base64' con el archivo en base64."
    texto = valor.strip()
    texto = _RE_DATA_URL.sub("", texto)      # data:image/png;base64,XXXX → XXXX
    texto = re.sub(r"\s+", "", texto)        # el base64 de los LLMs suele venir con saltos
    try:
        contenido = base64.b64decode(texto, validate=True)
    except Exception:
        return None, (
            "'imagen_base64' no es un base64 válido. Envía el archivo de la "
            "imagen codificado en base64 (o una data URL 'data:image/...;base64,...')."
        )
    if not contenido:
        return None, "La imagen decodificada está vacía (0 bytes)."
    if len(contenido) > MAX_ENTRADA_BYTES:
        return None, (
            f"La imagen es demasiado grande ({len(contenido) / 1024 / 1024:.1f} MB). "
            "El máximo de entrada es 10 MB; el gestor la reduce de peso automáticamente."
        )
    return contenido, ""


def _comprimir_imagen(contenido: bytes) -> bytes:
    """
    El reductor de peso para fotos de agentes (el frontend comprime en el
    navegador; aquí el server hace ese papel). Reescala a 1600 px de lado
    mayor, aplana transparencias sobre blanco y re-comprime a JPEG bajando la
    calidad hasta caber en MAX_SALIDA_KB. Devuelve el original si queda más
    ligero que el comprimido, o si Pillow no puede procesarlo (HEIC sin
    plugin, GIF animado...) — la validación de magic bytes de imagenes.py
    sigue siendo la última red.
    """
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(contenido))
        img = ImageOps.exif_transpose(img) or img   # respeta la orientación EXIF
        if img.mode != "RGB":
            if img.mode in ("RGBA", "LA", "P"):
                plano = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                plano.paste(rgba, mask=rgba.getchannel("A"))
                img = plano
            else:
                img = img.convert("RGB")
        if max(img.size) > 1600:
            img.thumbnail((1600, 1600))

        mejor: bytes | None = None
        for calidad in (85, 80, 70, 60):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=calidad, optimize=True)
            mejor = buf.getvalue()
            if len(mejor) <= MAX_SALIDA_KB * 1024:
                break
        if mejor and len(mejor) < len(contenido):
            return mejor
    except Exception as e:
        # Reducción best-effort: sin Pillow o con formato exótico, seguimos
        # con los bytes originales.
        print(f"[MCP] No se pudo reducir la imagen del agente: {type(e).__name__}: {e}")
    return contenido


# Colores de relleno del marco (mismos tonos que el tema del catálogo ofrece).
_COLORES_MARCO = {
    "blanco": (255, 255, 255),
    "negro": (0, 0, 0),
    "rojo": (229, 57, 53),
}


def _relacion_del_tenant(tenant_id: str) -> tuple[int, int]:
    """
    Proporción con la que el catálogo de ESTE tenant muestra las fotos
    (catalogo_config.relacion_imagen, migración 024: '1:1' default o '4:5').
    Si el dueño cambia el catálogo a 4:5, las fotos del agente se adaptan
    solas. Best-effort: ante cualquier duda devuelve 1:1.
    """
    try:
        filas = query(
            "SELECT relacion_imagen FROM catalogo_config WHERE tenant_id = %s",
            (tenant_id,),
        )
        valor = ((filas[0].get("relacion_imagen") if filas else "") or "1:1").strip()
    except Exception as e:
        print(f"[MCP] No se pudo leer relacion_imagen del tenant: {e}")
        valor = "1:1"
    m = re.match(r"^(\d{1,2})\s*:\s*(\d{1,2})$", valor)
    if not m:
        return 1, 1
    ancho, alto = int(m.group(1)), int(m.group(2))
    if ancho <= 0 or alto <= 0:
        return 1, 1
    return ancho, alto


def _encajar_imagen(contenido: bytes, encaje: str, fondo: str, relacion: tuple[int, int]) -> bytes:
    """
    Encaja la imagen a la proporción del catálogo (el papel que hace el
    ImageCropperModal en la subida manual):
      - 'marco'   → la imagen COMPLETA, rellenando el lado corto con el color
                    de fondo (ImageOps.pad): nunca corta nada del producto.
      - 'recorte' → llena el marco recortando el centro (ImageOps.fit): puede
                    cortar bordes, útil cuando el producto ocupa toda la escena.
    El lado mayor del lienzo = el de la foto (máx. 1600), así una foto chica
    NUNCA se escala hacia arriba (se pierde nitidez y pesa más). Tras el
    encaje re-comprime a JPEG porque el marco agrandó el lienzo. Best-effort:
    si Pillow falla, devuelve los bytes tal cual llegaron.
    """
    if encaje not in ("marco", "recorte"):
        return contenido
    try:
        from PIL import Image, ImageOps

        img = Image.open(io.BytesIO(contenido))
        img = ImageOps.exif_transpose(img) or img
        if img.mode != "RGB":
            if img.mode in ("RGBA", "LA", "P"):
                plano = Image.new("RGB", img.size, (255, 255, 255))
                rgba = img.convert("RGBA")
                plano.paste(rgba, mask=rgba.getchannel("A"))
                img = plano
            else:
                img = img.convert("RGB")

        ancho_r, alto_r = relacion
        # El lado mayor del lienzo: en 'marco' es el de la foto (se RELLENA el
        # corto, nunca se escala hacia arriba); en 'recorte' es el lado corto
        # de la foto (se CORTA el sobrante, tampoco se escala hacia arriba).
        lado = min(1600, max(img.size) if encaje == "marco" else min(img.size))
        if ancho_r >= alto_r:
            tamano = (lado, max(1, round(lado * alto_r / ancho_r)))
        else:
            tamano = (max(1, round(lado * ancho_r / alto_r)), lado)

        if encaje == "marco":
            img = ImageOps.pad(img, tamano, color=_COLORES_MARCO.get(fondo, _COLORES_MARCO["blanco"]))
        else:
            img = ImageOps.fit(img, tamano)

        mejor: bytes | None = None
        for calidad in (85, 80, 70, 60):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=calidad, optimize=True)
            mejor = buf.getvalue()
            if len(mejor) <= MAX_SALIDA_KB * 1024:
                break
        return mejor or contenido
    except Exception as e:
        print(f"[MCP] No se pudo encajar la imagen del agente: {type(e).__name__}: {e}")
        return contenido


def _producto_del_tenant(tenant_id: str, producto_id: int) -> tuple[int | None, str]:
    """
    Verifica que el producto exista, sea del tenant y sea un producto FÍSICO
    activo (mismo alcance que crear/editar por MCP: servicios y compuestos
    quedan fuera). Devuelve (producto_nombre, error).
    """
    filas = query(
        "SELECT Producto AS producto FROM productos WHERE id = %s AND tenant_id = %s "
        "AND Estado = 'Activo' AND tipo_producto = 'stock'",
        (producto_id, tenant_id),
    )
    if not filas:
        return None, f"No existe un producto físico activo con id {producto_id}."
    return filas[0]["producto"], ""


def adjuntar(
    tenant_id: str, producto_id: int, imagen_b64, destino: str,
    encaje: str = "marco", fondo: str = "blanco",
) -> dict:
    """
    Adjunta una imagen (base64) a un producto. destino:
      - 'principal' → reemplaza/asigna la foto principal (productos.imagen).
      - 'galeria'   → agrega una imagen extra (plan Plus, máx. 5 fotos).
    encaje:
      - 'marco' (default) → la imagen COMPLETA rellenando el lado corto con
        'fondo' (blanco/negro/rojo) hasta la proporción del catálogo del
        tenant: el equivalente exacto del recorte manual del frontend.
      - 'recorte' → llena el marco recortando el centro.
      - 'libre' → la foto tal cual (sin marco).
    Devuelve dicts tipo MCP: {...} en éxito o {"error": ...}.

    'principal' replica el flujo humano COMPLETO (upload + PATCH): subir_foto
    solo manda la imagen a Cloudinary y devuelve la ruta — quien guarda la URL
    en productos.imagen y sincroniza la galería es asignar_foto_principal.
    Sin ese paso la foto quedaba huérfana en Cloudinary y la primera imagen
    de galería terminaba tomando el slot 1 y pisando la principal (bug de la
    Kuromi: 'la de frente se volvió la principal y la de estudio se perdió').
    """
    try:
        producto_id = int(producto_id)
    except (TypeError, ValueError):
        return {"error": "El 'producto_id' debe ser un número."}

    destino = (destino or "principal").strip().lower()
    if destino not in ("principal", "galeria"):
        return {"error": "destino inválido: usa 'principal' o 'galeria'."}

    encaje = (encaje or "marco").strip().lower()
    if encaje not in ("marco", "recorte", "libre"):
        return {"error": "encaje inválido: usa 'marco' (imagen completa con relleno), 'recorte' o 'libre'."}

    fondo = (fondo or "blanco").strip().lower()
    if fondo not in _COLORES_MARCO:
        return {"error": "fondo inválido: usa 'blanco', 'negro' o 'rojo'."}

    contenido, error = _decodificar_imagen(imagen_b64)
    if error:
        return {"error": error}

    producto_nombre, error = _producto_del_tenant(tenant_id, producto_id)
    if error:
        return {"error": error}

    contenido = _comprimir_imagen(contenido)
    if encaje in ("marco", "recorte"):
        contenido = _encajar_imagen(contenido, encaje, fondo, _relacion_del_tenant(tenant_id))

    if destino == "principal":
        resultado = subir_foto(producto_nombre, contenido, tenant_id)
    else:
        resultado = subir_imagen_extra(producto_nombre, contenido, None, tenant_id)

    if not isinstance(resultado, dict) or not resultado.get("ok"):
        mensaje = (resultado or {}).get("mensaje") if isinstance(resultado, dict) else None
        return {"error": mensaje or "No se pudo adjuntar la imagen."}

    url = resultado.get("url") or resultado.get("ruta") or ""

    if destino == "principal":
        # Segundo paso del flujo humano: GUARDAR la URL en la BD. Sin esto la
        # foto solo existe en Cloudinary y el producto sigue "sin foto" —
        # la primera galería que llegue después pisaría la principal.
        asignacion = asignar_foto_principal(producto_id, url, tenant_id)
        if not isinstance(asignacion, dict) or not asignacion.get("ok"):
            mensaje = asignacion.get("mensaje") if isinstance(asignacion, dict) else None
            return {"error": mensaje or "No se pudo asignar la foto principal."}
    respuesta = {
        "ok": True,
        "producto_id": producto_id,
        "destino": destino,
        "url": url,
        "nota": "La imagen ya está en el producto y pasó por el reductor de peso del gestor.",
    }
    if resultado.get("aviso"):
        respuesta["aviso"] = resultado["aviso"]
    if destino == "galeria" and resultado.get("orden") is not None:
        respuesta["orden"] = resultado["orden"]

    try:
        registrar_actividad(
            tenant_id, "producto", "producto", "editar",
            resumen=(
                f"El agente IA adjuntó una foto a {producto_nombre} "
                f"({'principal' if destino == 'principal' else 'galería'})"
            ),
            entidad_id=producto_id,
            detalle={"origen": "agente_ia", "destino": destino, "url": url},
        )
    except Exception:
        pass  # el historial nunca bloquea la operación

    return respuesta
