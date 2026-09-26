from typing import Optional, List
from pydantic import BaseModel, Field


class VariacionAlta(BaseModel):
    """Variación a crear en el ALTA de un producto (nombre + precio propio).
    stock_inicial/costo: opcionales — si se da stock, se crea el lote de ESA
    variación y el producto pasa a manejar stock por variación."""
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
    etiqueta       : Optional[str] = None
    sufijo_precio  : Optional[str] = None
    fraccionable   : Optional[bool] = None
    tipo_producto  : str = "stock"
    costo_servicio : Optional[float] = None
    precio_servicio: Optional[float] = None
    visible_en_catalogo : Optional[bool] = None
    variaciones   : Optional[list[VariacionAlta]] = None
    recetas       : Optional[list[RecetaAlta]] = None


class Restock(BaseModel):
    producto    : str
    costo       : float
    precio_venta: float
    stock       : float
    etiqueta    : Optional[str] = None
    variacion   : Optional[str] = None


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
    sufijo_precio       : Optional[str] = None
    fraccionable        : Optional[bool] = None
    tipo_producto       : Optional[str] = None
    costo_servicio      : Optional[float] = None
    precio_servicio     : Optional[float] = None


class ActualizarLote(BaseModel):
    costo       : float
    precio_venta: float
    stock       : float
    etiqueta    : Optional[str] = None
    variacion   : Optional[str] = None


class CrearCategoria(BaseModel):
    nombre: str = Field(..., min_length=1, description="Nombre de la categoría a crear")


class RenombrarCategoria(BaseModel):
    nuevo_nombre: str = Field(..., min_length=1, description="Nuevo nombre para la categoría")


class BorrarImagen(BaseModel):
    url: str = Field(..., description="URL de Cloudinary a borrar")


class NuevaVariacion(BaseModel):
    producto: str = Field(..., description="Nombre del producto al que pertenece la variación")
    nombre  : str = Field(..., description="Nombre de la variación (ej. 'Doble', 'S', 'Premium')")
    precio  : float = Field(0, description="Precio propio de la variación")
    foto    : str = Field("", description="URL de Cloudinary de la foto propia de la variación (opcional)")
    stock_inicial: Optional[float] = Field(None, description="Stock inicial propio de esta variación")
    costo   : Optional[float] = Field(None, description="Costo del lote inicial (si se omite, usa 0)")


class ActualizarVariacion(BaseModel):
    nombre : str = Field(..., description="Nuevo nombre de la variación")
    precio : float = Field(0, description="Nuevo precio de la variación")
    foto   : Optional[str] = Field(None, description="URL de la foto; None = conservar, '' = quitar")


class NuevoMaterialReceta(BaseModel):
    producto : str = Field(..., description="Nombre del producto compuesto (el que se vende)")
    material : str = Field(..., description="Nombre del material que consume (producto de stock)")
    cantidad : float = Field(1, description="Cantidad de material por unidad del compuesto")
    variacion_id : Optional[int] = Field(None, description="Id de la variación; None = receta base")


class ActualizarMaterialReceta(BaseModel):
    cantidad : float = Field(..., description="Nueva cantidad de material por unidad")


class ActualizarPerfil(BaseModel):
    modo_precio_sugerido: Optional[str] = None
    zona_horaria: Optional[str] = None
    metodo_pago_default: Optional[str] = None
    gasto_comision_automatico: Optional[bool] = None
    # ── Cartera de clientes (migración 038) ──
    clientes_activos: Optional[bool] = None
    # {email | telefono | pin: {activo, requerido}}; 'nombre' es fijo.
    cliente_campos: Optional[dict] = None
    # ── Sistema de puntos (migración 039) ──
    puntos_activos: Optional[bool] = None
    puntos_valor_punto: Optional[float] = None   # $ que vale 1 punto (1 = "1 pt = $1"; 0.01 = "100 pts = $1")
    puntos_modo: Optional[str] = None            # 'por_gasto' | 'fijo'
    puntos_gasto_monto: Optional[float] = None   # Y en "X pts por cada $Y"
    puntos_gasto_pts: Optional[int] = None       # X en "X pts por cada $Y"
    puntos_fijos: Optional[int] = None           # pts por venta cuando modo = 'fijo'


class ActualizarPostOverride(BaseModel):
    post_override: Optional[dict] = None


class ReordenarImagenes(BaseModel):
    ids: list[int] = Field(..., description="Array de IDs de imágenes en el nuevo orden")


class ConteoCaptura(BaseModel):
    """Autosave de un renglón del conteo (PATCH /inventario/conteos/{id}/items).
    El valor es ABSOLUTO (total contado, no un incremento): idempotente ante
    reintentos de red."""
    producto   : str = Field(..., description="Nombre del producto contado")
    variacion  : str = Field("", description="Nombre de la variación contada ('' = base)")
    contado    : Optional[float] = Field(None, ge=0, description="Total contado físico. None = borrar la captura")


class GuardarCapturasConteo(BaseModel):
    items: list[ConteoCaptura] = Field(..., description="Renglones a actualizar en el autosave")


class ConteoResolucion(BaseModel):
    """Decisión de cierre para un renglón con diferencia (POST /inventario/conteos/{id}/cerrar)."""
    producto    : str = Field(..., description="Nombre del producto")
    variacion   : str = Field("", description="Nombre de la variación ('' = base)")
    resolucion  : Optional[str] = Field(None, description="merma | venta | error_sistema | entrada_no_registrada. Si se omite: merma para faltantes, error_sistema para sobrantes")
    costo       : Optional[float] = Field(None, description="Solo entrada_no_registrada: costo de la mercancía hallada (default: costo del último lote activo)")
    precio_venta: Optional[float] = Field(None, description="Solo entrada_no_registrada: precio de venta (default: precio del último lote activo)")


class CerrarConteo(BaseModel):
    items: list[ConteoResolucion] = Field([], description="Resoluciones por renglón; los no listados usan el default")
    fecha: Optional[str] = Field(None, description="Fecha de las ventas declaradas ('YYYY-MM-DD' o ISO). Default: ahora")
