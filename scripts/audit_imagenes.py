# ==============================================================================
# backend/scripts/audit_imagenes.py
# Auditoría y reparación estructural de imágenes de productos (Cloudinary + BD).
#
# Detecta y repara los problemas derivados del bug de la galería:
#   - La foto principal (productos.imagen) fuera de producto_imagenes (desync).
#   - URLs duplicadas dentro de la galería de un producto.
#   - Filas con URLs rotas/no-Cloudinary.
#   - Órdenes no secuenciales (huecos).
#   - Assets huérfanos en Cloudinary (solo lectura, con --cloudinary).
#   - URLs que ya no existen en Cloudinary (con --check-urls).
#
# Uso (desde backend/):
#   python -m scripts.audit_imagenes report     [--producto "X"] [--check-urls] [--cloudinary]
#   python -m scripts.audit_imagenes fix        [--producto "X"] [--apply]
#   python -m scripts.audit_imagenes restaurar  [--apply]
#
# modos:
#   report     — lista los productos con problemas (solo lectura, no cambia nada).
#   fix        — repara la estructura en BD (dedupe, sincroniza la principal con
#                la galería, renumera órdenes). Por defecto es dry-run: hay que
#                pasar --apply para escribir en la BD. NUNCA destruye assets de
#                Cloudinary automáticamente.
#   restaurar  — recupera fotos principales PISADAS por el bug del slot 1 (la
#                primera imagen extra sobrescribía productos.imagen). Cruza los
#                assets huérfanos de Cloudinary contra los productos y propone
#                restaurar productos.imagen. Dry-run por defecto (--apply para
#                escribir en la BD). NUNCA borra nada de Cloudinary.
#
# flags:
#   --producto   — limita la operación a un producto concreto (por nombre).
#   --check-urls — hace HEAD a cada URL de Cloudinary para detectar rotas.
#   --cloudinary — lista los assets de Cloudinary y cruza contra la BD para
#                  estimar cuántos quedaron huérfanos (solo lectura).
#   --apply      — (fix) aplica los cambios de verdad (sin esto es dry-run).
# ==============================================================================

import argparse
import sys
import urllib.request
from collections import Counter
from pathlib import Path

# Permitir importar database.* corriendo desde backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.conexion import query, execute  # noqa: E402

CLOUDINARY_MARK = "res.cloudinary.com"


# ── Helpers de BD ─────────────────────────────────────────────────────────────

def cargar_productos():
    # Alias a minúscula: PostgreSQL folda los identificadores no citados y
    # psycopg2 entrega las claves del dict tal como el servidor las reporta
    # ("producto", "imagen"). Sin el alias, las lecturas con "Imagen"/
    # "Producto" fallaban en silencio (y "Producto" lanzaba KeyError).
    return query(
        "SELECT id, Producto AS producto, Imagen AS imagen, tenant_id "
        "FROM productos ORDER BY Producto ASC"
    )


def cargar_galerias():
    return query(
        "SELECT id, producto_id, url, orden FROM producto_imagenes ORDER BY producto_id, orden"
    )


def cargar_planes():
    planes = {}
    for fila in query("SELECT id, plan FROM tenants"):
        planes[str(fila["id"])] = fila.get("plan") or "basico"
    return planes


# ── Detección de problemas ────────────────────────────────────────────────────

def es_url_cloudinary(url):
    return bool(url) and CLOUDINARY_MARK in url


def url_rota(url, timeout=8):
    """True si el asset ya no existe en Cloudinary (HTTP != 200)."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status != 200
    except Exception:
        return True  # error de red o 404 → asumir rota


def analizar_producto(prod, galerias, planes, check_urls=False):
    """Devuelve la lista de problemas (strings) de un producto."""
    issues = []
    rows = [g for g in galerias if g["producto_id"] == prod["id"]]
    img = (prod.get("imagen") or "").strip()
    tiene_principal = bool(img) and img != "No hay foto" and es_url_cloudinary(img)
    plan = planes.get(str(prod["tenant_id"]), "basico")

    if not rows and not tiene_principal:
        return issues  # producto sin fotos: sin problemas

    # URLs duplicadas en la galería
    urls = [r["url"] for r in rows]
    duplicados = [u for u, c in Counter(urls).items() if c > 1 and es_url_cloudinary(u)]
    if duplicados:
        issues.append(f"URLs duplicadas en la galería: {len(duplicados)} (ej: {duplicados[0][:80]})")

    # Filas malas (no-Cloudinary / vacías)
    malas = [r for r in rows if not es_url_cloudinary(r["url"]) or r["url"] == "No hay foto"]
    if malas:
        issues.append(f"{len(malas)} fila(s) de la galería con URL inválida")

    # Principal fuera de la galería (desync — el bug raíz)
    if tiene_principal:
        fila_principal = next((r for r in rows if r["url"] == img), None)
        if fila_principal is None:
            issues.append("La foto principal NO está en la galería (desync pre-fix)")
        elif fila_principal["orden"] != 1:
            issues.append(f"La foto principal está en la galería con orden {fila_principal['orden']} (debería ser 1)")

    # Órdenes no secuenciales (huecos / desorden)
    if rows:
        ordenes = sorted(r["orden"] for r in rows)
        if ordenes != list(range(1, len(ordenes) + 1)):
            issues.append(f"Órdenes no secuenciales: {ordenes}")

    # URLs rotas (opcional, lento)
    if check_urls:
        rotas = [r["url"] for r in rows if es_url_cloudinary(r["url"]) and url_rota(r["url"])]
        if tiene_principal and url_rota(img) and img not in [r["url"] for r in rows]:
            rotas.append(img)
        if rotas:
            issues.append(f"{len(rotas)} URL(s) ROTA(S) en Cloudinary (foto negra/rota) — ej: {rotas[0][:80]}")

    if rows and plan != "plus":
        issues.append(f"Tiene galería ({len(rows)} foto(s)) pero el plan es '{plan}'")

    return issues


# ── Reparación estructural (BD) ───────────────────────────────────────────────

def reparar_producto(prod, galerias, planes, aplicar=False, solo_reporte=False):
    """
    Reordena/limpia la estructura de la galería en BD.
    Misma lógica que _sincronizar_principal_galeria() del backend:
      - Dedupe de URLs (se queda la de menor orden).
      - Elimina filas inválidas.
      - La principal se refleja en orden 1 (swap si ya está en otra posición).
      - Renumera los órdenes 1..N.
    Devuelve una lista de acciones (strings) para mostrar.
    """
    if solo_reporte:
        return []
    acciones = []
    rows = [g for g in galerias if g["producto_id"] == prod["id"]]
    img = (prod.get("imagen") or "").strip()
    tiene_principal = bool(img) and img != "No hay foto" and es_url_cloudinary(img)
    plan = planes.get(str(prod["tenant_id"]), "basico")
    nombre = prod["producto"]

    # 1. Filas inválidas → eliminar
    for r in rows:
        if not es_url_cloudinary(r["url"]) or r["url"] == "No hay foto":
            acciones.append(f"  · eliminar fila id={r['id']} con URL inválida ({str(r['url'])[:40]})")
            if aplicar:
                execute("DELETE FROM producto_imagenes WHERE id=%s", (r["id"],))
    rows = [r for r in rows if es_url_cloudinary(r["url"]) and r["url"] != "No hay foto"]

    # 2. Dedupe por URL (conservar la de menor orden)
    mejores = {}
    for r in sorted(rows, key=lambda x: (x["orden"], x["id"])):
        if r["url"] not in mejores:
            mejores[r["url"]] = r
    for r in rows:
        if mejores[r["url"]]["id"] != r["id"]:
            acciones.append(f"  · eliminar fila id={r['id']} (duplicada de {r['url'][:60]}…)")
            if aplicar:
                execute("DELETE FROM producto_imagenes WHERE id=%s", (r["id"],))
    rows = list(mejores.values())

    # 3. Sincronizar la principal con la galería (solo plan plus)
    if plan == "plus" and tiene_principal:
        fila1 = next((r for r in rows if r["orden"] == 1), None)
        if fila1 and fila1["url"] != img:
            # ¿La URL de la principal ya está en otra posición? → swap
            otra = next((r for r in rows if r["url"] == img and r["id"] != fila1["id"]), None)
            if otra:
                acciones.append(
                    f"  · swap: orden 1 pasa a la principal y la fila {otra['id']} toma la URL anterior"
                )
                if aplicar:
                    execute("UPDATE producto_imagenes SET url=%s WHERE id=%s", (img, fila1["id"]))
                    execute("UPDATE producto_imagenes SET url=%s WHERE id=%s", (fila1["url"], otra["id"]))
                # refrescar filas tras el swap
                for r in rows:
                    if r["id"] == fila1["id"]:
                        r["url"] = img
                    elif r["id"] == otra["id"]:
                        r["url"] = fila1["url"]
            else:
                acciones.append("  · actualizar fila de orden 1 con la URL de la principal")
                if aplicar:
                    execute("UPDATE producto_imagenes SET url=%s WHERE id=%s", (img, fila1["id"]))
                fila1["url"] = img
        elif not fila1:
            otra = next((r for r in rows if r["url"] == img), None)
            if otra:
                acciones.append(f"  · mover fila {otra['id']} (la principal) a orden 1")
                if aplicar:
                    execute("UPDATE producto_imagenes SET orden=1 WHERE id=%s", (otra["id"],))
                otra["orden"] = 1
            else:
                acciones.append("  · INSERTAR la principal en la galería (orden 1)")
                if aplicar:
                    execute(
                        "INSERT INTO producto_imagenes (producto_id, tenant_id, url, orden) "
                        "VALUES (%s, %s, %s, 1)",
                        (prod["id"], prod["tenant_id"], img),
                    )

    # 4. Renumerar órdenes 1..N (la principal primero si está en la galería)
    rows = [r for r in rows if es_url_cloudinary(r["url"]) and r["url"] != "No hay foto"]
    if tiene_principal:
        principal = next((r for r in rows if r["url"] == img), None)
        if principal:
            resto = sorted((r for r in rows if r["id"] != principal["id"]), key=lambda x: x["orden"])
            filas_orden = [principal] + resto
        else:
            filas_orden = sorted(rows, key=lambda x: x["orden"])
    else:
        filas_orden = sorted(rows, key=lambda x: x["orden"])
    for nuevo, r in enumerate(filas_orden, start=1):
        if r["orden"] != nuevo:
            acciones.append(f"  · fila id={r['id']}: orden {r['orden']} → {nuevo}")
            if aplicar:
                execute("UPDATE producto_imagenes SET orden=%s WHERE id=%s", (nuevo, r["id"]))

    return acciones


# ── Huérfanos en Cloudinary (solo lectura) ────────────────────────────────────

def _urls_referenciadas_en_bd() -> set:
    """URLs de Cloudinary referenciadas en cualquier tabla de la BD."""
    referenciadas = set()
    for fila in query("SELECT Imagen as url FROM productos WHERE Imagen LIKE '%res.cloudinary.com%'"):
        referenciadas.add(fila["url"])
    for fila in query("SELECT url FROM producto_imagenes WHERE url LIKE '%res.cloudinary.com%'"):
        referenciadas.add(fila["url"])
    for fila in query("SELECT foto as url FROM producto_variaciones WHERE foto LIKE '%res.cloudinary.com%'"):
        referenciadas.add(fila["url"])
    for fila in query("SELECT logo as url FROM tenants WHERE logo LIKE '%res.cloudinary.com%'"):
        referenciadas.add(fila["url"])
    for fila in query(
        "SELECT banner_url as url FROM catalogo_config WHERE banner_url LIKE '%res.cloudinary.com%' "
        "UNION SELECT banner_url_movil FROM catalogo_config WHERE banner_url_movil LIKE '%res.cloudinary.com%'"
    ):
        referenciadas.add(fila["url"])
    return referenciadas


def _configurar_cloudinary() -> bool:
    """Configura el SDK de Cloudinary desde .env. True si quedó listo."""
    try:
        import cloudinary
        import cloudinary.api
        import os
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        )
        return True
    except Exception as e:
        print(f"⚠️  No se pudo configurar Cloudinary: {e}")
        return False


def _listar_assets_cloudinary(prefix: str = "productos/") -> list[dict]:
    """Lista TODOS los assets de Cloudinary bajo el prefijo (paginando)."""
    import cloudinary.api
    assets: list[dict] = []
    next_cursor = None
    while True:
        kwargs = {"type": "upload", "resource_type": "image", "prefix": prefix, "max_results": 500}
        if next_cursor:
            kwargs["next_cursor"] = next_cursor
        res = cloudinary.api.resources(**kwargs)
        assets.extend(res.get("resources", []))
        next_cursor = res.get("next_cursor")
        if not next_cursor:
            break
    return assets


def estimar_huerfanos_cloudinary():
    """Lista los assets de Cloudinary y estima cuáles no están referenciados."""
    if not _configurar_cloudinary():
        return None

    referenciadas = _urls_referenciadas_en_bd()

    huerfanas = []
    try:
        assets = _listar_assets_cloudinary()
        for asset in assets:
            url = asset.get("secure_url") or asset.get("url")
            if url and url not in referenciadas:
                huerfanas.append(url)
    except Exception as e:
        print(f"⚠️  Error listando assets de Cloudinary: {e}")
        return None
    return huerfanas


# ── Restauración de principales pisadas (bug del slot 1) ─────────────────────

import re as _re

# Las fotos de la galería se suben con public_id "{producto}_{hex}_img{1-5}".
_SUFIX_GALERIA = _re.compile(r"_img[1-5]$")
# Toda foto lleva un sufijo aleatorio "_{hex8}" tras el nombre del producto.
SUFIX_ALEATORIO = _re.compile(r"_[0-9a-f]{8}")


def _nombre_desde_public_id(public_id: str) -> str:
    """
    'productos/Taco de bistec_ab12cd34'      → 'Taco de bistec'
    'productos/Taco de bistec_ab12cd34_img2' → 'Taco de bistec' (foto de galería)
    """
    base = public_id.split("/")[-1]
    base = _SUFIX_GALERIA.sub("", base)
    base = SUFIX_ALEATORIO.sub("", base)
    return base.strip()


def restaurar_principales(aplicar: bool = False):
    """
    Recupera fotos principales PISADAS por el bug del slot 1 (la primera
    imagen extra sobrescribía productos.imagen).

    Cruza los assets huérfanos de Cloudinary (carpeta 'productos', SIN sufijo
    _imgN → eran principales) contra los productos por nombre y propone
    restaurar productos.imagen. Dry-run por defecto: con --apply escribe en la
    BD (nunca toca Cloudinary).

    Devuelve el número de restauraciones propuestas/aplicadas.
    """
    if not _configurar_cloudinary():
        return 0

    try:
        assets = _listar_assets_cloudinary("productos/")
    except Exception as e:
        print(f"⚠️  Error listando assets de Cloudinary: {e}")
        return 0

    referenciadas = _urls_referenciadas_en_bd()

    # Huérfanos SIN sufijo _imgN: eran fotos principales.
    huerfanos_principal = []
    for asset in assets:
        public_id = asset.get("public_id") or ""
        url = asset.get("secure_url") or asset.get("url")
        if not url or url in referenciadas:
            continue
        if _SUFIX_GALERIA.search(public_id):
            continue
        huerfanos_principal.append({
            "url": url,
            "public_id": public_id,
            "creado": asset.get("created_at") or "",
        })
    huerfanos_principal.sort(key=lambda a: a["creado"])  # más reciente al final

    productos = cargar_productos()
    por_nombre: dict[str, dict] = {}
    for p in productos:
        por_nombre.setdefault((p["producto"] or "").strip(), p)

    # Agrupar propuestas por producto
    propuestas: dict[str, list] = {}
    for a in huerfanos_principal:
        prod = por_nombre.get(_nombre_desde_public_id(a["public_id"]))
        if not prod:
            continue
        img_actual = (prod.get("imagen") or "").strip()
        if img_actual == a["url"]:
            continue  # ya restaurada
        key = f"{prod['tenant_id']}|{prod['producto']}"
        propuestas.setdefault(key, []).append({**a, "prod": prod, "img_actual": img_actual})

    if not propuestas:
        print("✅ No se encontraron fotos principales pisadas por restaurar.")
        return 0

    print(f"🔎 {sum(len(v) for v in propuestas.values())} candidata(s) en "
          f"{len(propuestas)} producto(s):\\n")
    total = 0
    aplicadas = 0
    for key, cands in sorted(propuestas.items()):
        prod = cands[0]["prod"]
        img_actual = cands[0]["img_actual"]
        print(f"⚠️  {prod['producto']} (tenant {str(prod['tenant_id'])[:8]}…):")
        if img_actual and img_actual != "No hay foto":
            print(f"     imagen actual (pisada por la 1ª secundaria): {img_actual[:100]}")
        else:
            print("     imagen actual: 'No hay foto'")
        # Con --apply se elige la MÁS RECIENTE (los re-intentos de alta dejan
        # varios assets; la última es la que el usuario llegó a guardar).
        elegida = cands[-1]
        for c in cands:
            marca = "  ← elegida" if aplicar and c["url"] == elegida["url"] else ""
            print(f"     · {c['creado'][:19]}  {c['url'][:100]}{marca}")
        total += len(cands)
        if aplicar:
            try:
                execute(
                    "UPDATE productos SET Imagen=%s WHERE id=%s AND tenant_id=%s",
                    (elegida["url"], prod["id"], prod["tenant_id"]),
                )
                _sincronizar_galeria_con_principal(prod, elegida["url"], img_actual)
                aplicadas += 1
                print("     ✅ productos.imagen restaurada y galería sincronizada.")
            except Exception as e:
                print(f"     ❌ Error aplicando: {e}")
        else:
            print("     (dry-run: repite con --apply para restaurar)")
        print()

    print(f"{'✅' if aplicar else '⚠️'} {aplicadas if aplicar else total} "
          f"restauración(es) {'aplicadas' if aplicar else 'pendientes'}.")
    return total


def _sincronizar_galeria_con_principal(prod: dict, url_principal: str, url_pisada: str) -> None:
    """
    Tras restaurar productos.imagen, deja la galería coherente:
      - La foto restaurada pasa a la orden 1.
      - La foto que la pisaba (la 1ª secundaria) NO se pierde: se mueve al
        primer slot libre 2-5. Si la galería está llena (1..5), la orden 1
        se reemplaza por la restaurada (espejo de la lógica del backend).
    """
    filas = query(
        "SELECT id, url, orden FROM producto_imagenes "
        "WHERE producto_id=%s AND tenant_id=%s ORDER BY orden ASC",
        (prod["id"], prod["tenant_id"]),
    )
    fila1 = next((r for r in filas if r["orden"] == 1), None)
    if fila1 and fila1["url"] == url_principal:
        return  # ya coherente
    ocupados = {r["orden"] for r in filas}
    libre = next((i for i in range(2, 6) if i not in ocupados), None)
    if fila1 and libre is not None:
        execute("UPDATE producto_imagenes SET orden=%s WHERE id=%s", (libre, fila1["id"]))
        execute(
            "INSERT INTO producto_imagenes (producto_id, tenant_id, url, orden) "
            "VALUES (%s, %s, %s, 1)",
            (prod["id"], prod["tenant_id"], url_principal),
        )
    elif fila1:
        execute("UPDATE producto_imagenes SET url=%s WHERE id=%s", (url_principal, fila1["id"]))
    else:
        execute(
            "INSERT INTO producto_imagenes (producto_id, tenant_id, url, orden) "
            "VALUES (%s, %s, %s, 1)",
            (prod["id"], prod["tenant_id"], url_principal),
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Auditoría/reparación de imágenes de productos")
    parser.add_argument(
        "modo", choices=["report", "fix", "restaurar"],
        help="report (solo lectura) | fix (reparar) | restaurar (recuperar principales pisadas)",
    )
    parser.add_argument("--producto", help="Limitar a un producto por nombre")
    parser.add_argument("--check-urls", action="store_true", help="HEAD a cada URL de Cloudinary (lento)")
    parser.add_argument("--cloudinary", action="store_true", help="Estimar huérfanos de Cloudinary (solo lectura)")
    parser.add_argument("--apply", action="store_true", help="En fix/restaurar: aplicar los cambios (sin esto es dry-run)")
    args = parser.parse_args()

    if args.modo == "restaurar":
        restaurar_principales(aplicar=args.apply)
        return

    productos = cargar_productos()
    galerias = cargar_galerias()
    planes = cargar_planes()

    if args.producto:
        productos = [p for p in productos if p["producto"] == args.producto]
        if not productos:
            print(f"❌ No se encontró el producto '{args.producto}'")
            sys.exit(1)

    if args.modo == "report":
        print(f"Analizando {len(productos)} producto(s)…\n")
        con_problemas = 0
        for prod in productos:
            issues = analizar_producto(prod, galerias, planes, check_urls=args.check_urls)
            if issues:
                con_problemas += 1
                print(f"⚠️  {prod['producto']} (tenant {str(prod['tenant_id'])[:8]}…):")
                for i in issues:
                    print(f"     - {i}")
        print(f"\n✅ {len(productos) - con_problemas} producto(s) sin problemas, {con_problemas} con problemas.")

        if args.cloudinary:
            print("\n── Huérfanos en Cloudinary (solo lectura) ──")
            huerfanas = estimar_huerfanos_cloudinary()
            if huerfanas is not None:
                print(f"{len(huerfanas)} asset(s) sin referencia en BD.")
                for u in huerfanas[:10]:
                    print(f"   · {u[:110]}")
                if len(huerfanas) > 10:
                    print(f"   … y {len(huerfanas) - 10} más.")
        return

    # fix
    print("Modo FIX" + ("" if args.apply else " (DRY-RUN — pasa --apply para aplicar)"))
    print(f"Reparando {len(productos)} producto(s)…\n")
    total_acciones = 0
    for prod in productos:
        acciones = reparar_producto(prod, galerias, planes, aplicar=args.apply)
        if acciones:
            print(f"🛠️  {prod['producto']}:")
            for a in acciones:
                print(f"   {a}")
            total_acciones += len(acciones)
    if total_acciones == 0:
        print("✅ Nada que reparar.")
    elif not args.apply:
        print(f"\n⚠️  {total_acciones} acción(es) pendientes. Vuelve a ejecutar con --apply para aplicarlas.")
    else:
        print(f"\n✅ {total_acciones} acción(es) aplicadas.")


if __name__ == "__main__":
    main()
