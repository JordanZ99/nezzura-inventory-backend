# ==============================================================================
# backend/routers/inventario.py
# Endpoints de inventario, lotes y productos.
# ==============================================================================

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Depends
from pydantic import BaseModel
from typing import Optional
from dependencies import get_tenant_id
import os, uuid, re

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
        # Saltar prefijos de transformación y de versión (v123/)
        i = 0
        while i < len(segmentos) - 1:
            seg = segmentos[i]
            if re.match(r"^v\d+$", seg) or "," in seg or re.match(
                r"^(f_|q_|w_|c_|e_|t_|dpr_|g_|r_|o_|a_|x_|y_|fl_|l_|if_|b_)", seg
            ):
                i += 1
            else:
                break
        public_id = "/".join(segmentos[i:]).rsplit(".", 1)[0]
        return public_id or None
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
        cloudinary.uploader.destroy(public_id)
    except Exception as e:
        print(f"Error borrando imagen de Cloudinary ({public_id}): {e}")

from database.lotes import (
    get_lotes, get_productos_meta, get_inventario_consolidado,
    get_detalle_lotes, agregar_lote, crear_producto_completo, actualizar_producto,
    actualizar_lote,
    eliminar_categoria_de_productos, listar_categorias, renombrar_categoria,
    crear_categoria, eliminar_lote, toggle_visibilidad_categoria,
    listar_variaciones_producto, crear_variacion, actualizar_variacion,
    eliminar_variacion,
    listar_recetas_producto, agregar_material_receta, actualizar_material_receta,
    eliminar_material_receta
)
from database.conexion import query, execute
from pydantic import Field
from dependencies import validar_sesion

router = APIRouter(prefix="/inventario", tags=["Inventario"])

# --- Modelos Pydantic (validan los datos que llegan) ---

class VariacionAlta(BaseModel):
    """Variación a crear en el ALTA de un producto (nombre + precio propio).
    stock_inicial/costo: opcionales — si se da stock, se crea el lote de ESA
    variación y el producto pasa a manejar stock por variación (Fase 6)."""
    nombre: str = Field(..., description="Nombre de la variación (ej. 'Doble', 'S')")
    precio: float = Field(0, description="Precio propio de la variación")
    stock_inicial: Optional[float] = Field(None, description="Stock inicial propio de esta variación (crea su lote)")
    costo: Optional[float] = Field(None, description="Costo del lote inicial de esta variación (si se omite, usa el costo del producto)")

class RecetaAlta(BaseModel):
    """Material de la receta a crear en el ALTA de un compuesto."""
    material: str = Field(..., description="Nombre del material (producto de stock ya existente)")
    cantidad: float = Field(1, description="Cantidad por unidad (permite 0.5, 150, etc.)")

class NuevoProducto(BaseModel):
    producto       : str
    descripcion    : str  = ""
    costo          : float
    precio_venta   : float
    stock          : float
    imagen         : str  = "No hay foto"
    categoria      : list[str]  = ["General"]
    codigo_interno : Optional[str] = None
    codigo_barras  : Optional[str] = None
    ubicacion      : Optional[str] = None
    etiqueta       : Optional[str] = None  # Presentación del lote inicial (ej. "20cm", "Premium")
    sufijo_precio  : Optional[str] = None  # Sufijo del precio en el catálogo ("c/u", "por kilo", libre)
    fraccionable   : Optional[bool] = None # Si true, se puede vender por fracciones (0.5 kg, 1.5 lt...)
    tipo_producto  : str = "stock"        # 'stock' (normal) | 'servicio' | 'compuesto'
    costo_servicio : Optional[float] = None
    precio_servicio: Optional[float] = None
    visible_en_catalogo : Optional[bool] = None  # Solo aplica si el producto es NUEVO
    variaciones   : Optional[list[VariacionAlta]] = None  # Se crean en la misma transacción
    recetas       : Optional[list[RecetaAlta]] = None     # Solo para compuestos

class Restock(BaseModel):
    producto    : str
    costo       : float
    precio_venta: float
    stock       : float
    etiqueta    : Optional[str] = None  # Presentación del nuevo lote (ej. "20cm", "Premium")
    variacion   : Optional[str] = None  # Variación a la que llega el stock (obligatoria si el producto maneja stock por variación)

class ActualizarProducto(BaseModel):
    descripcion         : str
    imagen              : str
    estado              : str
    categoria           : list[str]
    costo               : Optional[float] = None
    precio_venta        : Optional[float] = None
    producto            : Optional[str] = None
    codigo_interno      : Optional[str] = None
    codigo_barras       : Optional[str] = None
    ubicacion           : Optional[str] = None
    visible_en_catalogo : Optional[bool] = None
    sufijo_precio       : Optional[str] = None  # Sufijo del precio en el catálogo ("c/u", "por kilo", libre)
    fraccionable        : Optional[bool] = None  # Si true, se puede vender por fracciones (0.5 kg, 1.5 lt...)
    tipo_producto       : Optional[str] = None  # 'stock' | 'servicio'
    costo_servicio      : Optional[float] = None
    precio_servicio     : Optional[float] = None
    stock_por_variacion : Optional[bool] = None  # Fase 6: si true, el stock vive en lotes por variación

class ActualizarLote(BaseModel):
    costo       : float
    precio_venta: float
    stock       : float
    etiqueta    : Optional[str] = None  # Presentación del lote (opcional)
    variacion   : Optional[str] = None  # Reasignar/desvincular la variación del lote ('' = base)

class CrearCategoria(BaseModel):
    nombre: str = Field(..., min_length=1, description="Nombre de la categoría a crear")

class RenombrarCategoria(BaseModel):
    nuevo_nombre: str = Field(..., min_length=1, description="Nuevo nombre para la categoría")

class BorrarImagen(BaseModel):
    url: str = Field(..., description="URL de Cloudinary a borrar (imagen reemplazada o eliminada)")

class NuevaVariacion(BaseModel):
    producto: str = Field(..., description="Nombre del producto al que pertenece la variación")
    nombre  : str = Field(..., description="Nombre de la variación (ej. 'Doble', 'S', 'Premium')")
    precio  : float = Field(0, description="Precio propio de la variación")
    foto    : str = Field("", description="URL de Cloudinary de la foto propia de la variación (opcional)")

class ActualizarVariacion(BaseModel):
    nombre : str = Field(..., description="Nuevo nombre de la variación")
    precio : float = Field(0, description="Nuevo precio de la variación")
    foto   : Optional[str] = Field(None, description="URL de la foto; None = conservar, '' = quitar")

class NuevoMaterialReceta(BaseModel):
    producto : str = Field(..., description="Nombre del producto compuesto (el que se vende)")
    material : str = Field(..., description="Nombre del material que consume (producto de stock)")
    cantidad : float = Field(1, description="Cantidad de material por unidad del compuesto (permite 0.5, 150, etc.)")
    variacion_id : Optional[int] = Field(None, description="Id de la variación a la que pertenece esta receta; None = receta base")

class ActualizarMaterialReceta(BaseModel):
    cantidad : float = Field(..., description="Nueva cantidad de material por unidad")

class ActualizarPerfil(BaseModel):
    modo_precio_sugerido: Optional[str] = None  # 'antiguo' | 'maximo' | 'reciente'


# --- Endpoints ---

@router.get("/me")
def obtener_mi_perfil(tenant_id: str = Depends(get_tenant_id)):
    """
    Este endpoint está protegido. 
    Lee el JWT del Header, lo decodifica y devuelve el ID del usuario
    junto con su configuración de perfil (modo de precio sugerido del POS).
    """
    modo = "antiguo"
    try:
        fila = query("SELECT modo_precio_sugerido FROM tenants WHERE id = %s", (tenant_id,))
        if fila and fila[0].get("modo_precio_sugerido"):
            modo = fila[0]["modo_precio_sugerido"]
    except Exception:
        pass
    return {"tenant_id": tenant_id, "modo_precio_sugerido": modo}


@router.patch("/me")
def actualizar_mi_perfil(data: ActualizarPerfil, tenant_id: str = Depends(get_tenant_id)):
    """
    Actualiza la configuración del perfil del tenant.
    Por ahora solo gestiona modo_precio_sugerido (cómo el POS sugiere el precio).
    """
    if data.modo_precio_sugerido is not None:
        modo = data.modo_precio_sugerido.strip().lower()
        if modo not in ("antiguo", "maximo", "reciente"):
            raise HTTPException(
                status_code=400,
                detail="modo_precio_sugerido debe ser 'antiguo', 'maximo' o 'reciente'",
            )
        execute("UPDATE tenants SET modo_precio_sugerido = %s WHERE id = %s", (modo, tenant_id))
        return {"ok": True, "modo_precio_sugerido": modo}
    # Sin cambios: devolver el valor actual persistido
    modo_actual = "antiguo"
    try:
        fila = query("SELECT modo_precio_sugerido FROM tenants WHERE id = %s", (tenant_id,))
        if fila and fila[0].get("modo_precio_sugerido"):
            modo_actual = fila[0]["modo_precio_sugerido"]
    except Exception:
        pass
    return {"ok": True, "modo_precio_sugerido": modo_actual}


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
    """
    Sube la foto de un producto a Cloudinary y devuelve la URL segura.

    Validaciones de seguridad:
    - El archivo no debe exceder 1 MB (1024 KB). Esto es una red de seguridad,
      ya que el frontend ya comprime las imágenes a ~80 KB como máximo.
    - Se pasa la opción quality=auto a Cloudinary para que optimice aún más
      el peso del archivo sin pérdida de calidad visible.
    """
    try:
        # Leemos el contenido del archivo subido
        contents = await foto.read()

        # Validar que el contenido no esté vacío
        if not contents or len(contents) == 0:
            raise HTTPException(status_code=400, detail="La imagen recibida está vacía (0 bytes)")

        # Obtenemos el tamaño en kilobytes para validación
        tamano_kb = len(contents) / 1024

        # Límite de seguridad: rechazamos archivos mayores a 1 MB.
        # El frontend comprime a ~80 KB, pero validamos en backend por si
        # alguien llama la API directamente sin pasar por el frontend.
        MAX_TAMANO_KB = 1024  # 1 MB
        if tamano_kb > MAX_TAMANO_KB:
            raise HTTPException(
                status_code=413,
                detail=f"La imagen es demasiado grande ({tamano_kb:.0f} KB). "
                       f"El máximo permitido es {MAX_TAMANO_KB} KB. "
                       "El frontend comprime automáticamente las imágenes "
                       "antes de subirlas para evitar este error."
            )

        # Subimos a Cloudinary con optimización automática de calidad.
        # folder="productos" agrupa todas las fotos en una carpeta en Cloudinary.
        # public_id usa el nombre del producto + un hex aleatorio para unicidad.
        # quality="auto:best" permite que Cloudinary optimice el peso del
        # archivo automáticamente sin pérdida de calidad visible.
        # fetch_format="auto" convierte automáticamente al formato más
        # eficiente (WebP en navegadores modernos, JPEG en el resto).
        resultado = cloudinary.uploader.upload(
            contents,
            folder="productos",
            public_id=f"{producto}_{uuid.uuid4().hex[:8]}",
            quality="auto:best",
            fetch_format="auto"
        )
        # Cloudinary devuelve una URL HTTPS persistente que se guarda en la DB.
        # Esta URL sobrevive a deploys de Render porque vive en el CDN de Cloudinary.
        return {"ruta": resultado.get("secure_url")}

    except HTTPException:
        # Re-lanzamos excepciones HTTP para que FastAPI las maneje correctamente
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error subiendo a Cloudinary: {str(e)}")


@router.post("/borrar_imagen")
def borrar_imagen(data: BorrarImagen, tenant_id: str = Depends(get_tenant_id)):
    """
    Borra una imagen de Cloudinary tras ser reemplazada o eliminada por el
    tenant (logo, banners de escritorio/móvil). Best-effort: si falla, no
    rompe el flujo (la app ya no depende de la foto vieja).

    Guardia de propiedad multitenant: solo borra si el public_id incluye el
    prefijo del tenant (el logo/banner se suben con _logo_{id}/_banner_{id})
    o si la URL está referenciada en las tablas del propio tenant.
    """
    url = (data.url or "").strip()
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


@router.patch("/{producto}")
def editar_producto(producto: str, data: ActualizarProducto, tenant_id: str = Depends(get_tenant_id)):
    """Actualiza metadatos (descripción, imagen, estado) de un producto."""
    # Intentar borrar la imagen anterior de Cloudinary si se reemplazó.
    # Esto evita acumular fotos huérfanas en el CDN (que cuestan almacenamiento).
    try:
        old_meta = query("SELECT Imagen as imagen FROM productos WHERE Producto=%s", (producto,))
        if old_meta:
            old_url = old_meta[0]["imagen"]
            # Solo intentamos borrar si la URL vieja era de Cloudinary y cambió.
            if old_url and old_url != data.imagen:
                borrar_imagen_cloudinary(old_url)
    except Exception as e:
        print(f"Error interno borrando foto antigua de Cloudinary: {e}")

    return actualizar_producto(
        producto, data.descripcion, data.imagen, data.estado, data.categoria,
        data.costo, data.precio_venta, tenant_id,
        nuevo_producto=data.producto,
        codigo_interno=data.codigo_interno,
        codigo_barras=data.codigo_barras,
        ubicacion=data.ubicacion,
        visible_en_catalogo=data.visible_en_catalogo,
        sufijo_precio=data.sufijo_precio,
        fraccionable=data.fraccionable,
        tipo_producto=data.tipo_producto,
        costo_servicio=data.costo_servicio,
        precio_servicio=data.precio_servicio,
        stock_por_variacion=data.stock_por_variacion
    )


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
    """Crea una variación nueva para un producto (nombre + precio propio + foto opcional)."""
    return crear_variacion(data.producto, data.nombre, data.precio, tenant_id, foto=data.foto)


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
    """
    # Si NO hay confirmación, primero advertir por el stock — aquí NO se borra
    # nada (la foto de Cloudinary se borra solo si realmente se elimina).
    if not confirmar:
        stock_info = query("""
            SELECT COALESCE(SUM(Stock_Lote), 0) AS unidades, COUNT(*) AS lotes
            FROM lotes
            WHERE variacion_id = %s AND tenant_id = %s AND Estado = 'Activo'
        """, (variacion_id, tenant_id))
        if stock_info and (float(stock_info[0]["unidades"] or 0) > 0 or int(stock_info[0]["lotes"] or 0) > 0):
            return {
                "ok": False,
                "requiere_confirmacion": True,
                "unidades": float(stock_info[0]["unidades"] or 0),
                "lotes": int(stock_info[0]["lotes"] or 0),
                "mensaje": "Esta variación tiene stock. Puedes moverlo a otra variación o desvincularlo antes de eliminar, o eliminar de todos modos (su stock desaparecerá del inventario).",
            }

    # Aquí sí se elimina: borrar la foto de Cloudinary y la variación
    try:
        fila = query("SELECT foto FROM producto_variaciones WHERE id=%s AND tenant_id=%s", (variacion_id, tenant_id))
        if fila and fila[0].get("foto"):
            borrar_imagen_cloudinary(fila[0]["foto"])
    except Exception as e:
        print(f"Error borrando foto de variación en Cloudinary: {e}")
    return eliminar_variacion(variacion_id, tenant_id)


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
    fila = query(
        "SELECT foto FROM producto_variaciones WHERE id=%s AND tenant_id=%s",
        (variacion_id, tenant_id)
    )
    if not fila:
        raise HTTPException(status_code=404, detail="Variación no encontrada")

    contents = await foto.read()
    if not contents or len(contents) == 0:
        raise HTTPException(status_code=400, detail="La imagen recibida está vacía (0 bytes)")
    tamano_kb = len(contents) / 1024
    if tamano_kb > 1024:
        raise HTTPException(
            status_code=413,
            detail=f"La imagen es demasiado grande ({tamano_kb:.0f} KB). Máximo 1024 KB."
        )

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
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return resultado[0]["id"]


@router.get("/imagenes/{producto}")
def listar_imagenes_producto(producto: str, tenant_id: str = Depends(get_tenant_id)):
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
    # 1. Validar plan plus
    plan = _get_tenant_plan(tenant_id)
    if plan != "plus":
        raise HTTPException(
            status_code=403,
            detail="La galería de imágenes es exclusiva del plan Plus."
        )

    # 2. Obtener el ID del producto
    producto_id = _get_producto_id(producto, tenant_id)

    # 3. Leer y validar el archivo
    contents = await foto.read()
    if not contents or len(contents) == 0:
        raise HTTPException(status_code=400, detail="La imagen recibida está vacía (0 bytes)")

    tamano_kb = len(contents) / 1024
    MAX_TAMANO_KB = 1024  # 1 MB
    if tamano_kb > MAX_TAMANO_KB:
        raise HTTPException(
            status_code=413,
            detail=f"La imagen es demasiado grande ({tamano_kb:.0f} KB). Máximo {MAX_TAMANO_KB} KB."
        )

    # 4. Determinar el orden y si es reemplazo o inserción nueva
    if orden_target is not None:
        # Reemplazo: validar que el orden esté en rango
        if orden_target < 1 or orden_target > 5:
            raise HTTPException(status_code=400, detail="orden_target debe estar entre 1 y 5")
        nueva_orden = orden_target

        # Borrar la imagen existente en ese orden de Cloudinary + DB
        existente = query(
            "SELECT id, url FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s AND orden = %s",
            (producto_id, tenant_id, nueva_orden)
        )
        if existente:
            old_url = existente[0]["url"]
            borrar_imagen_cloudinary(old_url)
            execute(
                "DELETE FROM producto_imagenes WHERE id = %s",
                (existente[0]["id"],)
            )
    else:
        # Inserción nueva: calcular siguiente slot disponible
        conteo = query(
            "SELECT COUNT(*) as total FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s",
            (producto_id, tenant_id)
        )
        total_actual = conteo[0]["total"] if conteo else 0
        if total_actual >= 5:
            raise HTTPException(
                status_code=403,
                detail="Este producto ya tiene 5 imágenes. Elimina una antes de subir otra."
            )

        ordenes = query(
            "SELECT orden FROM producto_imagenes "
            "WHERE producto_id = %s AND tenant_id = %s ORDER BY orden",
            (producto_id, tenant_id)
        )
        ordenes_usadas = {r["orden"] for r in ordenes}
        nueva_orden = 1
        for i in range(1, 6):
            if i not in ordenes_usadas:
                nueva_orden = i
                break

    # 5. Subir a Cloudinary en la carpeta "productos/galeria"
    resultado = cloudinary.uploader.upload(
        contents,
        folder="productos/galeria",
        public_id=f"{producto}_{uuid.uuid4().hex[:8]}_img{nueva_orden}",
        quality="auto:best",
        fetch_format="auto"
    )
    url = resultado.get("secure_url")

    # 6. Insertar en la base de datos
    execute(
        "INSERT INTO producto_imagenes (producto_id, tenant_id, url, orden) "
        "VALUES (%s, %s, %s, %s)",
        (producto_id, tenant_id, url, nueva_orden)
    )

    # 7. Sincronizar productos.imagen si la orden es 1 (la principal)
    if nueva_orden == 1:
        execute(
            "UPDATE productos SET Imagen = %s WHERE id = %s AND tenant_id = %s",
            (url, producto_id, tenant_id)
        )

    return {"ok": True, "url": url, "orden": nueva_orden}


@router.delete("/imagenes/{imagen_id}")
def eliminar_imagen_extra(imagen_id: int, tenant_id: str = Depends(get_tenant_id)):
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
        raise HTTPException(
            status_code=403,
            detail="La gestión de galería es exclusiva del plan Plus."
        )

    # 2. Obtener la imagen antes de borrar
    resultado = query(
        "SELECT id, producto_id, url, orden FROM producto_imagenes "
        "WHERE id = %s AND tenant_id = %s",
        (imagen_id, tenant_id)
    )
    if not resultado:
        raise HTTPException(status_code=404, detail="Imagen no encontrada")

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


class ReordenarImagenes(BaseModel):
    """Modelo para reordenar imágenes de la galería de un producto.
    Recibe un array de IDs en el nuevo orden deseado (primero = orden 1 = principal)."""
    ids: list[int] = Field(..., description="Array de IDs de imágenes en el nuevo orden")


@router.patch("/imagenes/{producto}/reordenar")
def reordenar_imagenes(producto: str, data: ReordenarImagenes, tenant_id: str = Depends(get_tenant_id)):
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
        raise HTTPException(
            status_code=403,
            detail="La gestión de galería es exclusiva del plan Plus."
        )

    if not data.ids:
        raise HTTPException(status_code=400, detail="El array de IDs no puede estar vacío")

    producto_id = _get_producto_id(producto, tenant_id)

    # 2. Verificar que todos los IDs pertenecen a este producto y tenant
    existentes = query(
        "SELECT id, url, orden FROM producto_imagenes "
        "WHERE producto_id = %s AND tenant_id = %s AND id = ANY(%s)",
        (producto_id, tenant_id, data.ids)
    )
    ids_validos = {r["id"] for r in existentes}
    ids_enviados = set(data.ids)
    if ids_enviados - ids_validos:
        raise HTTPException(
            status_code=400,
            detail="Algunos IDs no pertenecen a este producto o no existen."
        )

    # 3. Actualizar orden secuencialmente según la posición en el array
    for idx, img_id in enumerate(data.ids):
        nuevo_orden = idx + 1
        execute(
            "UPDATE producto_imagenes SET orden = %s WHERE id = %s AND tenant_id = %s",
            (nuevo_orden, img_id, tenant_id)
        )

    # 4. Sincronizar productos.imagen con la nueva orden 1
    primera = query(
        "SELECT url FROM producto_imagenes WHERE id = %s AND tenant_id = %s",
        (data.ids[0], tenant_id)
    )
    if primera:
        execute(
            "UPDATE productos SET Imagen = %s WHERE id = %s AND tenant_id = %s",
            (primera[0]["url"], producto_id, tenant_id)
        )

    return {"ok": True, "mensaje": "Imágenes reordenadas correctamente"}
