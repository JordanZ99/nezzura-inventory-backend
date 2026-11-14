# ==============================================================================
# backend/database/almacenamiento.py
# Cuota de almacenamiento por tenant según su plan (migración 071).
#
# El medidor vive SOLO en la BD propia (tenant_storage + storage_eventos):
# NUNCA consulta APIs del proveedor de imágenes para decidir. Así la futura
# migración de Cloudinary a otro proveedor (Supabase Storage / Cloudflare
# Images) no toca este sistema: solo cambia la llamada de subida en la capa
# de imágenes (database/imagenes.py, helper _subir_con_cupo).
#
# Qué cuenta la cuota: los BYTES que el tenant SUBE (len del archivo que
# envía). Es el número que el tenant puede verificar ("lo que subí") y basta
# como límite honesto. bytes_reales (peso real en el proveedor) se registra
# aparte para control de costos, sin entrar en la cuota.
#
# Reglas:
#   - BLOQUEO DURO: si la subida nueva no cabe en el límite del plan ACTUAL,
#     se rechaza con 422 antes de tocar al proveedor. El tenant libera espacio
#     borrando imágenes, o mejora a Plus.
#   - REEMPLAZO 1:1: si la subida destruye un asset existente (foto de
#     variación, imagen de galería en un slot, logo/banner), el peso de la
#     foto vieja cuenta como disponible. Un tenant excedido puede cambiar una
#     foto por otra igual o MÁS LIGERA (el peso neto nunca crece), pero ni un
#     byte más de crecimiento.
#   - FAIL-OPEN: si el medidor falla por un error interno de BD, la subida
#     PASA (la app no se rompe por el medidor) y el drift lo corrige la
#     reconciliación. El bloqueo aplica solo cuando el medidor responde bien.
# ==============================================================================

import json

from psycopg2.extras import RealDictCursor
from psycopg2.errors import UndefinedColumn, UndefinedTable

from database.conexion import get_conn, release_conn, query, execute

GB = 1073741824  # 1 GB binario (como lo muestra el sistema operativo)
CLOUDINARY_MARK = "res.cloudinary.com"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_gb(bytes_: int) -> str:
    """Formato legible: '0.42 GB' / '312 MB'."""
    if abs(bytes_) >= GB:
        return f"{bytes_ / GB:.2f} GB"
    return f"{bytes_ / (1024 * 1024):.0f} MB"


def _cruce_umbral(antes: int, despues: int, limite: int) -> str | None:
    """Nombre del umbral de uso que se CRUZÓ con esta subida (80/95%), o None."""
    for pct in (95, 80):
        umbral = limite * pct // 100
        if antes < umbral <= despues:
            return f"umbral_{pct}"
    return None


def _mensaje_lleno(usados: int, limite: int, plan: str, faltan: int, es_reemplazo: bool) -> str:
    base = (
        f"Tu espacio está lleno ({_fmt_gb(usados)} de {_fmt_gb(limite)} "
        f"en el plan {plan})."
    )
    if es_reemplazo:
        base += f" La nueva imagen pesa {_fmt_gb(faltan)} más de lo que te queda; "
    else:
        base += f" La imagen pesa {_fmt_gb(faltan)} y no cabe. "
    return base + "Libera espacio borrando imágenes, o mejora a Plus."


# ── Lectura (medidor) ────────────────────────────────────────────────────────

# ── Desglose en 3 categorías (Imágenes / Datos / Historial) ──────────────────
# El tenant no distingue foto principal de galería ni logo de banner: la
# categoría "imagenes" abarca TODO lo de Cloudinary (es lo único que cuenta
# en el límite). Datos e historial son ESTIMACIONES nocturnas cacheadas
# (migración 073): prorrateamos el pg_total_relation_size de cada tabla por
# la proporción de filas del tenant. Cacheadas para que /uso (poll de 60 s)
# sea una lectura barata, no un cálculo pesado por request.

_TABLAS_INFRA = {"storage_eventos", "tenant_storage", "storage_avisos", "schema_migrations"}

_TABLAS_DATOS = {
    "productos", "lotes", "clientes", "categorias", "gastos_categorias",
    "producto_variaciones", "producto_imagenes", "producto_recetas",
    "catalogo_config", "catalogo_publicado", "mesas", "terminales",
    "dispositivos_licencia", "mcp_api_keys", "cuotas_clientes", "etiquetas",
    "cliente_etiqueta", "post_config", "gastos_programados",
}

_TABLAS_HISTORIAL = {
    "ventas", "gastos", "inventario_movimientos", "registro_actividad",
    "crm_interacciones", "conteos", "conteo_items", "ordenes", "mesa_items",
    "turnos", "puntos_movimientos", "cobros", "sync_log",
}


def _estimar_datos_historial(tenant_id: str, tablas: set[str]) -> dict:
    """Por tabla clasificada: pg_total_relation_size × (filas del tenant /
    filas totales). Solo tablas que existen y tienen columna tenant_id
    (existe-check contra information_schema: las listas pueden nombrar tablas
    de migraciones futuras)."""
    tablas_validas = (_TABLAS_DATOS | _TABLAS_HISTORIAL) & tablas - _TABLAS_INFRA
    resultado = {"datos": 0, "historial": 0}
    for tabla in sorted(tablas_validas):
        categoria = "datos" if tabla in _TABLAS_DATOS else "historial"
        try:
            filas = query(
                f"SELECT pg_total_relation_size('{tabla}'::regclass) AS size, "
                f"count(*) FILTER (WHERE tenant_id = %s) AS filas_tenant, "
                f"count(*) AS filas_total FROM {tabla}",
                (tenant_id,),
            )
            f = filas[0]
            size = int(f["size"] or 0)
            total = int(f["filas_total"] or 0)
            del_tenant = int(f["filas_tenant"] or 0)
            if size <= 0 or total <= 0 or del_tenant <= 0:
                continue
            resultado[categoria] += int(size * del_tenant / total)
        except Exception as e:
            print(f"Estimación: tabla {tabla} omitida ({e})")
    return resultado


def actualizar_estimaciones(tenant_id: str | None = None) -> dict:
    """
    Cron (nocturno): refresca datos_bytes / historial_bytes / sin_uso_bytes
    de cada tenant en tenant_storage (migración 073).

    sin_uso = peso de subidas activas del ledger cuya URL ya no se referencia
    en NINGUNA tabla de NINGÚN tenant (los refs globales se calculan UNA vez
    y se comparten entre todos los tenants de la corrida).
    """
    targets = [{"id": tenant_id}] if tenant_id else query("SELECT id FROM tenants")
    refs_globales: set[str] | None = None
    tablas = _tablas_con_tenant_id()
    actualizados = 0
    for t in targets:
        tid = str(t["id"])
        try:
            if refs_globales is None:
                refs_globales = urls_referenciadas(None)
            datos_hist = _estimar_datos_historial(tid, tablas)
            eventos = query(
                "SELECT url, MAX(bytes) AS bytes FROM storage_eventos "
                "WHERE tenant_id = %s AND evento = 'upload' AND NOT liberado GROUP BY url",
                (tid,),
            )
            sin_uso = sum(
                int(ev["bytes"] or 0)
                for ev in eventos
                if ev["url"] not in refs_globales
            )
            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO tenant_storage "
                        "(tenant_id, datos_bytes, historial_bytes, sin_uso_bytes) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (tenant_id) DO UPDATE SET "
                        "datos_bytes = EXCLUDED.datos_bytes, "
                        "historial_bytes = EXCLUDED.historial_bytes, "
                        "sin_uso_bytes = EXCLUDED.sin_uso_bytes, "
                        "actualizado_en = now()",
                        (tid, datos_hist["datos"], datos_hist["historial"], sin_uso),
                    )
                conn.commit()
            except (UndefinedColumn, UndefinedTable):
                # Migración 073 aún no aplicada (conn cruda no dispara el
                # auto-heal de conexion.py): migrar y reintentar UNA vez.
                conn.rollback()
                from database.conexion import inicializar_db
                inicializar_db()
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO tenant_storage "
                        "(tenant_id, datos_bytes, historial_bytes, sin_uso_bytes) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (tenant_id) DO UPDATE SET "
                        "datos_bytes = EXCLUDED.datos_bytes, "
                        "historial_bytes = EXCLUDED.historial_bytes, "
                        "sin_uso_bytes = EXCLUDED.sin_uso_bytes, "
                        "actualizado_en = now()",
                        (tid, datos_hist["datos"], datos_hist["historial"], sin_uso),
                    )
                conn.commit()
            finally:
                release_conn(conn)
            actualizados += 1
        except Exception as e:
            print(f"Error estimando datos/historial de {tid}: {e}")
    return {"ok": True, "actualizados": actualizados}


def _tablas_con_tenant_id() -> set[str]:
    """Tablas del schema public que tienen columna tenant_id (existe-check:
    las listas de arriba pueden nombrar tablas de migraciones futuras)."""
    filas = query(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'tenant_id'"
    )
    return {f["table_name"] for f in filas}


def uso_actual(tenant_id: str) -> dict:
    """
    Estado del medidor para el frontend (StorageMeter / pre-check de subida).
    Límite SIEMPRE vía JOIN con planes_config: cambiar de plan surte efecto
    al instante sin tocar el contador.
    """
    filas = query(
        "SELECT p.plan, p.nombre, p.limite_bytes, "
        "COALESCE(s.bytes_usados, 0) AS bytes_usados, "
        "COALESCE(s.bytes_reales, 0) AS bytes_reales "
        "FROM tenants t "
        "JOIN planes_config p ON p.plan = t.plan "
        "LEFT JOIN tenant_storage s ON s.tenant_id = t.id "
        "WHERE t.id = %s",
        (tenant_id,),
    )
    if not filas:
        return {"ok": False, "status": 422,
                "mensaje": "Tu plan no está reconocido. Contacta a soporte."}
    f = filas[0]
    usados = int(f["bytes_usados"])
    limite = int(f["limite_bytes"])

    # Desglose en 3 categorías (migración 073): "imagenes" es la MISMA cifra
    # del límite; datos/historial son estimaciones nocturnas cacheadas (no
    # ocupan el espacio de imágenes — el frontend lo aclara). "sin_uso"
    # (huérfanas) solo aparece cuando existe: es la pista accionable.
    desglose: dict = {"imagenes": usados}
    fila_ts = query(
        "SELECT datos_bytes, historial_bytes, sin_uso_bytes "
        "FROM tenant_storage WHERE tenant_id = %s",
        (tenant_id,),
    )
    if fila_ts:
        if int(fila_ts[0]["datos_bytes"] or 0) > 0:
            desglose["datos"] = int(fila_ts[0]["datos_bytes"])
        if int(fila_ts[0]["historial_bytes"] or 0) > 0:
            desglose["historial"] = int(fila_ts[0]["historial_bytes"])
        if int(fila_ts[0]["sin_uso_bytes"] or 0) > 0:
            desglose["sin_uso"] = int(fila_ts[0]["sin_uso_bytes"])

    return {
        "ok": True,
        "plan": f["plan"],
        "nombre": f["nombre"],
        "limite_bytes": limite,
        "bytes_usados": usados,
        "bytes_reales": int(f["bytes_reales"]),
        "porcentaje": round(usados / limite * 100, 1) if limite > 0 else 0.0,
        "quedan_bytes": max(0, limite - usados),
        "excedido": usados > limite,
        "desglose": desglose,
    }


# ── El guardián ──────────────────────────────────────────────────────────────

def reservar(tenant_id: str, bytes_nuevos: int, url_a_reemplazar: str | None = None) -> dict:
    """
    Reserva espacio ANTES de subir al proveedor. Transacción con lock de fila
    (FOR UPDATE): dos subidas simultáneas no pueden pasar ambas con el último
    espacio libre.

    url_a_reemplazar: URL del asset que el flujo destruirá tras subir el
    nuevo (foto de variación, slot de galería, logo/banner). Su peso cuenta
    como disponible — ver REEMPLAZO 1:1 en el encabezado del módulo.

    Devuelve:
      {"ok": True, "aviso": "umbral_80"|None}          → puede subir
      {"ok": False, "status": 422, "codigo": "CUOTA_LLENA", ...} → bloqueado
    """
    bytes_nuevos = int(bytes_nuevos or 0)
    if bytes_nuevos < 0:
        return {"ok": False, "status": 400, "mensaje": "Tamaño de archivo inválido."}
    if bytes_nuevos == 0:
        return {"ok": True, "aviso": None}

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # El contador nace en 0 en la primera subida del tenant
            cur.execute(
                "INSERT INTO tenant_storage (tenant_id) VALUES (%s) "
                "ON CONFLICT (tenant_id) DO NOTHING",
                (tenant_id,),
            )
            # Lock del contador + límite del plan ACTUAL (JOIN)
            cur.execute(
                "SELECT s.bytes_usados, p.limite_bytes, p.nombre AS plan_nombre "
                "FROM tenant_storage s "
                "JOIN tenants t ON t.id = s.tenant_id "
                "JOIN planes_config p ON p.plan = t.plan "
                "WHERE s.tenant_id = %s "
                "FOR UPDATE OF s",
                (tenant_id,),
            )
            fila = cur.fetchone()
            if not fila:
                conn.rollback()
                return {"ok": False, "status": 422,
                        "mensaje": "Tu plan no está reconocido. Contacta a soporte."}
            usados = int(fila["bytes_usados"])
            limite = int(fila["limite_bytes"])
            plan_nombre = fila["plan_nombre"]

            # Crédito de reemplazo: el peso del asset que sale
            bytes_viejos = 0
            evento_viejo_id = None
            url_vieja = (url_a_reemplazar or "").strip()
            if url_vieja:
                cur.execute(
                    "SELECT id, bytes FROM storage_eventos "
                    "WHERE tenant_id = %s AND url = %s "
                    "AND evento = 'upload' AND NOT liberado "
                    "ORDER BY id DESC LIMIT 1 "
                    "FOR UPDATE",
                    (tenant_id, url_vieja),
                )
                ev = cur.fetchone()
                if ev:
                    bytes_viejos = int(ev["bytes"] or 0)
                    evento_viejo_id = ev["id"]

            despues = usados + bytes_nuevos - bytes_viejos
            if despues > limite:
                conn.rollback()
                return {
                    "ok": False,
                    "status": 422,
                    "codigo": "CUOTA_LLENA",
                    "plan": plan_nombre,
                    "limite_bytes": limite,
                    "bytes_usados": usados,
                    "quedan_bytes": max(0, limite - usados),
                    "excedente_bytes": max(0, despues - limite),
                    "mensaje": _mensaje_lleno(
                        usados, limite, plan_nombre,
                        max(0, bytes_nuevos - max(0, limite - usados)),
                        es_reemplazo=bool(bytes_viejos),
                    ),
                }

            nuevo_uso = max(0, despues)
            cur.execute(
                "UPDATE tenant_storage SET bytes_usados = %s, actualizado_en = now() "
                "WHERE tenant_id = %s",
                (nuevo_uso, tenant_id),
            )
            if bytes_viejos:
                # El crédito se ejerce YA: el asset viejo quedará destruido
                # tras la subida (el flujo que pidió el reemplazo lo borra);
                # si el flujo muere a medias, la reconciliación corrige.
                cur.execute(
                    "UPDATE storage_eventos SET liberado = TRUE WHERE id = %s",
                    (evento_viejo_id,),
                )
                cur.execute(
                    "INSERT INTO storage_eventos "
                    "(tenant_id, url, bytes, evento, liberado, detalle) "
                    "VALUES (%s, %s, %s, 'delete', FALSE, %s)",
                    (tenant_id, url_vieja, bytes_viejos,
                     "Reemplazo: crédito ejercido en reservar()"),
                )
            aviso = _cruce_umbral(usados, nuevo_uso, limite)
        conn.commit()
        return {"ok": True, "aviso": aviso}
    except Exception as e:
        conn.rollback()
        # FAIL-OPEN (ver encabezado del módulo): un error interno del medidor
        # no bloquea la subida; el drift lo corrige la reconciliación.
        print(f"⚠️ Error reservando cuota (fail-open, la subida pasa): {e}")
        return {"ok": True, "aviso": None, "degradado": True}
    finally:
        release_conn(conn)


def confirmar(tenant_id: str, url: str, bytes_subidos: int,
              bytes_reales: int | None = None, detalle: str | None = None) -> dict:
    """
    Registra en el ledger una subida EXITOSA al proveedor (el contador de uso
    ya lo cobró reservar()). bytes_reales = lo que pesa de verdad en el
    proveedor (Cloudinary lo devuelve en su respuesta).
    """
    url = (url or "").strip()
    bytes_subidos = int(bytes_subidos or 0)
    reales = int(bytes_reales if bytes_reales is not None else bytes_subidos)
    if not url or bytes_subidos <= 0:
        return {"ok": False, "mensaje": "Datos de subida incompletos"}
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO storage_eventos "
                    "(tenant_id, url, bytes, bytes_reales, evento, liberado, detalle) "
                    "VALUES (%s, %s, %s, %s, 'upload', FALSE, %s)",
                    (tenant_id, url, bytes_subidos, reales, detalle),
                )
                cur.execute(
                    "UPDATE tenant_storage SET bytes_reales = bytes_reales + %s, "
                    "actualizado_en = now() WHERE tenant_id = %s",
                    (reales, tenant_id),
                )
            conn.commit()
        finally:
            release_conn(conn)
        return {"ok": True}
    except Exception as e:
        print(f"⚠️ Error confirmando subida en el ledger (el asset sí se subió): {e}")
        return {"ok": False, "mensaje": str(e)}


# ── Liberación ───────────────────────────────────────────────────────────────

def liberar_url(url: str, tenant_id: str | None = None) -> dict:
    """
    Descuenta del medidor el peso de la última subida ACTIVA de esa URL.
    Idempotente: si ya fue liberada (o nunca se rastreó — imágenes legadas
    previas al backfill), no resta nada. Best-effort: nunca rompe el flujo.
    """
    url = (url or "").strip()
    if not url:
        return {"ok": True, "mensaje": "Nada que liberar"}
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if not tenant_id:
                # El ledger conoce al dueño de la URL (una URL subida pertenece
                # a un tenant; borrar_imagen_cloudinary llama sin tenant_id)
                cur.execute(
                    "SELECT tenant_id FROM storage_eventos "
                    "WHERE url = %s ORDER BY id DESC LIMIT 1",
                    (url,),
                )
                fila = cur.fetchone()
                if not fila:
                    conn.rollback()
                    return {"ok": True, "mensaje": "URL sin seguimiento (legada)"}
                tenant_id = str(fila["tenant_id"])
            cur.execute(
                "SELECT id, bytes, bytes_reales FROM storage_eventos "
                "WHERE tenant_id = %s AND url = %s "
                "AND evento = 'upload' AND NOT liberado "
                "ORDER BY id DESC LIMIT 1 "
                "FOR UPDATE",
                (tenant_id, url),
            )
            ev = cur.fetchone()
            if not ev:
                conn.rollback()
                return {"ok": True, "mensaje": "Ya liberada o sin seguimiento"}
            cur.execute(
                "UPDATE storage_eventos SET liberado = TRUE WHERE id = %s",
                (ev["id"],),
            )
            cur.execute(
                "INSERT INTO storage_eventos "
                "(tenant_id, url, bytes, evento, liberado, detalle) "
                "VALUES (%s, %s, 0, 'delete', FALSE, %s)",
                (tenant_id, url, f"Liberados {_fmt_gb(int(ev['bytes'] or 0))}"),
            )
            cur.execute(
                "UPDATE tenant_storage SET "
                "bytes_usados = GREATEST(0, bytes_usados - %s), "
                "bytes_reales = GREATEST(0, bytes_reales - %s), "
                "actualizado_en = now() "
                "WHERE tenant_id = %s",
                (int(ev["bytes"] or 0), int(ev["bytes_reales"] or 0), tenant_id),
            )
        conn.commit()
        return {"ok": True, "bytes_liberados": int(ev["bytes"] or 0)}
    except Exception as e:
        conn.rollback()
        print(f"Error liberando cuota de {url}: {e}")
        return {"ok": False, "mensaje": str(e)}
    finally:
        release_conn(conn)


def liberar_reserva(tenant_id: str, bytes_nuevos: int) -> None:
    """
    Compensación cuando la subida al proveedor FALLÓ después de reservar:
    devuelve el espacio reservado (el contador queda como estaba).
    """
    bytes_nuevos = int(bytes_nuevos or 0)
    if bytes_nuevos <= 0:
        return
    try:
        execute(
            "UPDATE tenant_storage SET bytes_usados = GREATEST(0, bytes_usados - %s), "
            "actualizado_en = now() WHERE tenant_id = %s",
            (bytes_nuevos, tenant_id),
        )
    except Exception as e:
        print(f"⚠️ Error liberando reserva fallida ({bytes_nuevos} bytes): {e}")


# ── Mantenimiento y soporte ──────────────────────────────────────────────────

def reconciliar(tenant_id: str | None = None) -> dict:
    """
    Recomputa el medidor desde el ledger (fuente de verdad):

        bytes_usados = SUM(bytes de 'upload' NO liberados)
                     + SUM(bytes de 'ajuste')          # ajustes pueden ser negativos

    Detecta drift de requests que crashearon a medias (reservó y no subió,
    subió y no confirmó). Idempotente; registra un evento por corrección.
    Sin tenant_id revisa todos.
    """
    if tenant_id:
        targets = [{"id": tenant_id}]
    else:
        targets = query("SELECT id FROM tenants")
    corregidos = []
    for t in targets:
        tid = str(t["id"])
        try:
            filas = query(
                "SELECT COALESCE(SUM(CASE WHEN evento = 'upload' AND NOT liberado "
                "                     THEN bytes ELSE 0 END), 0) "
                "     + COALESCE(SUM(CASE WHEN evento = 'ajuste' "
                "                     THEN bytes ELSE 0 END), 0) AS recomputado "
                "FROM storage_eventos WHERE tenant_id = %s",
                (tid,),
            )
            recomputado = int(filas[0]["recomputado"] or 0)
            actuales = query(
                "SELECT bytes_usados FROM tenant_storage WHERE tenant_id = %s",
                (tid,),
            )
            usados = int(actuales[0]["bytes_usados"]) if actuales else 0
            if recomputado != usados:
                conn = get_conn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO tenant_storage (tenant_id, bytes_usados) "
                            "VALUES (%s, %s) "
                            "ON CONFLICT (tenant_id) DO UPDATE SET "
                            "bytes_usados = EXCLUDED.bytes_usados, "
                            "actualizado_en = now()",
                            (tid, recomputado),
                        )
                        cur.execute(
                            "INSERT INTO storage_eventos "
                            "(tenant_id, url, bytes, evento, liberado, detalle) "
                            "VALUES (%s, '', %s, 'reconciliacion', FALSE, %s)",
                            (tid, recomputado - usados,
                             f"Drift {usados} → {recomputado}"),
                        )
                    conn.commit()
                finally:
                    release_conn(conn)
                corregidos.append({"tenant_id": tid, "antes": usados, "despues": recomputado})
        except Exception as e:
            print(f"Error reconciliando {tid}: {e}")
    return {"ok": True, "revisados": len(targets), "corregidos": corregidos}


def ajustar(tenant_id: str, delta_bytes: int, motivo: str) -> dict:
    """
    Corrección manual de soporte (delta puede ser negativo): ajusta el
    contador y deja rastro auditado en el ledger con evento 'ajuste'.
    """
    delta_bytes = int(delta_bytes or 0)
    if delta_bytes == 0 or not (motivo or "").strip():
        return {"ok": False, "status": 400,
                "mensaje": "Se requiere delta != 0 y motivo."}
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO tenant_storage (tenant_id, bytes_usados) "
                    "VALUES (%s, GREATEST(0, %s)) "
                    "ON CONFLICT (tenant_id) DO UPDATE SET "
                    "bytes_usados = GREATEST(0, tenant_storage.bytes_usados + EXCLUDED.bytes_usados), "
                    "actualizado_en = now()",
                    (tenant_id, delta_bytes),
                )
                cur.execute(
                    "INSERT INTO storage_eventos "
                    "(tenant_id, url, bytes, evento, liberado, detalle) "
                    "VALUES (%s, '', %s, 'ajuste', FALSE, %s)",
                    (tenant_id, delta_bytes, motivo.strip()),
                )
            conn.commit()
        finally:
            release_conn(conn)
        return {"ok": True, "delta_bytes": delta_bytes}
    except Exception as e:
        print(f"Error ajustando cuota de {tenant_id}: {e}")
        return {"ok": False, "status": 500, "mensaje": str(e)}


# ── Gestor de espacio (Fase B) ───────────────────────────────────────────────

def listar_imagenes(tenant_id: str) -> dict:
    """
    Inventario de imágenes del tenant para el gestor de espacio:
    [{url, bytes, fuente, contexto, liberable}] ordenadas por peso desc.

    - bytes: del ledger (última subida activa de esa URL). 0 = imagen legada
      sin backfill (se lista igual, para que el tenant la pueda liberar).
    - liberable=False para fotos de catalogo_publicado: viven en el snapshot
      del catálogo del Modo Local y se gestionan REPUBLICANDO desde la app,
      no borrándolas aquí (borrarlas rompería el catálogo publicado).
    """
    marcas = "%res.cloudinary.com%"

    # 1. Mapa url → bytes desde el ledger (última subida activa por URL)
    eventos = query(
        "SELECT url, MAX(bytes) AS bytes FROM storage_eventos "
        "WHERE tenant_id = %s AND evento = 'upload' AND NOT liberado "
        "GROUP BY url",
        (tenant_id,),
    )
    pesos = {ev["url"]: int(ev["bytes"] or 0) for ev in eventos}

    items: dict[str, dict] = {}

    def _agregar(url: str, fuente: str, contexto: str, liberable: bool) -> None:
        url = (url or "").strip()
        if not url or url in items or CLOUDINARY_MARK not in url:
            return
        items[url] = {
            "url": url,
            "bytes": pesos.get(url, 0),
            "fuente": fuente,
            "contexto": contexto,
            "liberable": liberable,
        }

    try:
        # Foto principal de producto
        for fila in query(
            "SELECT Imagen AS url, Producto AS producto FROM productos "
            "WHERE tenant_id = %s AND Imagen LIKE %s",
            (tenant_id, marcas),
        ):
            _agregar(fila["url"], "Foto principal", fila.get("producto") or "Producto", True)
        # Galería (plan Plus)
        for fila in query(
            "SELECT pi.url, p.Producto AS producto FROM producto_imagenes pi "
            "JOIN productos p ON p.id = pi.producto_id "
            "WHERE pi.tenant_id = %s AND pi.url LIKE %s",
            (tenant_id, marcas),
        ):
            _agregar(fila["url"], "Galería", fila.get("producto") or "Producto", True)
        # Foto de variación
        for fila in query(
            "SELECT pv.foto AS url, pv.nombre AS variacion, p.Producto AS producto "
            "FROM producto_variaciones pv JOIN productos p ON p.id = pv.producto_id "
            "WHERE pv.tenant_id = %s AND pv.foto LIKE %s",
            (tenant_id, marcas),
        ):
            _agregar(fila["url"], "Variación",
                     f"{fila.get('producto') or 'Producto'} · {fila.get('variacion') or ''}".strip(" ·"),
                     True)
        # Branding
        for fila in query(
            "SELECT id, logo FROM tenants WHERE id = %s AND logo LIKE %s",
            (tenant_id, marcas),
        ):
            _agregar(fila["logo"], "Branding", "Logo del negocio", True)
        filas_cfg = query(
            "SELECT banner_url, banner_url_movil, hero_url, hero_url_movil "
            "FROM catalogo_config WHERE tenant_id = %s",
            (tenant_id,),
        )
        if filas_cfg:
            cfg = filas_cfg[0]
            for columna, etiqueta in (
                ("banner_url", "Banner escritorio"),
                ("banner_url_movil", "Banner móvil"),
                ("hero_url", "Hero escritorio"),
                ("hero_url_movil", "Hero móvil"),
            ):
                _agregar(cfg.get(columna) or "", "Branding", etiqueta, True)
        # Catálogo publicado (Modo Local): URLs dentro del JSONB del snapshot.
        # liberable=False: se gestionan republicando, no borrando aquí.
        for fila in query(
            "SELECT clave, data FROM catalogo_publicado WHERE tenant_id = %s",
            (tenant_id,),
        ):
            data = fila.get("data")
            if isinstance(data, str):
                try:
                    import json as _json
                    data = _json.loads(data)
                except (TypeError, ValueError):
                    data = None
            urls_pub = _urls_cloudinary_en_json(data)
            for url_pub in urls_pub:
                _agregar(url_pub, "Catálogo publicado",
                         fila.get("clave") or "snapshot", False)
    except Exception as e:
        print(f"Error listando imágenes de {tenant_id}: {e}")
        return {"ok": False, "status": 500,
                "mensaje": "No se pudo leer el inventario de imágenes"}

    ordenado = sorted(items.values(), key=lambda i: i["bytes"], reverse=True)
    return {"ok": True, "imagenes": ordenado,
            "total_bytes": sum(i["bytes"] for i in ordenado)}


def _urls_cloudinary_en_json(obj) -> list[str]:
    """URLs de Cloudinary dentro de una estructura JSON (el snapshot del
    catálogo publicado guarda las fotos como strings en varias claves)."""
    encontrados: set[str] = set()

    def _rec(o) -> None:
        if isinstance(o, dict):
            for v in o.values():
                _rec(v)
        elif isinstance(o, list):
            for v in o:
                _rec(v)
        elif isinstance(o, str) and o.startswith("http") and CLOUDINARY_MARK in o:
            encontrados.add(o.strip())

    _rec(obj)
    return sorted(encontrados)


# ── Huérfanos (Fase C) ───────────────────────────────────────────────────────

def urls_referenciadas(tenant_id: str | None = None) -> set[str]:
    """
    Todas las URLs de Cloudinary referenciadas en las tablas de negocio.
    Sin tenant_id revisa TODOS los tenants (una foto huérfana de un tenant
    nunca se reporta si otra tabla —de quien sea— la sigue usando).
    Incluye el JSONB de catalogo_publicado (Modo Local).
    """
    marcas = "%res.cloudinary.com%"
    refs: set[str] = set()

    def _colectar(sql: str, params: tuple, columnas: tuple[str, ...]) -> None:
        for f in query(sql, params):
            for col in columnas:
                v = f.get(col)
                if v and CLOUDINARY_MARK in v:
                    refs.add(v.strip())

    # ── Columnas simples (LIKE filtra en BD; el filtro Python es por si acaso)
    _colectar(
        "SELECT Imagen AS url FROM productos WHERE Imagen LIKE %s" + (" AND tenant_id = %s" if tenant_id else ""),
        (marcas, tenant_id) if tenant_id else (marcas,),
        ("url",),
    )
    _colectar(
        "SELECT url FROM producto_imagenes WHERE url LIKE %s" + (" AND tenant_id = %s" if tenant_id else ""),
        (marcas, tenant_id) if tenant_id else (marcas,),
        ("url",),
    )
    _colectar(
        "SELECT foto AS url FROM producto_variaciones WHERE foto LIKE %s" + (" AND tenant_id = %s" if tenant_id else ""),
        (marcas, tenant_id) if tenant_id else (marcas,),
        ("url",),
    )
    _colectar(
        "SELECT logo AS url FROM tenants WHERE logo LIKE %s" + (" AND id = %s" if tenant_id else ""),
        (marcas, tenant_id) if tenant_id else (marcas,),
        ("url",),
    )
    # ── Branding: 4 columnas en una fila
    _colectar(
        "SELECT banner_url AS c1, banner_url_movil AS c2, hero_url AS c3, hero_url_movil AS c4 "
        "FROM catalogo_config" + (" WHERE tenant_id = %s" if tenant_id else ""),
        (tenant_id,) if tenant_id else (),
        ("c1", "c2", "c3", "c4"),
    )
    # ── Catálogo publicado (Modo Local): URLs dentro del JSONB
    for f in query(
        "SELECT data FROM catalogo_publicado" + (" WHERE tenant_id = %s" if tenant_id else ""),
        (tenant_id,) if tenant_id else (),
    ):
        data = f.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                data = None
        for url in _urls_cloudinary_en_json(data):
            refs.add(url)

    return refs


def detectar_huerfanos(tenant_id: str | None = None) -> list[dict]:
    """
    Subidas activas en el ledger cuya URL ya NO se referencia en NINGUNA
    tabla (de ningún tenant): fotos subidas y nunca guardadas (el tenant
    cerró el formulario), o huérfanas de reemplazos fallidos. Siguen
    costando dinero en el proveedor.
    """
    sql = (
        "SELECT id, tenant_id, url, bytes, creado_en FROM storage_eventos "
        "WHERE evento = 'upload' AND NOT liberado"
    )
    params: tuple = ()
    if tenant_id:
        sql += " AND tenant_id = %s"
        params = (tenant_id,)
    eventos = query(sql, params)
    refs = urls_referenciadas(None)
    return [e for e in eventos if e["url"] not in refs]


# ── Avisos por umbrales (Fase C) ─────────────────────────────────────────────

def procesar_avisos(tenant_id: str | None = None) -> list[dict]:
    """
    Cron-driven (scripts/reconciliar_storage.py): revisa el porcentaje de uso
    de cada tenant y registra los avisos al cruzar 80% / 95% / 100%.

    Dedupe: un aviso por (tenant, umbral) HASTA que el uso vuelva a bajar de
    ese umbral — entonces la fila se borra y un nuevo cruce vuelve a avisar.
    Nunca corre en el path de subida (el cron paga la latencia del envío).

    Devuelve los avisos NUEVOS (los que se acaban de cruzar):
      [{tenant_id, empresa, correo, umbral, plan, porcentaje, bytes_usados, limite_bytes}]
    """
    sql = (
        "SELECT t.id, t.empresa, t.correo, p.nombre AS plan, p.limite_bytes, "
        "COALESCE(s.bytes_usados, 0) AS bytes_usados "
        "FROM tenants t "
        "JOIN planes_config p ON p.plan = t.plan "
        "LEFT JOIN tenant_storage s ON s.tenant_id = t.id"
    )
    params: tuple = ()
    if tenant_id:
        sql += " WHERE t.id = %s"
        params = (tenant_id,)

    nuevos: list[dict] = []
    for f in query(sql, params):
        usados = int(f["bytes_usados"] or 0)
        limite = int(f["limite_bytes"] or 0)
        if limite <= 0:
            continue
        pct = usados / limite * 100
        for umbral in (80, 95, 100):
            if pct >= umbral:
                filas = query(
                    "INSERT INTO storage_avisos (tenant_id, umbral) VALUES (%s, %s) "
                    "ON CONFLICT (tenant_id, umbral) DO NOTHING RETURNING tenant_id",
                    (f["id"], umbral),
                )
                if filas:
                    nuevos.append({
                        "tenant_id": str(f["id"]),
                        "empresa": f.get("empresa") or "tu negocio",
                        "correo": (f.get("correo") or "").strip(),
                        "umbral": umbral,
                        "plan": f["plan"],
                        "porcentaje": round(pct, 1),
                        "bytes_usados": usados,
                        "limite_bytes": limite,
                    })
            else:
                execute(
                    "DELETE FROM storage_avisos WHERE tenant_id = %s AND umbral = %s",
                    (f["id"], umbral),
                )
    return nuevos
