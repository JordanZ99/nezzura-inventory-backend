# ==============================================================================
# backend/database/mcp_estilos.py
# Presets de FOTO por comando para el conector MCP (rol Diseñador / Inventario).
#
# El dueño define comandos de foto (ej. '/img') desde el gestor. Cuando escribe
# el comando en el chat —o pide quitar/limpiar el fondo, retocar la foto o
# ponerla en fondo de estudio— el agente llama la tool estilos_foto, toma el
# prompt del preset y lo aplica a la imagen que le dieron (sin cambiar el
# producto), para luego adjuntarla con adjuntar_imagen.
#
# Fuente de los presets (merge):
#   1. DEFAULT del código: la clave 'estudio' con el prompt recomendado por la
#      investigación de prompting de fotografía de producto (fórmula: preservar
#      primero → cambio concreto → luz precisa → restricciones negativas).
#   2. Filas de mcp_estilos_foto (migrations/082): el override del default si
#      el dueño lo editó, y los presets extra que añadió.
#
# La BD caída NUNCA rompe el conector: cualquier fallo cae al default del
# código (el agente sigue teniendo su comando /img).
# ==============================================================================

import re

from database.conexion import query, execute

# Tope de presets por tenant: los comandos de foto son un atajo para el dueño,
# no un catálogo infinito (más presets = peor selección de tool en el agente).
MAX_PRESETS_POR_TENANT = 6

MAX_LARGO_COMANDO = 20
MAX_LARGO_NOMBRE = 40
MAX_LARGO_PROMPT = 2000

# ── Default del sistema: preset 'estudio' ─────────────────────────────────────
# Prompt orientado por las guías de prompting de fotografía de producto:
# 1. PRESERVAR primero (forma, colores, texturas, proporciones) — evita que la
#    IA reinvente el producto.
# 2. Un cambio grande por edición (fondo de estudio + su luz, nada más).
# 3. Luz descrita con precisión: softbox frontal-superior + sombra que PISA el
#    producto (una luz bien descrita vale más que tres sueltas).
# 4. Restricciones negativas al final (sin texto, sin objetos, sin bordes
#    distorsionados).
DEFAULT_ESTUDIO = {
    "clave": "estudio",
    "comando": "/img",
    "nombre": "Foto de estudio",
    "prompt": (
        "Retoque profesional de foto de producto para catálogo. PRESERVA "
        "EXACTAMENTE la forma, los colores, las texturas, las proporciones y "
        "todos los detalles del producto: no cambies su diseño ni inventes "
        "nada. Recorta el producto con precisión y colócalo sobre un fondo "
        "blanco puro de estudio. Iluminación: luz suave y difusa de estudio "
        "(softbox frontal-superior), con una sombra sutil y realista debajo "
        "del producto para que no parezca flotar. Nitidez alta y uniforme. "
        "No agregues texto, objetos, personas ni reflejos que distorsionen el "
        "producto; no recortes ninguna parte del producto."
    ),
    "es_default": True,
    "activo": True,
}


def normalizar_comando(comando) -> str | None:
    """
    Valida y normaliza un comando de foto: debe empezar con '/', sin espacios,
    sin caracteres raros (letras, números, guion bajo después de la barra).
    Devuelve el comando en minúsculas o None si es inválido.
    """
    if not isinstance(comando, str):
        return None
    limpio = comando.strip().lower()
    if len(limpio) < 2 or len(limpio) > MAX_LARGO_COMANDO:
        return None
    if not re.fullmatch(r"/[a-z0-9_]+", limpio):
        return None
    return limpio


def _limpiar_filas(filas: list[dict]) -> list[dict]:
    """Filas de la BD → presets con el mismo formato del default."""
    return [
        {
            "clave": str(f.get("clave") or "").strip(),
            "comando": str(f.get("comando") or "").strip().lower(),
            "nombre": str(f.get("nombre") or "").strip() or "Foto",
            "prompt": str(f.get("prompt") or "").strip(),
            "es_default": bool(f.get("es_default")),
            "activo": bool(f.get("activo")),
        }
        for f in filas
    ]


def estilos_foto(tenant_id: str) -> list[dict]:
    """
    Presets EFECTIVOS del tenant: el default del código (posiblemente
    sobrescrito por la fila 'estudio' de la BD) + los presets extra activos.
    Nunca lanza: si la BD falla devuelve el default del código.
    """
    try:
        filas = _limpiar_filas(query(
            "SELECT clave, comando, nombre, prompt, es_default, activo "
            "FROM mcp_estilos_foto WHERE tenant_id = %s ORDER BY id",
            (tenant_id,),
        ))
    except Exception as e:
        print(f"[MCP] estilos_foto fallo: {type(e).__name__}: {e}")
        return [dict(DEFAULT_ESTUDIO)]

    overrides = {f["clave"]: f for f in filas if f["clave"] == DEFAULT_ESTUDIO["clave"]}
    extra = [f for f in filas if f["clave"] != DEFAULT_ESTUDIO["clave"]]

    # El default: override del dueño si existe, si no el del código.
    base = overrides.get(DEFAULT_ESTUDIO["clave"], dict(DEFAULT_ESTUDIO))

    activos = [base] + [f for f in extra if f["activo"]]
    # Defensa: sin comando ni prompt no sirve de nada.
    return [
        f for f in activos
        if f["comando"].startswith("/") and f["prompt"]
    ]


def presets_para_mcp(tenant_id: str) -> dict:
    """Respuesta de la tool estilos_foto: presets activos con su comando y
    prompt exacto, más la nota de flujo (config_catalogo → retocar →
    adjuntar_imagen)."""
    presets = estilos_foto(tenant_id)
    if not presets:
        return {"error": "El negocio no tiene comandos de foto activos."}
    return {
        "presets": [
            {
                "comando": p["comando"],
                "nombre": p["nombre"],
                "prompt": p["prompt"],
            }
            for p in presets
        ],
        "nota": (
            "Elige el preset cuyo comando escribió el dueño (o el que "
            "corresponda a lo que pidió). Copia el prompt TAL CUAL para "
            "retocar la imagen que te dio el dueño, SIN cambiar el producto, "
            "y adjunta el resultado con adjuntar_imagen (consulta "
            "config_catalogo antes para la proporción del catálogo)."
        ),
    }


def comandos_resumen(tenant_id: str) -> str:
    """
    Lista corta de comandos para inyectar en instrucciones/prompt del conector:
    '/img (Foto de estudio), /premium (Estilo premium)'. Nunca lanza.
    """
    try:
        presets = estilos_foto(tenant_id)
    except Exception:
        presets = [dict(DEFAULT_ESTUDIO)]
    return ", ".join(f"{p['comando']} ({p['nombre']})" for p in presets) or DEFAULT_ESTUDIO["comando"]


# ── CRUD del gestor (routers/mcp_conexion.py) ────────────────────────────────

def _slug_de_clave(nombre: str) -> str:
    """Clave corta para un preset extra: del nombre, sin espacios ni acentos."""
    base = (nombre or "").strip().lower()
    base = re.sub(r"[áàäâã]", "a", base)
    base = re.sub(r"[éèëê]", "e", base)
    base = re.sub(r"[íìïî]", "i", base)
    base = re.sub(r"[óòöôõ]", "o", base)
    base = re.sub(r"[úùüû]", "u", base)
    base = re.sub(r"ñ", "n", base)
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:30] or "preset"
    return slug


def guardar_override_default(tenant_id: str, comando: str, nombre: str, prompt: str) -> dict:
    """
    Edita el preset DEFAULT ('estudio'): comando, nombre y prompt. Upsert: la
    fila solo existe si el dueño lo editó alguna vez. Sin rol de eliminación:
    el default vuelve al código borrando el override.
    """
    comando_limpio = normalizar_comando(comando)
    if comando_limpio is None:
        return {
            "ok": False,
            "status": 422,
            "mensaje": "Comando inválido: debe empezar con '/' y solo llevar letras, números o guion bajo (ej. /img).",
        }
    nombre_limpio = (nombre or "").strip()[:MAX_LARGO_NOMBRE] or "Foto de estudio"
    prompt_limpio = (prompt or "").strip()
    if not (10 <= len(prompt_limpio) <= MAX_LARGO_PROMPT):
        return {
            "ok": False,
            "status": 422,
            "mensaje": f"El prompt debe tener entre 10 y {MAX_LARGO_PROMPT} caracteres.",
        }
    # El comando no puede chocar con otro preset del tenant.
    try:
        choque = query(
            "SELECT 1 FROM mcp_estilos_foto "
            "WHERE tenant_id = %s AND comando = %s AND clave <> %s LIMIT 1",
            (tenant_id, comando_limpio, DEFAULT_ESTUDIO["clave"]),
        )
        if choque:
            return {
                "ok": False,
                "status": 409,
                "mensaje": f"El comando {comando_limpio} ya lo usa otro preset.",
            }
        execute(
            """INSERT INTO mcp_estilos_foto
                   (tenant_id, clave, comando, nombre, prompt, es_default, activo)
               VALUES (%s, %s, %s, %s, %s, TRUE, TRUE)
               ON CONFLICT (tenant_id, clave) DO UPDATE SET
                   comando = EXCLUDED.comando,
                   nombre = EXCLUDED.nombre,
                   prompt = EXCLUDED.prompt,
                   activo = TRUE,
                   editada_en = now()""",
            (tenant_id, DEFAULT_ESTUDIO["clave"], comando_limpio,
             nombre_limpio, prompt_limpio),
        )
    except Exception as e:
        print(f"[MCP] guardar_override_default fallo: {type(e).__name__}: {e}")
        return {"ok": False, "status": 500, "mensaje": "No se pudo guardar el preset."}
    return {"ok": True}


def editar_preset(tenant_id: str, clave: str, comando: str, nombre: str, prompt: str) -> dict:
    """
    Edita cualquier preset: comando, nombre y prompt. El default 'estudio' es
    un upsert del override (sin fila = prompt recomendado del código); los
    extras son UPDATE directos de su fila.
    """
    clave_limpia = (clave or "").strip().lower()
    if clave_limpia == DEFAULT_ESTUDIO["clave"]:
        return guardar_override_default(tenant_id, comando, nombre, prompt)

    comando_limpio = normalizar_comando(comando)
    if comando_limpio is None:
        return {
            "ok": False,
            "status": 422,
            "mensaje": "Comando inválido: debe empezar con '/' y solo llevar letras, números o guion bajo (ej. /detalle).",
        }
    nombre_limpio = (nombre or "").strip()[:MAX_LARGO_NOMBRE]
    if not nombre_limpio:
        return {"ok": False, "status": 422, "mensaje": "Ponle un nombre al preset."}
    prompt_limpio = (prompt or "").strip()
    if not (10 <= len(prompt_limpio) <= MAX_LARGO_PROMPT):
        return {
            "ok": False,
            "status": 422,
            "mensaje": f"El prompt debe tener entre 10 y {MAX_LARGO_PROMPT} caracteres.",
        }
    try:
        choque = query(
            "SELECT 1 FROM mcp_estilos_foto "
            "WHERE tenant_id = %s AND comando = %s AND clave <> %s LIMIT 1",
            (tenant_id, comando_limpio, clave_limpia),
        )
        if choque:
            return {
                "ok": False,
                "status": 409,
                "mensaje": f"El comando {comando_limpio} ya lo usa otro preset.",
            }
        existe = query(
            "SELECT 1 FROM mcp_estilos_foto WHERE tenant_id = %s AND clave = %s LIMIT 1",
            (tenant_id, clave_limpia),
        )
        if not existe:
            return {"ok": False, "status": 404, "mensaje": "Preset no encontrado."}
        execute(
            "UPDATE mcp_estilos_foto SET comando = %s, nombre = %s, prompt = %s, "
            "activo = TRUE, editada_en = now() "
            "WHERE tenant_id = %s AND clave = %s",
            (comando_limpio, nombre_limpio, prompt_limpio, tenant_id, clave_limpia),
        )
        return {"ok": True}
    except Exception as e:
        print(f"[MCP] editar_preset fallo: {type(e).__name__}: {e}")
        return {"ok": False, "status": 500, "mensaje": "No se pudo guardar el preset."}


def crear_preset(tenant_id: str, comando: str, nombre: str, prompt: str) -> dict:
    """Añade un preset extra (otro comando para otro estilo). La clave se
    deriva del nombre; los choques de clave se resuelven con sufijo."""
    comando_limpio = normalizar_comando(comando)
    if comando_limpio is None:
        return {
            "ok": False,
            "status": 422,
            "mensaje": "Comando inválido: debe empezar con '/' y solo llevar letras, números o guion bajo (ej. /detalle).",
        }
    nombre_limpio = (nombre or "").strip()[:MAX_LARGO_NOMBRE]
    if not nombre_limpio:
        return {"ok": False, "status": 422, "mensaje": "Ponle un nombre al preset."}
    prompt_limpio = (prompt or "").strip()
    if not (10 <= len(prompt_limpio) <= MAX_LARGO_PROMPT):
        return {
            "ok": False,
            "status": 422,
            "mensaje": f"El prompt debe tener entre 10 y {MAX_LARGO_PROMPT} caracteres.",
        }
    try:
        existentes = query(
            "SELECT comando, clave FROM mcp_estilos_foto WHERE tenant_id = %s",
            (tenant_id,),
        )
        if len(existentes) >= MAX_PRESETS_POR_TENANT:
            return {
                "ok": False,
                "status": 409,
                "mensaje": f"Ya tienes {MAX_PRESETS_POR_TENANT} presets. Elimina alguno antes de crear otro.",
            }
        if any((f.get("comando") or "").lower() == comando_limpio for f in existentes):
            return {
                "ok": False,
                "status": 409,
                "mensaje": f"El comando {comando_limpio} ya existe.",
            }
        clave = _slug_de_clave(nombre_limpio)
        claves = {f.get("clave") for f in existentes}
        if clave in claves or clave == DEFAULT_ESTUDIO["clave"]:
            n = 2
            while f"{clave}-{n}" in claves:
                n += 1
            clave = f"{clave}-{n}"
        execute(
            "INSERT INTO mcp_estilos_foto (tenant_id, clave, comando, nombre, prompt) "
            "VALUES (%s, %s, %s, %s, %s)",
            (tenant_id, clave, comando_limpio, nombre_limpio, prompt_limpio),
        )
        return {"ok": True, "clave": clave}
    except Exception as e:
        print(f"[MCP] crear_preset fallo: {type(e).__name__}: {e}")
        return {"ok": False, "status": 500, "mensaje": "No se pudo crear el preset."}


def eliminar_preset(tenant_id: str, clave: str) -> dict:
    """
    Elimina un preset. El DEFAULT ('estudio') NO se elimina: si el dueño
    quiere el prompt recomendado, borra su override (DELETE lo respeta igual —
    la fila desaparece y el código vuelve a servir el default).
    """
    clave_limpia = (clave or "").strip().lower()
    if not clave_limpia:
        return {"ok": False, "status": 422, "mensaje": "Falta la clave del preset."}
    try:
        execute(
            "DELETE FROM mcp_estilos_foto WHERE tenant_id = %s AND clave = %s",
            (tenant_id, clave_limpia),
        )
    except Exception as e:
        print(f"[MCP] eliminar_preset fallo: {type(e).__name__}: {e}")
        return {"ok": False, "status": 500, "mensaje": "No se pudo eliminar el preset."}
    return {"ok": True}


def listar_para_gestor(tenant_id: str) -> list[dict]:
    """
    Lista para la UI: el default (con override si existe, marcado editado) +
    los presets extra (activos e inactivos, para poder reactivar/borrar).
    """
    filas = {f["clave"]: f for f in _limpiar_filas(query(
        "SELECT clave, comando, nombre, prompt, es_default, activo "
        "FROM mcp_estilos_foto WHERE tenant_id = %s ORDER BY id",
        (tenant_id,),
    ))}
    base = dict(filas.get(DEFAULT_ESTUDIO["clave"], DEFAULT_ESTUDIO))
    base["editado"] = DEFAULT_ESTUDIO["clave"] in filas
    base["prompt_recomendado"] = DEFAULT_ESTUDIO["prompt"]
    extras = [f for k, f in filas.items() if k != DEFAULT_ESTUDIO["clave"]]
    return [base] + extras
