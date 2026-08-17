# ==============================================================================
# backend/database/categorias.py
# CRUD de categorías (tabla categorias + pivote producto_categorias).
# Extraído de lotes.py — Fase 1/2 del refactor por dominio.
# ==============================================================================

from database.conexion import query, execute
from database.helpers import _slugify


def listar_categorias(tenant_id: str) -> list[dict]:
    """
    Devuelve todas las categorías del tenant con el conteo de productos asociados.
    Útil para mostrar en la gestión de categorías del frontend.
    """
    return query("""
        SELECT
            c.id,
            c.nombre,
            c.slug,
            c.visible_en_catalogo,
            COUNT(pc.producto_id) AS total_productos
        FROM categorias c
        LEFT JOIN producto_categorias pc ON pc.categoria_id = c.id
        WHERE c.tenant_id = %s
        GROUP BY c.id, c.nombre, c.slug, c.visible_en_catalogo
        ORDER BY c.nombre ASC
    """, (tenant_id,))


def crear_categoria(nombre: str, tenant_id: str) -> dict:
    """
    Crea una categoría nueva para el tenant.
    Si ya existe, retorna la existente.
    """
    nombre = nombre.strip()
    slug = _slugify(nombre)

    result = query("""
        INSERT INTO categorias (tenant_id, nombre, slug)
        VALUES (%s, %s, %s)
        ON CONFLICT (tenant_id, nombre) DO NOTHING
        RETURNING id, nombre, slug
    """, (tenant_id, nombre, slug))

    if not result:
        # Si no se insertó es porque ya existe, la recuperamos
        existente = query(
            "SELECT id, nombre, slug FROM categorias WHERE nombre = %s AND tenant_id = %s",
            (nombre, tenant_id)
        )
        if existente:
            return {"ok": True, "categoria": existente[0], "mensaje": f"La categoría '{nombre}' ya existía"}
        return {"ok": False, "mensaje": "Error al crear la categoría"}

    return {"ok": True, "categoria": result[0], "mensaje": f"Categoría '{nombre}' creada"}


def renombrar_categoria(viejo_nombre: str, nuevo_nombre: str, tenant_id: str) -> dict:
    """
    Cambia el nombre de una categoría y actualiza su slug.
    Retorna la categoría actualizada o un error si no existe.
    """
    viejo_nombre = viejo_nombre.strip()
    nuevo_nombre = nuevo_nombre.strip()
    nuevo_slug = _slugify(nuevo_nombre)

    result = query("""
        UPDATE categorias
        SET nombre = %s, slug = %s
        WHERE nombre = %s AND tenant_id = %s
        RETURNING id, nombre, slug
    """, (nuevo_nombre, nuevo_slug, viejo_nombre, tenant_id))

    if not result:
        return {"ok": False, "mensaje": f"Categoría '{viejo_nombre}' no encontrada"}

    return {
        "ok": True,
        "categoria": result[0]
    }


def eliminar_categoria_de_productos(categoria: str, tenant_id: str) -> dict:
    """
    Elimina una categoría de todos los productos del tenant.
    Busca la categoría por nombre en la tabla 'categorias',
    elimina todas las relaciones en 'producto_categorias' y
    luego elimina la categoría misma.
    """
    categoria = categoria.strip()

    # Buscar la categoría por nombre y tenant
    result = query(
        "SELECT id FROM categorias WHERE nombre = %s AND tenant_id = %s",
        (categoria, tenant_id)
    )

    if not result:
        return {
            "ok": True,
            "categoria_eliminada": categoria,
            "productos_actualizados": 0
        }

    cat_id = result[0]["id"]

    # Contar cuántos productos tenían esta categoría
    affected = query(
        "SELECT COUNT(*) as count FROM producto_categorias WHERE categoria_id = %s",
        (cat_id,)
    )
    actualizados = affected[0]["count"] if affected else 0

    # Eliminar relaciones en la tabla pivote (CASCADE también lo haría,
    # pero hacemos DELETE explícito para claridad y control)
    execute(
        "DELETE FROM producto_categorias WHERE categoria_id = %s",
        (cat_id,)
    )

    # Eliminar la categoría de la tabla madre
    execute(
        "DELETE FROM categorias WHERE id = %s AND tenant_id = %s",
        (cat_id, tenant_id)
    )

    return {
        "ok": True,
        "categoria_eliminada": categoria,
        "productos_actualizados": actualizados
    }


def toggle_visibilidad_categoria(categoria: str, tenant_id: str) -> dict:
    """
    Alterna la visibilidad de una categoría en el catálogo público.
    Si estaba visible, la oculta y viceversa.
    """
    categoria = categoria.strip()

    # Obtener estado actual
    actual = query(
        "SELECT visible_en_catalogo FROM categorias WHERE nombre = %s AND tenant_id = %s",
        (categoria, tenant_id)
    )
    if not actual:
        return {"ok": False, "mensaje": f"Categoría '{categoria}' no encontrada"}

    nuevo_valor = not actual[0]["visible_en_catalogo"]
    execute(
        "UPDATE categorias SET visible_en_catalogo = %s WHERE nombre = %s AND tenant_id = %s",
        (nuevo_valor, categoria, tenant_id)
    )

    return {
        "ok": True,
        "categoria": categoria,
        "visible_en_catalogo": nuevo_valor,
        "mensaje": f"Categoría '{categoria}' {'visible' if nuevo_valor else 'oculta'} en el catálogo"
    }
