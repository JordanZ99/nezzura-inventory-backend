# ==============================================================================
# backend/scripts/backfill_storage.py
# Backfill del medidor de almacenamiento (migración 071).
#
# Siembra la cuota de cada tenant con el peso de las imágenes que YA tiene en
# Cloudinary, para que el medidor nazca honesto: nadie amanece "en cero"
# mintiendo ni bloqueado por sorpresa.
#
# Fuentes (las mismas columnas que audit_imagenes cruza):
#   productos.Imagen, producto_imagenes.url, producto_variaciones.foto,
#   tenants.logo, catalogo_config.banner_url/banner_url_movil,
#   catalogo_config.hero_url/hero_url_movil.
#
# El peso REAL de cada asset se lee de la Admin API de Cloudinary (los assets
# ya borrados cuentan 0 y se avisan: no deben cobrarse).
#
# Cada URL se registra como evento 'upload' (liberado=FALSE, detalle
# 'backfill inicial'): así las liberaciones futuras (borrar imagen →
# liberar_url) funcionan igual que para subidas nuevas.
#
# Uso (desde backend/):
#   python -m scripts.backfill_storage           # dry-run: solo reporta
#   python -m scripts.backfill_storage --apply   # escribe en la BD
#   python -m scripts.backfill_storage --tenant <uuid> [--apply]
#
# Idempotente: las URLs ya registradas en storage_eventos se saltan, así que
# se puede re-ejecutar sin inflar el medidor.
# ==============================================================================

import argparse
import json
import sys
from pathlib import Path

# Permitir importar database.* corriendo desde backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cloudinary.api  # noqa: E402
from database.conexion import query, get_conn, release_conn  # noqa: E402
from database.imagenes import _extraer_public_id  # noqa: E402
from database.almacenamiento import _urls_cloudinary_en_json  # noqa: E402

# Credenciales (mismo patrón que database/imagenes.py)
import os  # noqa: E402
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
)

CLOUDINARY_MARK = "res.cloudinary.com"
GB = 1073741824


# ── Carga de URLs por tenant ─────────────────────────────────────────────────

def cargar_urls_por_tenant(tenant_id: str | None = None) -> dict[str, set[str]]:
    """URLs de Cloudinary en uso por tenant (DISTINCT, dedupe dentro del tenant)."""
    marcas = "%res.cloudinary.com%"
    # Cada rama del UNION trae su LIKE %s; con filtro de tenant, además su id
    if tenant_id:
        filtro = " AND t.id = %s"
        params = (marcas, tenant_id) * 8
    else:
        filtro = ""
        params = (marcas,) * 8
    filas = query(
        "SELECT t.id AS tenant_id, p.Imagen AS url "
        "FROM tenants t JOIN productos p ON p.tenant_id = t.id "
        "WHERE p.Imagen LIKE %s" + filtro +
        " UNION SELECT t.id, pi.url "
        "FROM tenants t JOIN producto_imagenes pi ON pi.tenant_id = t.id "
        "WHERE pi.url LIKE %s" + filtro +
        " UNION SELECT t.id, pv.foto "
        "FROM tenants t JOIN producto_variaciones pv ON pv.tenant_id = t.id "
        "WHERE pv.foto LIKE %s" + filtro +
        " UNION SELECT t.id, t.logo "
        "FROM tenants t WHERE t.logo LIKE %s" + filtro +
        " UNION SELECT t.id, cc.banner_url "
        "FROM tenants t JOIN catalogo_config cc ON cc.tenant_id = t.id "
        "WHERE cc.banner_url LIKE %s" + filtro +
        " UNION SELECT t.id, cc.banner_url_movil "
        "FROM tenants t JOIN catalogo_config cc ON cc.tenant_id = t.id "
        "WHERE cc.banner_url_movil LIKE %s" + filtro +
        " UNION SELECT t.id, cc.hero_url "
        "FROM tenants t JOIN catalogo_config cc ON cc.tenant_id = t.id "
        "WHERE cc.hero_url LIKE %s" + filtro +
        " UNION SELECT t.id, cc.hero_url_movil "
        "FROM tenants t JOIN catalogo_config cc ON cc.tenant_id = t.id "
        "WHERE cc.hero_url_movil LIKE %s" + filtro,
        params,
    )
    urls: dict[str, set[str]] = {}
    for fila in filas:
        url = (fila.get("url") or "").strip()
        tid = str(fila["tenant_id"])
        if url and url != "No hay foto" and CLOUDINARY_MARK in url:
            urls.setdefault(tid, set()).add(url)

    # Catálogo publicado (Modo Local): las fotos viven dentro del JSONB del
    # snapshot — no hay columna de URL; se escanea la estructura completa.
    sql_pub = "SELECT tenant_id, data FROM catalogo_publicado"
    args_pub: tuple = ()
    if tenant_id:
        sql_pub += " WHERE tenant_id = %s"
        args_pub = (tenant_id,)
    for fila in query(sql_pub, args_pub):
        data = fila.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                data = None
        for url_pub in _urls_cloudinary_en_json(data):
            urls.setdefault(str(fila["tenant_id"]), set()).add(url_pub)
    return urls


# ── Peso real desde Cloudinary ───────────────────────────────────────────────

def mapa_bytes_cloudinary(prefixes: list[str]) -> dict[str, int]:
    """
    public_id → bytes, listando las carpetas usadas por la app una sola vez
    (evita una llamada Admin API por URL). Dedupe por public_id.
    """
    mapa: dict[str, int] = {}
    for prefix in prefixes:
        cursor = None
        while True:
            kwargs = {"type": "upload", "prefix": prefix, "max_results": 500}
            if cursor:
                kwargs["next_cursor"] = cursor
            res = cloudinary.api.resources(**kwargs)
            for asset in res.get("resources", []):
                pid = asset.get("public_id")
                if pid:
                    mapa[pid] = int(asset.get("bytes") or 0)
            cursor = res.get("next_cursor")
            if not cursor:
                break
    return mapa


def peso_de_url(url: str, mapa: dict[str, int]) -> int | None:
    """Bytes reales del asset (None si ya no existe en Cloudinary → no cobra)."""
    public_id = _extraer_public_id(url)
    if not public_id:
        return None
    return mapa.get(public_id)


# ── Siembra ──────────────────────────────────────────────────────────────────

def urls_ya_registradas(tenant_id: str) -> set[str]:
    filas = query(
        "SELECT DISTINCT url FROM storage_eventos "
        "WHERE tenant_id = %s AND evento = 'upload'",
        (tenant_id,),
    )
    return {f["url"] for f in filas}


def sembrar(tenant_id: str, urls_nuevas: list[tuple[str, int]]) -> None:
    """Inserta los eventos 'upload' del backfill y recalcula el contador desde
    el ledger completo (incluye subidas ya rastreadas si la app siguió
    operando entre el deploy y este backfill). Transacción."""
    from psycopg2.extras import RealDictCursor

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for url, bytes_ in urls_nuevas:
                cur.execute(
                    "INSERT INTO storage_eventos "
                    "(tenant_id, url, bytes, bytes_reales, evento, liberado, detalle) "
                    "VALUES (%s, %s, %s, %s, 'upload', FALSE, %s)",
                    (tenant_id, url, bytes_, bytes_, "backfill inicial"),
                )
            # Contador = recomputación del ledger completo (misma fórmula de
            # almacenamiento.reconciliar): nunca se pierden bytes ya rastreados.
            cur.execute(
                "SELECT COALESCE(SUM(CASE WHEN evento = 'upload' AND NOT liberado "
                "                     THEN bytes ELSE 0 END), 0) "
                "     + COALESCE(SUM(CASE WHEN evento = 'ajuste' "
                "                     THEN bytes ELSE 0 END), 0) AS total "
                "FROM storage_eventos WHERE tenant_id = %s",
                (tenant_id,),
            )
            total = int((cur.fetchone() or {}).get("total") or 0)
            cur.execute(
                "INSERT INTO tenant_storage (tenant_id, bytes_usados) "
                "VALUES (%s, %s) "
                "ON CONFLICT (tenant_id) DO UPDATE SET "
                "bytes_usados = EXCLUDED.bytes_usados, "
                "actualizado_en = now()",
                (tenant_id, total),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_conn(conn)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill del medidor de almacenamiento")
    parser.add_argument("--apply", action="store_true",
                        help="Escribe en la BD (por defecto es dry-run)")
    parser.add_argument("--tenant", help="Limita el backfill a un tenant (uuid)")
    args = parser.parse_args()

    if not (os.getenv("CLOUDINARY_CLOUD_NAME") and os.getenv("CLOUDINARY_API_KEY")):
        print("❌ Faltan CLOUDINARY_CLOUD_NAME / CLOUDINARY_API_KEY en .env")
        sys.exit(1)

    print("Listando assets de Cloudinary (folders de la app)...")
    mapa = mapa_bytes_cloudinary(["productos", "variaciones", "branding"])
    print(f"  {len(mapa)} assets indexados en los folders globales.")

    urls_por_tenant = cargar_urls_por_tenant(args.tenant)
    print(f"Tenants con imágenes: {len(urls_por_tenant)}")

    for tid, urls in urls_por_tenant.items():
        ya = urls_ya_registradas(tid)
        nuevas = sorted(urls - ya)
        if not nuevas:
            print(f"\nTenant {tid[:8]}…: sin URLs nuevas (ya registrado). Saltando.")
            continue

        mapa_tenant = dict(mapa)
        # Carpeta dedicada del catálogo publicado de este tenant (modo local)
        mapa_tenant.update(mapa_bytes_cloudinary([f"catalogo_publicado/{tid}"]))

        sembradas: list[tuple[str, int]] = []
        faltantes = 0
        total = 0
        for url in nuevas:
            bytes_ = peso_de_url(url, mapa_tenant)
            if bytes_ is None:
                faltantes += 1
                continue  # asset ya borrado: no se cobra
            sembradas.append((url, bytes_))
            total += bytes_

        plan = query("SELECT plan FROM tenants WHERE id = %s", (tid,))
        limite = query(
            "SELECT p.limite_bytes FROM tenants t "
            "JOIN planes_config p ON p.plan = t.plan WHERE t.id = %s",
            (tid,),
        )
        lim = limite[0]["limite_bytes"] if limite else 0
        pct = f"{total / lim * 100:.1f}%" if lim else "—"
        print(f"\nTenant {tid[:8]}… plan {plan[0]['plan'] if plan else '?'}: "
              f"{len(nuevas)} URLs nuevas, {len(sembradas)} rastreables, "
              f"{faltantes} assets ya borrados.")
        print(f"  Peso total a sembrar: {total / GB:.2f} GB "
              f"({pct} del límite de {lim / GB:.0f} GB)")

        if not args.apply:
            continue
        sembrar(tid, sembradas)
        print("  [OK] Sembrado.")

    if not args.apply:
        print("\n(Dry-run: no se escribió nada. Pasa --apply para sembrar.)")


if __name__ == "__main__":
    main()
