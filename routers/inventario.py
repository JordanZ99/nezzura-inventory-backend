# ==============================================================================
# backend/routers/inventario.py
# Endpoints de inventario, lotes y productos.
# ==============================================================================

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from typing import Optional
from dependencies import get_tenant_id

from database.imagenes import (
    _ErrorGaleria,
    borrar_imagen_url,
    leer_imagen_actual_producto,
    sincronizar_imagenes_tras_editar_producto,
    listar_imagenes_producto as listar_imagenes_dominio,
    subir_imagen_extra as subir_imagen_extra_negocio,
    eliminar_imagen_extra as eliminar_imagen_extra_negocio,
    reordenar_imagenes as reordenar_imagenes_negocio,
    subir_foto as subir_foto_negocio,
    subir_foto_variacion as subir_foto_variacion_negocio,
)
from database.lotes import (
    get_lotes, get_detalle_lotes, agregar_lote, actualizar_lote, eliminar_lote,
)
from database.productos import (
    get_productos_meta, get_inventario_consolidado, crear_producto_completo, actualizar_producto,
)
from database.categorias import (
    eliminar_categoria_de_productos, listar_categorias, renombrar_categoria,
    crear_categoria, toggle_visibilidad_categoria, reordenar_categorias,
)
from database.variaciones import (
    listar_variaciones_producto, crear_variacion, actualizar_variacion, eliminar_variacion,
    eliminar_variacion_verificada,
)
from database.recetas import (
    listar_recetas_producto, agregar_material_receta, actualizar_material_receta,
    eliminar_material_receta,
)
from database.tenants import leer_config_perfil, actualizar_config_perfil
from database.posts import guardar_post_override as guardar_post_override_dominio
from database.movimientos import get_movimientos
from database import conteos
from schemas.inventario import (
    VariacionAlta,
    RecetaAlta,
    NuevoProducto,
    Restock,
    ActualizarProducto,
    ActualizarLote,
    CrearCategoria,
    RenombrarCategoria,
    BorrarImagen,
    NuevaVariacion,
    ActualizarVariacion,
    NuevoMaterialReceta,
    ActualizarMaterialReceta,
    ActualizarPerfil,
    ActualizarPostOverride,
    ReordenarImagenes,
    ReordenarCategorias,
    GuardarCapturasConteo,
    CerrarConteo,
    AbrirConteo,
    RenglonManualConteo,
)

router = APIRouter(prefix="/inventario", tags=["Inventario"])

# --- Endpoints ---
# El SQL y las validaciones del perfil viven en database/tenants.py; aquí
# solo se rutea HTTP y se mapea su dict de dominio ({ok, status, mensaje})
# a HTTPException (mismo patrón que database/imagenes.py).


@router.get("/me")
def obtener_mi_perfil(tenant_id: str = Depends(get_tenant_id)):
    """
    Este endpoint está protegido.
    Lee el JWT del Header, lo decodifica y devuelve el ID del usuario
    junto con su configuración de perfil (modo de precio sugerido del POS
    y zona horaria del negocio).
    """
    return {"tenant_id": tenant_id, **leer_config_perfil(tenant_id)}


@router.patch("/me")
def actualizar_mi_perfil(data: ActualizarPerfil, tenant_id: str = Depends(get_tenant_id)):
    """
    Actualiza la configuración del perfil del tenant:
    - modo_precio_sugerido: cómo el POS sugiere el precio.
    - zona_horaria: nombre IANA del negocio; define el día contable de
      ventas, gastos y cortes de caja (migración 031).
    - clientes_* / puntos_*: cartera de clientes y sistema de puntos
      (migraciones 038/039, doc sistemaPuntos.md).
    """
    resultado = actualizar_config_perfil(data.model_dump(), tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(
            status_code=resultado.get("status", 422),
            detail=resultado.get("mensaje", "Configuración inválida"),
        )
    return resultado


@router.get("/")
def listar_inventario(tenant_id: str = Depends(get_tenant_id)):
    """Vista consolidada: un producto = una fila con stock total."""
    resultado = get_inventario_consolidado(tenant_id)
    return resultado


@router.get("/lotes")
def listar_lotes(tenant_id: str = Depends(get_tenant_id)):
    """Todos los lotes activos con detalle de costo y stock por lote."""
    resultado = get_lotes(tenant_id)
    return resultado


@router.get("/movimientos")
def listar_movimientos(
    tenant_id: str = Depends(get_tenant_id),
    limit: int = 50,
    offset: int = 0,
    tipo: Optional[str] = None,
    producto: Optional[str] = None,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
):
    """
    Historial de cambios de inventario (ledger append-only, migración 040).

    Cada entrada/salida/ajuste de stock deja un renglón inmutable: venta,
    restock, ajuste manual, edición de venta, anulación o baja de lote, con
    el stock resultante del lote al momento del movimiento.

    Filtros opcionales: tipo (entrada|salida|ajuste), texto de producto y
    rango de fechas (YYYY-MM-DD, en zona horaria del negocio).
    """
    return get_movimientos(tenant_id, limit=limit, offset=offset, tipo=tipo, producto=producto, desde=desde, hasta=hasta)


@router.get("/lotes/{producto}")
def lotes_por_producto(producto: str, tenant_id: str = Depends(get_tenant_id)):
    """Lotes activos de un producto específico."""
    return get_detalle_lotes(producto, tenant_id)


@router.get("/productos")
def listar_productos(tenant_id: str = Depends(get_tenant_id)):
    """Metadatos de todos los productos."""
    resultado = get_productos_meta(tenant_id)
    return resultado


@router.post("/")
def crear_producto(data: NuevoProducto, tenant_id: str = Depends(get_tenant_id)):
    """
    Registra un producto nuevo con su primer lote, VARIACIONES y RECETA
    (si vienen) en UNA sola transacción atómica.

    - visible_en_catalogo: solo aplica si el producto es nuevo (si ya existía,
      se conserva la visibilidad actual).
    - variaciones: [{nombre, precio}] — se crean junto al producto.
    - recetas: [{material, cantidad}] — solo para compuestos; los materiales
      deben ser productos de stock ya existentes.
    """
    resultado = crear_producto_completo(
        data.producto, data.descripcion,
        data.costo, data.precio_venta,
        data.stock, data.imagen, data.categoria,
        tenant_id,
        codigo_interno=data.codigo_interno,
        codigo_barras=data.codigo_barras,
        ubicacion=data.ubicacion,
        etiqueta=data.etiqueta,
        sufijo_precio=data.sufijo_precio,
        fraccionable=data.fraccionable,
        tamano_cm=data.tamano_cm,
        tipo_producto=data.tipo_producto,
        costo_servicio=data.costo_servicio,
        precio_servicio=data.precio_servicio,
        visible_en_catalogo=data.visible_en_catalogo,
        variaciones=[v.model_dump() for v in (data.variaciones or [])],
        recetas=[r.model_dump() for r in (data.recetas or [])],
    )
    if not resultado.get("ok"):
        raise HTTPException(
            status_code=422 if resultado.get("tipo") == "validacion" else 500,
            detail=resultado.get("mensaje", "Error al crear el producto"),
        )
    return resultado


@router.post("/restock")
def restockear(data: Restock, tenant_id: str = Depends(get_tenant_id)):
    """Añade stock a un producto existente (nuevo lote o suma al existente).
    Si el producto maneja stock por variación, `variacion` es obligatoria."""
    resultado = agregar_lote(
        producto=data.producto,
        descripcion="",
        costo=data.costo,
        precio_venta=data.precio_venta,
        stock=data.stock,
        tenant_id=tenant_id,
        etiqueta=data.etiqueta,
        variacion=data.variacion or ""
    )
    if not resultado.get("ok", True):
        raise HTTPException(
            status_code=422 if resultado.get("tipo") == "validacion" else 500,
            detail=resultado.get("mensaje", "Error al restockear"),
        )
    return resultado


@router.post("/foto/{producto}")
async def subir_foto(producto: str, foto: UploadFile = File(...), tenant_id: str = Depends(get_tenant_id)):
    """Sube la foto de un producto a Cloudinary y devuelve la URL segura."""
    contents = await foto.read()
    resultado = subir_foto_negocio(producto, contents, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=resultado["status"], detail=resultado["mensaje"])
    return {"ruta": resultado["ruta"]}


@router.post("/borrar_imagen")
def borrar_imagen(data: BorrarImagen, tenant_id: str = Depends(get_tenant_id)):
    """
    Borra una imagen de Cloudinary tras ser reemplazada o eliminada por el
    tenant (logo, banners de escritorio/móvil). Best-effort: si falla, no
    rompe el flujo (la app ya no depende de la foto vieja).
    """
    return borrar_imagen_url(data.url, tenant_id)


@router.patch("/{producto}")
def editar_producto(producto: str, data: ActualizarProducto, tenant_id: str = Depends(get_tenant_id)):
    """
    Actualiza metadatos (descripción, imagen, estado) de un producto.

    La foto anterior se lee ANTES del update y la sincronización de galería +
    limpieza de la foto vieja ocurren DESPUÉS, en la capa de imágenes
    (database/imagenes.py): nunca se destruye un asset que sigue en uso
    (evita la clase de fotos rotas del catálogo público).
    """
    old_url = leer_imagen_actual_producto(producto, tenant_id)

    resultado = actualizar_producto(
        producto, data.descripcion, data.imagen, data.estado, data.categoria,
        data.costo, data.precio_venta, tenant_id,
        nuevo_producto=data.producto,
        codigo_interno=data.codigo_interno,
        codigo_barras=data.codigo_barras,
        ubicacion=data.ubicacion,
        visible_en_catalogo=data.visible_en_catalogo,
        sufijo_precio=data.sufijo_precio,
        fraccionable=data.fraccionable,
        tamano_cm=data.tamano_cm,
        tipo_producto=data.tipo_producto,
        costo_servicio=data.costo_servicio,
        precio_servicio=data.precio_servicio
    )

    # ── Sincronizar la foto principal con la galería (plan Plus) y borrar la
    # foto anterior SOLO si cambió y quedó huérfana ── (todo en database/imagenes.py)
    sincronizar_imagenes_tras_editar_producto(
        producto, resultado, (data.imagen or "").strip(), old_url, tenant_id
    )

    return resultado


@router.patch("/lote/{id_lote}")
def editar_lote(id_lote: str, data: ActualizarLote, tenant_id: str = Depends(get_tenant_id)):
    """Actualiza costo, precio de venta, stock y etiqueta de un lote específico."""
    return actualizar_lote(id_lote, data.costo, data.precio_venta, data.stock, tenant_id, etiqueta=data.etiqueta, variacion=data.variacion)


@router.delete("/lote/{id_lote}")
def borrar_lote(id_lote: str, tenant_id: str = Depends(get_tenant_id)):
    """Da de baja un lote. Si es el último activo, también desactiva el producto."""
    return eliminar_lote(id_lote, tenant_id)


@router.get("/categorias")
def obtener_categorias(tenant_id: str = Depends(get_tenant_id)):
    """Devuelve la lista de categorías del tenant con conteo de productos."""
    return listar_categorias(tenant_id)


@router.post("/categoria/crear")
def crear_categoria_endpoint(data: CrearCategoria, tenant_id: str = Depends(get_tenant_id)):
    """Crea una categoría nueva o devuelve la existente si ya existe."""
    return crear_categoria(data.nombre, tenant_id)


@router.patch("/categoria/{categoria}")
def editar_categoria(categoria: str, data: RenombrarCategoria, tenant_id: str = Depends(get_tenant_id)):
    """
    Renombra una categoría existente.
    Actualiza todas las relaciones automáticamente (el slug se recalcula).
    """
    return renombrar_categoria(categoria, data.nuevo_nombre, tenant_id)


@router.delete("/categoria/{categoria}")
def borrar_categoria(categoria: str, tenant_id: str = Depends(get_tenant_id)):
    """Elimina una categoría de todos los productos del tenant."""
    return eliminar_categoria_de_productos(categoria, tenant_id)


@router.patch("/categoria/{categoria}/visibilidad")
def alternar_visibilidad_catalogo(categoria: str, tenant_id: str = Depends(get_tenant_id)):
    """
    Alterna la visibilidad de una categoría en el catálogo público.
    Si estaba visible, se oculta; si estaba oculta, se muestra.
    """
    return toggle_visibilidad_categoria(categoria, tenant_id)


@router.patch("/categorias/reordenar")
def reordenar_categorias_endpoint(data: ReordenarCategorias, tenant_id: str = Depends(get_tenant_id)):
    """
    Guarda el orden manual de las categorías del catálogo tras un drag & drop
    desde Personalización > Catálogo: [{id, orden}] con orden 1..N según la
    lista visible (mismo patrón que mesas / imágenes de producto).
    """
    return reordenar_categorias(
        tenant_id, [{"id": fila.id, "orden": fila.orden} for fila in data.ordenes]
    )


# =============================================================================
# ── POSTS AUTOMÁTICOS (Fase 1): override de la tarjeta por producto ──────────
# Un producto sin override (post_override NULL) usa los defaults del negocio
# (tabla post_config). El override solo existe para la foto rebelde que lo pida.
# La validación y el SQL viven en database/posts.py.
# =============================================================================


@router.patch("/{producto}/post_override")
def guardar_post_override(producto: str, data: ActualizarPostOverride, tenant_id: str = Depends(get_tenant_id)):
    """
    Guarda (o quita) el override de la tarjeta de post de un producto.

    - post_override = {template?, color?, font?, posicion?, mostrar?} → se guarda
      tal cual (las claves ausentes se heredan de los defaults del negocio).
    - post_override = null → se quita el override: el producto vuelve a usar
      los defaults del negocio.

    Valida los valores contra las listas permitidas (422 si son inválidos).
    """
    resultado = guardar_post_override_dominio(producto, data.post_override, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(
            status_code=resultado.get("status", 422),
            detail=resultado.get("mensaje", "Override inválido"),
        )
    return resultado


# =============================================================================
# ── VARIACIONES DE PRODUCTO (Fase 2) ────────────────────────────────────────
# Una variación es una presentación con su PROPIO precio para un mismo producto
# (ej. hamburguesa Sencilla/Doble, remera S/M/L, corte Caballero/Dama).
# =============================================================================

@router.get("/variaciones/{producto}")
def obtener_variaciones(producto: str, tenant_id: str = Depends(get_tenant_id)):
    """Variaciones de un producto concreto (nombre + precio propio)."""
    return listar_variaciones_producto(producto, tenant_id)


@router.post("/variaciones")
def crear_variacion_endpoint(data: NuevaVariacion, tenant_id: str = Depends(get_tenant_id)):
    """Crea una variación nueva para un producto (nombre + precio propio + foto opcional).
    Si stock_inicial > 0, crea el lote de la variación (como en el ALTA)."""
    return crear_variacion(data.producto, data.nombre, data.precio, tenant_id, foto=data.foto, stock_inicial=data.stock_inicial, costo=data.costo)


@router.patch("/variaciones/{variacion_id}")
def editar_variacion_endpoint(variacion_id: int, data: ActualizarVariacion, tenant_id: str = Depends(get_tenant_id)):
    """Actualiza nombre, precio y/o foto de una variación."""
    return actualizar_variacion(variacion_id, data.nombre, data.precio, tenant_id, foto=data.foto)


@router.delete("/variaciones/{variacion_id}")
def borrar_variacion_endpoint(variacion_id: int, confirmar: bool = False, tenant_id: str = Depends(get_tenant_id)):
    """
    Elimina una variación del producto (borra también su foto de Cloudinary).

    Si la variación tiene STOCK en sus lotes y no se pasa confirmar=true,
    devuelve un aviso (requiere_confirmacion) para que el frontend muestre el
    modal "mover/desvincular o eliminar de todos modos". Con confirmar=true
    se elimina (sus lotes se borran en cascada: el stock desaparece a propósito).

    La verificación de stock y el SQL viven en database/variaciones.py
    (eliminar_variacion_verificada).
    """
    return eliminar_variacion_verificada(variacion_id, confirmar, tenant_id)


@router.post("/variaciones/{variacion_id}/foto")
async def subir_foto_variacion(
    variacion_id: int,
    foto: UploadFile = File(...),
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Sube (o reemplaza) la foto propia de una variación a Cloudinary.
    - Valida que la variación pertenezca al tenant.
    - Sube a la carpeta "variaciones" y borra la foto anterior de Cloudinary.
    - Devuelve {ok, url}.
    """
    contents = await foto.read()
    resultado = subir_foto_variacion_negocio(variacion_id, contents, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=resultado["status"], detail=resultado["mensaje"])
    return resultado


# =============================================================================
# ── RECETAS DE PRODUCTOS COMPUESTOS (Fase 3) ─────────────────────────────────
# Un compuesto (ej. hamburguesa) no tiene stock propio: al venderlo se
# descuentan sus MATERIALES (receta/BOM). Referencias por ID (sobreviven
# renombrados). La cantidad es REAL (permite 0.5 pan, 150g, etc.).
# =============================================================================

@router.get("/recetas/{producto}")
def obtener_recetas(producto: str, tenant_id: str = Depends(get_tenant_id)):
    """Materiales de un compuesto (nombre + cantidad por unidad)."""
    return listar_recetas_producto(producto, tenant_id)


@router.post("/recetas")
def agregar_material_endpoint(data: NuevoMaterialReceta, tenant_id: str = Depends(get_tenant_id)):
    """Añade (o actualiza) un material a la receta de un compuesto.
    variacion_id opcional: None = receta base, si no = receta de esa variación."""
    return agregar_material_receta(data.producto, data.material, data.cantidad, tenant_id, variacion_id=data.variacion_id)


@router.patch("/recetas/{receta_id}")
def editar_material_endpoint(receta_id: int, data: ActualizarMaterialReceta, tenant_id: str = Depends(get_tenant_id)):
    """Actualiza la cantidad de un material en la receta."""
    return actualizar_material_receta(receta_id, data.cantidad, tenant_id)


@router.delete("/recetas/{receta_id}")
def borrar_material_endpoint(receta_id: int, tenant_id: str = Depends(get_tenant_id)):
    """Elimina un material de la receta."""
    return eliminar_material_receta(receta_id, tenant_id)


# =============================================================================
# ── GALERÍA DE IMÁGENES (Plan Plus) ──────────────────────────────────────────
# Los tenants con plan "plus" pueden subir hasta 5 imágenes adicionales
# por producto. La imagen principal sigue en productos.imagen (Cloudinary).
# Las imágenes extra viven en la tabla producto_imagenes.
# =============================================================================

@router.get("/imagenes/{producto}")
def listar_imagenes_producto(producto: str, tenant_id: str = Depends(get_tenant_id)):
    """
    Devuelve la galería completa de imágenes de un producto.
    Para Plan Plus: hasta 5 imágenes unificadas (la orden 1 es la principal).
    Para Plan básico: devuelve una lista vacía (el frontend usa ImagePicker simple).
    """
    try:
        return listar_imagenes_dominio(producto, tenant_id)
    except _ErrorGaleria as e:
        raise HTTPException(status_code=e.status, detail=e.mensaje)


@router.post("/imagenes/{producto}")
async def subir_imagen_extra(
    producto: str,
    foto: UploadFile = File(...),
    orden_target: Optional[int] = Form(None),
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Sube una imagen a la galería de un producto (Plan Plus).
    - Si orden_target es None: añade una imagen nueva (siguiente slot disponible).
    - Si orden_target es 1-5: reemplaza la imagen en esa posición (borra la anterior de Cloudinary).
    Máximo 5 imágenes por producto.
    La orden 1 se sincroniza automáticamente con productos.imagen.
    """
    contents = await foto.read()
    resultado = subir_imagen_extra_negocio(producto, contents, orden_target, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=resultado["status"], detail=resultado["mensaje"])
    return resultado


@router.delete("/imagenes/{imagen_id}")
def eliminar_imagen_extra(imagen_id: int, tenant_id: str = Depends(get_tenant_id)):
    """
    Elimina una imagen de la galería de un producto (Plan Plus).
    - Borra la imagen de Cloudinary.
    - Si era la orden 1 (principal), promueve la siguiente imagen a principal
      y sincroniza productos.imagen.
    - Reordena las imágenes restantes para que no quien huecos.
    """
    resultado = eliminar_imagen_extra_negocio(imagen_id, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=resultado["status"], detail=resultado["mensaje"])
    return resultado


@router.patch("/imagenes/{producto}/reordenar")
def reordenar_imagenes(producto: str, data: ReordenarImagenes, tenant_id: str = Depends(get_tenant_id)):
    """
    Reordena las imágenes de la galería de un producto (Plan Plus).
    Recibe un array de IDs en el nuevo orden (posición 0 = orden 1 = principal).
    El backend asigna orden 1, 2, 3... secuencialmente según el orden del array.
    Si el ID en orden 1 es diferente al anterior, actualiza productos.imagen
    automáticamente con la URL de la imagen principal nueva.
    """
    resultado = reordenar_imagenes_negocio(producto, data.ids, tenant_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=resultado["status"], detail=resultado["mensaje"])
    return resultado


# ==============================================================================
# Conteos de auditoría (conteo físico vs sistema — migraciones 041 y 056)
#
# Ciclo: POST /conteos (abre y congela snapshot) → PATCH /conteos/{id}/items
# (autosave acumulativo) y POST /conteos/{id}/items-manual (renglones de
# productos no registrados) → POST /conteos/{id}/cerrar (resuelve diferencias:
# merma / venta declarada / ingreso hallado / error del sistema).
# ==============================================================================


def _traducir_error_conteo(resultado: dict) -> None:
    """Convierte el {ok: False, tipo} del dominio en el HTTP status correcto."""
    tipo = resultado.get("tipo")
    if tipo == "no_encontrado":
        raise HTTPException(status_code=404, detail=resultado.get("mensaje", "Conteo no encontrado"))
    if tipo == "cerrado":
        raise HTTPException(status_code=409, detail=resultado.get("mensaje", "El conteo ya está cerrado"))
    if tipo == "validacion":
        raise HTTPException(status_code=422, detail=resultado.get("mensaje", "Solicitud inválida"))
    raise HTTPException(status_code=500, detail=resultado.get("mensaje", "Error al procesar el conteo"))


@router.post("/conteos")
def iniciar_conteo(data: AbrirConteo | None = None, tenant_id: str = Depends(get_tenant_id)):
    """
    Abre una sesión de conteo de auditoría congelando el snapshot del stock
    (un renglón por producto+variación, incluidos los de stock 0). Solo puede
    haber UN conteo abierto por tenant a la vez.

    Con `alcance` (categorías y/o producto_ids) el conteo es PARCIAL: solo
    esos productos entran al snapshot y el resto del inventario no se toca.
    """
    alcance = data.alcance.model_dump(exclude_none=True) if (data and data.alcance) else None
    resultado = conteos.abrir_conteo(tenant_id, alcance)
    if not resultado.get("ok"):
        _traducir_error_conteo(resultado)
    return resultado


# Debe declararse ANTES que /conteos/{conteo_id} para que "activo" no se
# capture como id de conteo.
@router.get("/conteos/activo")
def conteo_activo(tenant_id: str = Depends(get_tenant_id)):
    """Sesión de conteo abierta (con sus renglones de captura) o conteo: null."""
    return conteos.get_conteo_activo(tenant_id)


@router.get("/conteos")
def listar_conteos(tenant_id: str = Depends(get_tenant_id), limit: int = 30):
    """Historial de conteos cerrados (el más reciente primero) con su resumen."""
    return conteos.historial_conteos(tenant_id, limit=limit)


@router.get("/conteos/{conteo_id}")
def detalle_conteo(conteo_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Detalle de un conteo (abierto o cerrado) con el veredicto de cada renglón."""
    resultado = conteos.get_conteo(conteo_id, tenant_id)
    if not resultado.get("ok"):
        _traducir_error_conteo(resultado)
    return resultado


@router.patch("/conteos/{conteo_id}/items")
def guardar_capturas_conteo(conteo_id: str, data: GuardarCapturasConteo,
                            tenant_id: str = Depends(get_tenant_id)):
    """
    Autosave del conteo: fija el total contado (valor absoluto) de cada
    renglón (producto, variación). contado null borra la captura.
    Los renglones que no existen en el snapshot se devuelven en
    `no_encontrados` sin romper el guardado del resto.
    """
    resultado = conteos.guardar_capturas(conteo_id, tenant_id, [c.model_dump() for c in data.items])
    if not resultado.get("ok"):
        _traducir_error_conteo(resultado)
    return resultado


@router.post("/conteos/{conteo_id}/items-manual")
def agregar_renglon_manual(conteo_id: str, data: RenglonManualConteo,
                           tenant_id: str = Depends(get_tenant_id)):
    """
    Agrega al conteo abierto un renglón MANUAL: un producto que nunca se
    registró en el inventario y por eso no está en el snapshot. Con `inicial`
    (cuánto había al inicio), al cerrar la diferencia se declara como venta;
    lo contado se da de alta como producto nuevo + lote 'Conteo'.
    Idempotente: si el nombre ya tiene renglón en este conteo, actualiza su
    `inicial`.
    """
    resultado = conteos.agregar_renglon_manual(
        conteo_id, tenant_id, data.producto, data.inicial, data.contado)
    if not resultado.get("ok"):
        _traducir_error_conteo(resultado)
    return resultado


@router.post("/conteos/{conteo_id}/cerrar")
def cerrar_conteo(conteo_id: str, data: CerrarConteo, tenant_id: str = Depends(get_tenant_id)):
    """
    Cierra la sesión resolviendo cada diferencia CONTRA EL STOCK VIVO (las
    ventas hechas durante el conteo quedan absorbidas y no cuentan como merma).

    - Faltante: merma (default) | venta (crea una orden retroactiva real) |
      error_sistema.
    - Sobrante: error_sistema (default) | entrada_no_registrada (crea un lote
      con el costo indicado).
    - Renglones manuales (056): el producto no registrado se da de alta con
      lo contado y, si declaró 'inicial', la venta (inicial − contado) con
      precio REAL.
    - Renglones sin captura o que cuadran no se tocan.

    `fecha` ('YYYY-MM-DD') aplica a las ventas declaradas para que caigan al
    día contable correcto (ej. lo vendido en el bazar del sábado).
    Todo es atómico: si una resolución falla, no se aplica nada.
    """
    resultado = conteos.cerrar_conteo(
        conteo_id, tenant_id,
        [r.model_dump() for r in data.items],
        fecha=data.fecha,
    )
    if not resultado.get("ok"):
        _traducir_error_conteo(resultado)
    return resultado
