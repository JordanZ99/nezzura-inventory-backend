# ==============================================================================
# backend/database/recetas.py
# Recetas de productos compuestos (Fase 3)
# Un compuesto (ej. hamburguesa) no tiene stock propio: al venderlo se
# descuentan sus MATERIALES según la receta (BOM). Referencias por ID.
# Extraído de lotes.py — Fase 1/2 del refactor por dominio.
# ==============================================================================

from psycopg2.errors import UniqueViolation
from database.conexion import query
from database.helpers import _resolver_producto_id


def _adjuntar_recetas(filas: list[dict], tenant_id: str) -> None:
    """
    Adjunta las recetas de cada producto compuesto a una lista de filas que ya
    contienen la llave `producto` (nombre). Muta las filas in-place añadiendo
    `recetas` = [{id, material, cantidad, variacion_id}].

    variacion_id: None = receta base; si no, receta de ESA variación.
    Los que no son compuestos quedan con lista vacía.
    """
    if not filas:
        return
    recetas = query("""
        SELECT c.Producto AS compuesto, r.id, m.Producto AS material, r.cantidad,
               r.variacion_id, v.nombre AS variacion
        FROM producto_recetas r
        JOIN productos c ON c.id = r.producto_id
        JOIN productos m ON m.id = r.material_id
        LEFT JOIN producto_variaciones v ON v.id = r.variacion_id
        WHERE c.tenant_id = %s
        ORDER BY c.Producto ASC, v.nombre ASC NULLS FIRST, m.Producto ASC
    """, (tenant_id,))
    por_producto: dict = {}
    for r in recetas:
        por_producto.setdefault(r["compuesto"], []).append({
            "id": r["id"],
            "material": r["material"],
            "cantidad": float(r["cantidad"] or 0),
            "variacion_id": r.get("variacion_id"),
            "variacion": r.get("variacion"),
        })
    for f in filas:
        f["recetas"] = por_producto.get(f["producto"], [])


def _calcular_disponibilidad_compuestos(filas: list[dict], tenant_id: str) -> None:
    """
    Añade `disponibilidad_estimada` (int o None) a cada producto compuesto:
    cuántas unidades se pueden vender con el stock ACTUAL de sus materiales.

    Se calcula POR RECETA (base y cada variación por separado) y se toma el
    MÍNIMO entre ellas: es la cantidad que garantizas poder vender sin
    importar qué presentación pida el cliente.

    Ej: Hamburguesa = 1 pan + 150 carne + 2 queso. Con 4 panes, 600g carne
    y 10 quesos → quedan min(4/1, 600/150, 10/2) = min(4, 4, 5) = 4.
    None = el compuesto no tiene receta (no se puede estimar).
    """
    compuestos = [f for f in filas if f.get("tipo_producto") == "compuesto"]
    if not compuestos:
        return

    # Recetas por compuesto, SIN mezclar: la base es una receta, y cada
    # variación con receta propia es otra receta independiente.
    recetas_por_compuesto: dict[str, list[dict[str, float]]] = {}
    for f in compuestos:
        recetas_por_compuesto[f["producto"]] = []
        base = [r for r in (f.get("recetas") or []) if r.get("variacion_id") is None]
        if base:
            recetas_por_compuesto[f["producto"]].append(
                {r["material"]: float(r["cantidad"] or 0) for r in base}
            )
        por_variacion: dict[int, list[dict]] = {}
        for r in (f.get("recetas") or []):
            if r.get("variacion_id") is not None:
                por_variacion.setdefault(r["variacion_id"], []).append(r)
        for var_recetas in por_variacion.values():
            recetas_por_compuesto[f["producto"]].append(
                {r["material"]: float(r["cantidad"] or 0) for r in var_recetas}
            )

    nombres = sorted({
        m for recetas in recetas_por_compuesto.values() for receta in recetas for m in receta
    })
    if not nombres:
        for f in compuestos:
            f["disponibilidad_estimada"] = None
        return

    stocks = query(
        "SELECT Producto AS producto, SUM(Stock_Lote) AS stock FROM lotes "
        "WHERE tenant_id = %s AND Estado = 'Activo' AND Producto = ANY(%s) "
        "GROUP BY Producto",
        (tenant_id, nombres)
    )
    stock_map = {r["producto"]: float(r["stock"] or 0) for r in stocks}

    for f in compuestos:
        recetas = recetas_por_compuesto.get(f["producto"]) or []
        if not recetas:
            f["disponibilidad_estimada"] = None
            continue
        mejor: int | None = None
        for receta in recetas:
            disp_receta: int | None = None
            for mat, cant in receta.items():
                if cant <= 0:
                    continue
                n = int(stock_map.get(mat, 0) // cant)
                disp_receta = n if disp_receta is None else min(disp_receta, n)
            if disp_receta is not None:
                mejor = disp_receta if mejor is None else min(mejor, disp_receta)
        f["disponibilidad_estimada"] = max(mejor, 0) if mejor is not None else None


def listar_recetas_producto(producto: str, tenant_id: str) -> list[dict]:
    """Materiales de un compuesto (nombre + cantidad por unidad), con la
    variación a la que pertenece cada receta (None = receta base)."""
    pid = _resolver_producto_id(producto, tenant_id)
    if not pid:
        return []
    return query("""
        SELECT r.id, m.Producto AS material, r.cantidad, r.variacion_id,
               v.nombre AS variacion
        FROM producto_recetas r
        JOIN productos m ON m.id = r.material_id
        LEFT JOIN producto_variaciones v ON v.id = r.variacion_id
        WHERE r.producto_id = %s AND r.tenant_id = %s
        ORDER BY v.nombre ASC NULLS FIRST, m.Producto ASC
    """, (pid, tenant_id))


def agregar_material_receta(producto: str, material: str, cantidad: float, tenant_id: str, variacion_id: int | None = None) -> dict:
    """
    Añade (o actualiza la cantidad de) un material a la receta de un compuesto.

    variacion_id:
      None → receta BASE (se usa si la variación vendida no tiene receta propia)
      {id} → receta específica de esa variación (ej. Hamburguesa Doble usa 200g)

    Unicidad por índices parciales: (compuesto+material) para la base y
    (compuesto+variación+material) para cada variación.
    """
    producto = (producto or "").strip()
    material = (material or "").strip()
    cantidad = float(cantidad or 0)
    if not producto or not material:
        return {"ok": False, "mensaje": "El compuesto y el material son obligatorios"}
    if cantidad <= 0:
        return {"ok": False, "mensaje": "La cantidad debe ser mayor a 0"}
    pid = _resolver_producto_id(producto, tenant_id)
    mid = _resolver_producto_id(material, tenant_id)
    if not pid:
        return {"ok": False, "mensaje": "Compuesto no encontrado"}
    if not mid:
        return {"ok": False, "mensaje": "Material no encontrado"}
    if pid == mid:
        return {"ok": False, "mensaje": "Un producto no puede ser material de sí mismo"}
    if variacion_id is not None:
        # Validar que la variación existe y pertenece a este compuesto + tenant
        var = query(
            "SELECT 1 FROM producto_variaciones WHERE id = %s AND producto_id = %s AND tenant_id = %s",
            (variacion_id, pid, tenant_id)
        )
        if not var:
            return {"ok": False, "mensaje": "La variación no pertenece a este compuesto"}

    # ¿Ya existe este material para (compuesto, variación)? → actualizar cantidad
    existente = query("""
        SELECT id FROM producto_recetas
        WHERE producto_id = %s AND material_id = %s AND tenant_id = %s
          AND variacion_id IS NOT DISTINCT FROM %s
        LIMIT 1
    """, (pid, mid, tenant_id, variacion_id))
    if existente:
        result = query("""
            UPDATE producto_recetas SET cantidad = %s
            WHERE id = %s AND tenant_id = %s
            RETURNING id, cantidad
        """, (cantidad, existente[0]["id"], tenant_id))
    else:
        try:
            result = query("""
                INSERT INTO producto_recetas (producto_id, material_id, cantidad, tenant_id, variacion_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, cantidad
            """, (pid, mid, cantidad, tenant_id, variacion_id))
        except UniqueViolation:
            # Carrera (TOCTOU): otro request insertó el mismo material entre el
            # SELECT y el INSERT → convertir en UPDATE (upsert atómico)
            result = query("""
                UPDATE producto_recetas SET cantidad = %s
                WHERE producto_id = %s AND material_id = %s AND tenant_id = %s
                  AND variacion_id IS NOT DISTINCT FROM %s
                RETURNING id, cantidad
            """, (cantidad, pid, mid, tenant_id, variacion_id))
    if not result:
        return {"ok": False, "mensaje": "Error al guardar el material"}
    return {"ok": True, "material": material, "cantidad": float(result[0]["cantidad"] or 0)}


def actualizar_material_receta(receta_id: int, cantidad: float, tenant_id: str) -> dict:
    """Actualiza la cantidad de un material en la receta (validando tenant)."""
    cantidad = float(cantidad or 0)
    if cantidad <= 0:
        return {"ok": False, "mensaje": "La cantidad debe ser mayor a 0"}
    result = query("""
        UPDATE producto_recetas
        SET cantidad = %s
        WHERE id = %s AND tenant_id = %s
        RETURNING id, cantidad
    """, (cantidad, receta_id, tenant_id))
    if not result:
        return {"ok": False, "mensaje": "Material no encontrado en la receta"}
    return {"ok": True, "id": receta_id, "cantidad": float(result[0]["cantidad"] or 0)}


def eliminar_material_receta(receta_id: int, tenant_id: str) -> dict:
    """Elimina un material de la receta (validando tenant)."""
    result = query("""
        DELETE FROM producto_recetas
        WHERE id = %s AND tenant_id = %s
        RETURNING id
    """, (receta_id, tenant_id))
    if not result:
        return {"ok": False, "mensaje": "Material no encontrado en la receta"}
    return {"ok": True, "id": receta_id}
