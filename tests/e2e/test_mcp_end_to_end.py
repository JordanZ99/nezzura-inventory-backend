# ==============================================================================
# backend/tests/e2e/test_mcp_end_to_end.py
# Prueba de integración END-TO-END del conector IA (servidor MCP).
#
# Requiere: docker run -d --name goyangi-mcp-test -e POSTGRES_PASSWORD=test123
#           -e POSTGRES_DB=goyangi -p 54399:5432 postgres:16-alpine
#           + backend/tests/e2e/mcp_harness.sql aplicado
#           + uvicorn en el puerto 8001 con DATABASE_URL/SUPABASE_URL de prueba
#
# Valida: migración 065, middleware X-API-Key, aislamiento multitenant,
# filtrado de costos, fórmula de precio del catálogo público, stock exacto,
# fotos, servicios/inactivos excluidos, anotaciones read-only y el flujo
# completo JSON-RPC (initialize -> tools/list -> tools/call).
# ==============================================================================

import asyncio
import hashlib
import json
import secrets
import sys

import httpx
import httpx2
import psycopg2
from psycopg2.extras import RealDictCursor

# El modulo database.conexion crea su pool al importarse: apuntarlo a la DB
# de prueba ANTES de importar mcp_claves (load_dotenv no pisa env vars).
import os
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:test123@127.0.0.1:54399/goyangi")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from database import mcp_claves as claves_db

DB = "postgresql://postgres:test123@127.0.0.1:54399/goyangi"
BASE = "http://127.0.0.1:8001"

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
TENANT_C = "33333333-3333-3333-3333-333333333333"

FALLOS: list[str] = []


def verificar(nombre: str, condicion: bool, detalle: str = ""):
    estado = "[OK]  " if condicion else "[FALLO]"
    print(f"  {estado} {nombre}" + (f" -- {detalle}" if detalle and not condicion else ""))
    if not condicion:
        FALLOS.append(nombre)


def q(sql, params=()):
    conn = psycopg2.connect(DB)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            if cur.description is None:
                return []
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.commit()
        conn.close()


CLAVE_A = ""
CLAVE_B = ""
CLAVE_A2 = ""
CLAVE_A3 = ""
CLAVE_B2 = ""
CLAVE_C2 = ""
CLAVE_INV = ""
CLAVE_CAJ = ""


def seed():
    global CLAVE_A, CLAVE_B, CLAVE_A2, CLAVE_A3, CLAVE_B2
    # El ledger de inventario es append-only (trigger): se limpia aparte.
    q("ALTER TABLE inventario_movimientos DISABLE TRIGGER trigger_movimientos_append_only")
    q("DELETE FROM inventario_movimientos")
    q("ALTER TABLE inventario_movimientos ENABLE TRIGGER trigger_movimientos_append_only")
    q("DELETE FROM mcp_api_keys")
    for t in ("producto_imagenes", "producto_variaciones", "producto_categorias",
              "lotes", "categorias"):
        q(f"DELETE FROM {t}")
    q("DELETE FROM productos")
    q("DELETE FROM ventas")
    q("DELETE FROM gastos")
    q("DELETE FROM tenants")

    q("INSERT INTO tenants (id, empresa, plan) VALUES (%s,'Tienda A','plus'), (%s,'Tienda B','basico'), (%s,'Tienda C sin ventas','plus')",
      (TENANT_A, TENANT_B, TENANT_C))

    # Producto simple: 2 lotes (uno agotado). El costo NO debe salir por ningun lado.
    camisa = q(
        """INSERT INTO productos (producto, Descripcion, imagen, Estado, tenant_id,
                                 codigo_interno, sufijo_precio, tamano_cm, tipo_producto)
           VALUES ('Camisa Polo Azul', 'Camisa polo de algodon, talla M, azul marino',
                   'https://res.cloudinary.com/demo/camisa.jpg', 'Activo', %s,
                   'CAM-01', '', 25.0, 'stock') RETURNING id""",
        (TENANT_A,))[0]["id"]
    q("""INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote,
                            Fecha_Entrada, Estado, tenant_id, producto_id)
         VALUES ('L1','Camisa Polo Azul', 40, 89.90, 14, '2026-01-01','Activo',%s,%s),
                ('L2','Camisa Polo Azul', 45, 95.00,  0, '2026-01-02','Activo',%s,%s)""",
      (TENANT_A, camisa, TENANT_A, camisa))

    # Producto con variaciones (cada variacion con su lote y stock propio)
    llavero = q(
        """INSERT INTO productos (producto, Descripcion, imagen, Estado, tenant_id, tipo_producto)
           VALUES ('Llavero Corazones', 'Llavero metalico en forma de corazon',
                   'https://res.cloudinary.com/demo/llavero.jpg', 'Activo', %s, 'stock')
           RETURNING id""", (TENANT_A,))[0]["id"]
    blanco = q("INSERT INTO producto_variaciones (producto_id, nombre, precio, tenant_id, foto) "
               "VALUES (%s,'Blanco',25,%s,'https://res.cloudinary.com/demo/llavero_b.jpg') RETURNING id",
               (llavero, TENANT_A))[0]["id"]
    rojo = q("INSERT INTO producto_variaciones (producto_id, nombre, precio, tenant_id) "
             "VALUES (%s,'Rojo',30,%s) RETURNING id", (llavero, TENANT_A))[0]["id"]
    q("""INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Estado, tenant_id, producto_id, variacion_id)
         VALUES ('L3','Llavero Corazones', 10, 25, 3, 'Activo', %s, %s, %s),
                ('L4','Llavero Corazones', 12, 30, 5, 'Activo', %s, %s, %s)""",
      (TENANT_A, llavero, blanco, TENANT_A, llavero, rojo))

    # Galeria Plus de la camisa (orden 2 y 3)
    q("INSERT INTO producto_imagenes (producto_id, tenant_id, url, orden) VALUES "
      "(%s,%s,'https://res.cloudinary.com/demo/camisa2.jpg',2),"
      "(%s,%s,'https://res.cloudinary.com/demo/camisa3.jpg',3)",
      (camisa, TENANT_A, camisa, TENANT_A))

    # Producto SIN foto (debe devolver error orientado al dueno)
    sin_foto = q(
        """INSERT INTO productos (producto, Descripcion, imagen, Estado, tenant_id, tipo_producto)
           VALUES ('Producto Sin Foto', 'sin foto', 'No hay foto', 'Activo', %s, 'stock')
           RETURNING id""", (TENANT_A,))[0]["id"]
    q("INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Estado, tenant_id, producto_id) "
      "VALUES ('L5','Producto Sin Foto', 5, 10, 2, 'Activo', %s, %s)", (TENANT_A, sin_foto))

    # Servicio e inactivo: NUNCA deben salir (alcance: solo fisicos activos)
    q("""INSERT INTO productos (producto, imagen, Estado, tenant_id, tipo_producto, costo_servicio, precio_servicio)
         VALUES ('Corte de Cabello','No hay foto','Activo',%s,'servicio',50,150)""", (TENANT_A,))
    q("""INSERT INTO productos (producto, imagen, Estado, tenant_id, tipo_producto)
         VALUES ('Producto Inactivo','No hay foto','Inactivo',%s,'stock')""", (TENANT_A,))

    ropa = q("INSERT INTO categorias (tenant_id, nombre, slug) VALUES (%s,'Ropa','ropa') RETURNING id",
             (TENANT_A,))[0]["id"]
    verano = q("INSERT INTO categorias (tenant_id, nombre, slug) VALUES (%s,'Verano','verano') RETURNING id",
               (TENANT_A,))[0]["id"]
    q("INSERT INTO producto_categorias (producto_id, categoria_id) VALUES (%s,%s),(%s,%s)",
      (camisa, ropa, camisa, verano))

    # Productos para probar la busqueda tolerante:
    #  - "MyMelody Strawberry" sin espacio (el agente busca "My Melody")
    #  - en la categoria Sanrio (el agente busca "sanrio" sin estar en el nombre)
    #  - "Pok\u00e9mon Figura" con acento (el agente busca "pokemon")
    sanrio_cat = q("INSERT INTO categorias (tenant_id, nombre, slug) VALUES (%s,'Sanrio','sanrio') RETURNING id",
                   (TENANT_A,))[0]["id"]
    mymelody = q("""INSERT INTO productos (producto, Descripcion, imagen, Estado, tenant_id, tipo_producto)
                    VALUES ('MyMelody Strawberry', 'Peluche rosa de Sanrio', 'No hay foto', 'Activo', %s, 'stock')
                    RETURNING id""", (TENANT_A,))[0]["id"]
    q("INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Estado, tenant_id, producto_id) "
      "VALUES ('L6','MyMelody Strawberry', 60, 120, 7, 'Activo', %s, %s)", (TENANT_A, mymelody))
    q("INSERT INTO producto_categorias (producto_id, categoria_id) VALUES (%s,%s)",
      (mymelody, sanrio_cat))
    pokemon = q("""INSERT INTO productos (producto, Descripcion, imagen, Estado, tenant_id, tipo_producto)
                   VALUES ('Pok\u00e9mon Figura', 'Figura coleccionable', 'No hay foto', 'Activo', %s, 'stock')
                   RETURNING id""", (TENANT_A,))[0]["id"]
    q("INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Estado, tenant_id, producto_id) "
      "VALUES ('L7','Pok\u00e9mon Figura', 30, 60, 4, 'Activo', %s, %s)", (TENANT_A, pokemon))

    # Tenant B: producto ajeno que NO debe verse con la clave de A
    q("""INSERT INTO productos (producto, imagen, Estado, tenant_id, tipo_producto)
         VALUES ('Producto De Otro Tenant','No hay foto','Activo',%s,'stock')""", (TENANT_B,))
    q("INSERT INTO categorias (tenant_id, nombre, slug) VALUES (%s,'Cat Ajena','cat-ajena')", (TENANT_B,))

    # Ventas de este mes (nivel 2: el analisis necesita >20 ventas para no
    # estar en cold start) + gastos (uno pagado, uno pendiente de confirmar).
    # producto_id y tipo_producto llenos: actualizar_venta (ediciones nivel 3)
    # los usa para ajustar el stock de los lotes.
    for i in range(26):
        producto = "Camisa Polo Azul" if i % 2 == 0 else "Llavero Corazones"
        pid = camisa if i % 2 == 0 else llavero
        precio = 89.9 if i % 2 == 0 else 25.0
        costo_u = 40.0 if i % 2 == 0 else 10.0
        q("INSERT INTO ventas (Fecha, Producto, Cantidad, Precio_Lista, Precio_Real, "
          "Costo_Unitario, Total_Venta, Ganancia_Bruta, Estado, ID_Lote, tenant_id, "
          "fecha_ts, producto_id, tipo_producto) "
          "VALUES (now()::date::text,%s,1,%s,%s,%s,%s,%s,'Activo','L1',%s,now(),%s,'stock')",
          (producto, precio, precio, costo_u, precio, precio - costo_u, TENANT_A, pid))
    q("INSERT INTO gastos (Fecha, Categoria, Descripcion, Monto, tenant_id, fecha_negocio, estado) "
      "VALUES (now()::date::text,'Renta','Renta mensual',500,%s,now()::date,'pagado'),"
      "(now()::date::text,'Papeleria','Bolsas',30,%s,now()::date,'pendiente')",
      (TENANT_A, TENANT_A))

    # Claves API por nivel (hash SHA-256, igual que database/mcp_claves.py)
    global CLAVE_A, CLAVE_B, CLAVE_A2, CLAVE_A3, CLAVE_B2, CLAVE_C2
    CLAVE_A = f"nezzura_mcp_{secrets.token_hex(24)}"
    CLAVE_B = f"nezzura_mcp_{secrets.token_hex(24)}"
    CLAVE_A2 = f"nezzura_mcp_{secrets.token_hex(24)}"
    CLAVE_A3 = f"nezzura_mcp_{secrets.token_hex(24)}"
    CLAVE_B2 = f"nezzura_mcp_{secrets.token_hex(24)}"
    CLAVE_C2 = f"nezzura_mcp_{secrets.token_hex(24)}"
    for tenant, clave, nivel, roles in (
        (TENANT_A, CLAVE_A, 1, "publicador"), (TENANT_B, CLAVE_B, 1, "publicador"),
        (TENANT_A, CLAVE_A2, 2, "publicador,analista"), (TENANT_A, CLAVE_A3, 3, "inventario,disenador"),
        (TENANT_B, CLAVE_B2, 2, "publicador,analista"), (TENANT_C, CLAVE_C2, 2, "publicador,analista"),
    ):
        q("INSERT INTO mcp_api_keys (tenant_id, nombre, key_hash, key_prefix, nivel, roles) "
          "VALUES (%s,%s,%s,%s,%s,%s)",
          (tenant, "IA de prueba", hashlib.sha256(clave.encode()).hexdigest(), clave[:12], nivel, roles))

    return camisa, llavero, sin_foto, mymelody, pokemon


async def llamar(sesion, tool: str, args: dict) -> dict:
    """call_tool -> dict: structured_content si existe, si no el texto JSON.
    Si el texto no es JSON (ej. error JSON-RPC de tool inexistente, que
    llega con texto vacío), se devuelve como {"error": ...}."""
    resultado = await sesion.call_tool(tool, args)
    if resultado.structured_content is not None:
        return resultado.structured_content
    if resultado.is_error:
        return {"error": "llamada rechazada (tool inexistente o fallo del server)"}
    for bloque in resultado.content:
        texto = getattr(bloque, "text", None)
        if texto:
            try:
                return json.loads(texto)
            except json.JSONDecodeError:
                return {"error": texto}
    return {}


async def prueba_mcp(camisa, llavero, sin_foto, mymelody, pokemon):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    print("\n-- Flujo MCP con la clave del tenant A --")
    cliente_a = httpx2.AsyncClient(headers={"X-API-Key": CLAVE_A}, timeout=30)
    async with streamable_http_client(
        f"{BASE}/mcp", http_client=cliente_a
    ) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            await sesion.initialize()

            herramientas = await sesion.list_tools()
            nombres = {t.name for t in herramientas.tools}
            verificar("rol publicador: 4 herramientas (sin stock ni escritura)", nombres == {
                "buscar_productos", "listar_productos",
                "listar_categorias", "datos_para_publicar",
            }, str(nombres))
            verificar("obtener_producto y obtener_fotos retiradas",
                      "obtener_producto" not in nombres and "obtener_fotos_producto" not in nombres)
            verificar("todos read-only",
                      all(t.annotations and t.annotations.read_only_hint for t in herramientas.tools))

            r = await llamar(sesion, "buscar_productos", {"consulta": "camisa"})
            prods = r.get("productos", [])
            verificar("buscar 'camisa': 1 resultado", len(prods) == 1, json.dumps(r)[:300])
            verificar("precio = formula del catalogo (89.90)", prods and prods[0]["precio_venta"] == 89.9, json.dumps(prods))
            verificar("stock exacto (14)", prods and prods[0]["stock_disponible"] == 14)
            verificar("id correcto", prods and prods[0]["id"] == camisa)

            # Busqueda tolerante: espacios, acentos y categorias
            r = await llamar(sesion, "buscar_productos", {"consulta": "my melody"})
            verificar("busqueda 'my melody' (con espacio) encuentra MyMelody",
                      r.get("total") == 1 and r["productos"][0]["id"] == mymelody, json.dumps(r))

            r = await llamar(sesion, "buscar_productos", {"consulta": "sanrio"})
            verificar("busqueda por categoria 'sanrio' lo encuentra",
                      r.get("total") == 1 and r["productos"][0]["id"] == mymelody, json.dumps(r))

            r = await llamar(sesion, "buscar_productos", {"consulta": "pokemon"})
            verificar("busqueda sin acento encuentra Pok\u00e9mon",
                      r.get("total") == 1 and r["productos"][0]["id"] == pokemon, json.dumps(r))

            r = await llamar(sesion, "buscar_productos", {"consulta": "melody"})
            verificar("relevancia: MyMelody primero en 'melody'",
                      r.get("total") >= 1 and r["productos"][0]["id"] == mymelody, json.dumps(r))

            r = await llamar(sesion, "datos_para_publicar", {"producto_id": llavero})
            texto = json.dumps(r)
            verificar("datos_para_publicar sin costos", "osto" not in texto, texto[:300])
            verificar("variaciones con precio/stock propios",
                      bool(r.get("variaciones"))
                      and r["variaciones"][0]["stock"] == 3
                      and r["variaciones"][1]["precio"] == 30,
                      json.dumps(r.get("variaciones")))
            verificar("stock total del llavero = 8 (3+5)", r.get("stock_disponible") == 8)
            verificar("fotos incluidas", r.get("fotos", {}).get("total") == 1)

            # datos_para_publicar es LA única vía de ficha + fotos (las tools
            # obtener_producto / obtener_fotos_producto se retiraron).
            r = await llamar(sesion, "datos_para_publicar", {"producto_id": camisa})
            verificar("ficha: descripcion y tamano", r.get("tamano_cm") == 25.0 and "algodon" in r.get("descripcion", ""))
            verificar("ficha: categorias", set(r.get("categoria", [])) == {"Ropa", "Verano"}, json.dumps(r.get("categoria")))
            verificar("ficha: condicion_sugerida", r.get("condicion_sugerida") == "nuevo")
            galeria = r.get("fotos", {}).get("galeria") or []
            verificar("fotos: principal + galeria ordenada (3)",
                      r.get("fotos", {}).get("total") == 3
                      and galeria and galeria[0].get("url", "").endswith("camisa.jpg"),
                      json.dumps(r.get("fotos")))

            r = await llamar(sesion, "datos_para_publicar", {"producto_id": sin_foto})
            verificar("sin foto: error orientado al dueno", "error" in r and "foto" in r["error"], json.dumps(r))

            r = await llamar(sesion, "buscar_productos", {"consulta": "cabello"})
            verificar("servicios excluidos", r.get("total") == 0)

            r = await llamar(sesion, "buscar_productos", {"consulta": "inactivo"})
            verificar("inactivos excluidos", r.get("total") == 0)

            r = await llamar(sesion, "buscar_productos", {"consulta": "otro tenant"})
            verificar("aislamiento: productos de otro tenant invisibles", r.get("total") == 0)

            r = await llamar(sesion, "listar_categorias", {})
            verificar("categorias solo del tenant", set(r.get("categorias", [])) == {"Ropa", "Sanrio", "Verano"}, json.dumps(r))

            r = await llamar(sesion, "datos_para_publicar", {"producto_id": 999999})
            verificar("id inexistente: error claro", "error" in r, json.dumps(r))

            # ── listar_productos: listado completo con filtros ──
            r = await llamar(sesion, "listar_productos", {})
            verificar("listar: total de fisicos activos de A (5)",
                      r.get("total") == 5, json.dumps(r)[:250])
            verificar("listar: paginacion informada", r.get("paginas") == 1 and r.get("pagina") == 1)
            verificar("listar: sin costos", "osto_unitario" not in json.dumps(r))
            verificar("listar: marca sin_foto/sin_descripcion",
                      all("tiene_foto" in p and "sin_descripcion" in p for p in r["productos"]))

            r = await llamar(sesion, "listar_productos", {"categoria": "sanrio"})
            verificar("listar por categoria (tolerante): solo MyMelody",
                      r.get("total") == 1 and r["productos"][0]["id"] == mymelody, json.dumps(r)[:200])

            r = await llamar(sesion, "listar_productos", {"estado_stock": "sin_stock"})
            verificar("listar sin_stock: vacio (todos tienen stock)", r.get("total") == 0, json.dumps(r)[:150])

            r = await llamar(sesion, "listar_productos", {"sin_datos": "sin_foto"})
            verificar("listar sin_foto: 3 productos",
                      r.get("total") == 3
                      and all(not p["tiene_foto"] for p in r["productos"]), json.dumps(r)[:250])

            r = await llamar(sesion, "listar_productos", {"sin_datos": "sin_tamanio"})
            verificar("listar sin_tamanio: 4 (todos menos la camisa)",
                      r.get("total") == 4, json.dumps(r)[:250])

            r = await llamar(sesion, "listar_productos", {"limite": 2, "pagina": 2, "orden": "nombre"})
            verificar("listar pagina 2 con limite 2: trae 2 y 3 paginas",
                      len(r["productos"]) == 2 and r["paginas"] == 3, json.dumps(r)[:250])

            r = await llamar(sesion, "listar_productos", {"estado_stock": "disponible", "orden": "stock_asc"})
            stocks = [p["stock_disponible"] for p in r["productos"]]
            verificar("listar disponible orden stock_asc: ordenado", stocks == sorted(stocks), str(stocks))

            r = await llamar(sesion, "listar_productos", {"estado_stock": "sin sentido"})
            verificar("listar estado_stock invalido: ignorado (no crashea)", "error" not in r and r.get("total") == 5)

            # Nivel 1: el analisis y la escritura estan BLOQUEADOS (defensa
            # en profundidad, aunque tools/list ya no los liste)
            r = await llamar(sesion, "resumen_negocio", {})
            verificar("sin rol analista: resumen_negocio bloqueado", "error" in r and "rol" in r["error"], json.dumps(r))
            r = await llamar(sesion, "proponer_producto", {"nombre": "x", "precio_venta": 1})
            verificar("nivel 1: proponer_producto bloqueado", "error" in r, json.dumps(r))
            r = await llamar(sesion, "proponer_edicion", {"tipo": "producto", "entidad_id": camisa, "cambios": {"estado": "Inactivo"}})
            verificar("nivel 1: proponer_edicion bloqueado", "error" in r, json.dumps(r))

    # Rate limit + uso registrado
    filas = q("SELECT llamadas_hoy, dia_uso IS NOT NULL AS con_dia, ultimo_uso_en IS NOT NULL AS con_uso "
              "FROM mcp_api_keys WHERE key_hash = %s", (hashlib.sha256(CLAVE_A.encode()).hexdigest(),))
    verificar("uso registrado (llamadas_hoy > 0)", filas[0]["llamadas_hoy"] > 0, json.dumps(filas[0], default=str))

    print("\n-- Nivel 2: analisis del negocio con datos reales --")
    cliente_n2 = httpx2.AsyncClient(headers={"X-API-Key": CLAVE_A2}, timeout=30)
    async with streamable_http_client(f"{BASE}/mcp", http_client=cliente_n2) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            await sesion.initialize()
            herramientas = await sesion.list_tools()
            nombres = {t.name for t in herramientas.tools}
            verificar("nivel 2: 9 herramientas (5 catalogo + 2 analisis + 2 ventas)",
                      len(nombres) == 9
                      and {"resumen_negocio", "hallazgos_inteligentes", "ventas_recientes", "top_vendidos"} <= nombres
                      and "proponer_producto" not in nombres and "confirmar_producto" not in nombres,
                      str(nombres))

            # ── stock_actual (ahora vive en el rol analista/publicador+analista) ──
            r = await llamar(sesion, "stock_actual", {"consulta": "sanrio"})
            verificar("stock_actual por categoria: 7",
                      r["productos"] and r["productos"][0]["stock_disponible"] == 7, json.dumps(r))
            r = await llamar(sesion, "stock_actual", {"consulta": "llavero"})
            verificar("stock_actual por consulta: 8", r["productos"][0]["stock_disponible"] == 8, json.dumps(r))
            r = await llamar(sesion, "stock_actual", {"producto_id": camisa})
            verificar("stock_actual por id: 14", r["productos"][0]["stock_disponible"] == 14)
            r = await llamar(sesion, "stock_actual", {})
            verificar("stock sin criterio: error claro", "error" in r)

            # ── ventas_recientes / top_vendidos (nuevas de nivel 2) ──
            r = await llamar(sesion, "ventas_recientes", {"periodo": "todo"})
            verificar("ventas_recientes: 26 ventas paginadas",
                      r.get("total") == 26 and len(r["ventas"]) == 20 and r.get("paginas") == 2,
                      json.dumps(r)[:250])
            verificar("ventas_recientes: campos de venta sin costo ni ganancia",
                      all("osto_unitario" not in v and "anancia" not in json.dumps(v) for v in r["ventas"]),
                      json.dumps(r["ventas"][0]))
            verificar("ventas_recientes: ordenado por fecha desc",
                      r["ventas"][0]["fecha"] >= r["ventas"][-1]["fecha"],
                      f'{r["ventas"][0]["fecha"]} vs {r["ventas"][-1]["fecha"]}')

            r = await llamar(sesion, "ventas_recientes", {"periodo": "todo", "pagina": 2, "limite": 20})
            verificar("ventas_recientes pagina 2: 6 restantes", len(r["ventas"]) == 6, json.dumps(r)[:150])

            r = await llamar(sesion, "ventas_recientes", {"periodo": "bogus"})
            verificar("ventas_recientes periodo invalido: error claro", "error" in r, json.dumps(r))

            r = await llamar(sesion, "top_vendidos", {"periodo": "todo"})
            top = r.get("top") or []
            verificar("top_vendidos: 2 productos, camisa primero (por ingresos)",
                      r.get("total") == 2 and top and top[0]["producto"] == "Camisa Polo Azul",
                      json.dumps(r)[:300])
            verificar("top_vendidos: unidades e ingresos coherentes",
                      top and top[0]["unidades"] == 13 and top[0]["ingresos"] == 1168.7
                      and top[1]["unidades"] == 13 and top[1]["ingresos"] == 325.0,
                      json.dumps(top))
            verificar("top_vendidos: stock actual y sin marca agotado",
                      top and top[0]["stock_actual"] == 14 and top[0]["agotado"] is False,
                      json.dumps(top[0]))

            r = await llamar(sesion, "resumen_negocio", {})
            metricas = r.get("metricas") or {}
            verificar("resumen: ingresos = suma de ventas (1493.70)",
                      metricas.get("ingresos_total") == 1493.7, json.dumps(metricas))
            verificar("resumen: ganancia y margen presentes",
                      metricas.get("ganancia_bruta_total", 0) > 0 and "margen_bruto_pct" in metricas,
                      json.dumps(metricas))
            texto_resumen = json.dumps(r)
            verificar("resumen sin costo unitario (Costo_Unitario absent)",
                      "osto_Unitario" not in texto_resumen and "productos_90" not in texto_resumen,
                      texto_resumen[:300])

            r = await llamar(sesion, "hallazgos_inteligentes", {})
            verificar("hallazgos 'todos': lista con total",
                      isinstance(r.get("hallazgos"), list) and r.get("total") == len(r.get("hallazgos", [])),
                      json.dumps(r)[:300])

            r = await llamar(sesion, "hallazgos_inteligentes", {"tipo": "stock"})
            tipos_stock = {"quiebre_best_seller", "stock_muerto", "lotes_inconsistentes", "vendido_sin_lote"}
            verificar("hallazgos filtro 'stock': solo tipos de inventario",
                      all(h.get("tipo") in tipos_stock for h in r.get("hallazgos", [])), json.dumps(r)[:300])

            r = await llamar(sesion, "hallazgos_inteligentes", {"tipo": "bancos"})
            verificar("filtro invalido: error claro", "error" in r, json.dumps(r))

            r = await llamar(sesion, "resumen_negocio", {"periodo": "semanas"})
            verificar("periodo invalido: error claro", "error" in r, json.dumps(r))

            r = await llamar(sesion, "resumen_negocio", {"periodo": "todo"})
            verificar("periodo 'todo' funciona", "metricas" in r, json.dumps(r)[:200])

            # TODOS los periodos: el bug de produccion fue que 30dias/90dias
            # crasheaban al desempacar el rango y nunca se probaron aqui.
            for periodo in ("mes", "30dias", "90dias", "30 dias", "este mes", "histórico"):
                r = await llamar(sesion, "resumen_negocio", {"periodo": periodo})
                verificar(f"periodo '{periodo}' funciona (1493.70)",
                          (r.get("metricas") or {}).get("ingresos_total") == 1493.7,
                          json.dumps(r)[:250])

            r = await llamar(sesion, "resumen_negocio", {"periodo": "mes"})
            ventana = r.get("rango_solicitado") or {}
            verificar("rango_solicitado del mes: dia 1 a fin de mes",
                      (ventana.get("desde") or "").endswith("-01") and ventana.get("hasta"),
                      json.dumps(ventana))

    print("\n-- Ventana sin ventas (mes vacio): metricas en 0, sin crash --")
    cliente_c2 = httpx2.AsyncClient(headers={"X-API-Key": CLAVE_C2}, timeout=30)
    async with streamable_http_client(f"{BASE}/mcp", http_client=cliente_c2) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            await sesion.initialize()
            r = await llamar(sesion, "resumen_negocio", {"periodo": "mes"})
            verificar("mes sin ventas: metricas en 0 y sin error",
                      "error" not in r and (r.get("metricas") or {}).get("ingresos_total") == 0,
                      json.dumps(r)[:250])
            r = await llamar(sesion, "hallazgos_inteligentes", {"periodo": "mes"})
            verificar("hallazgos sin ventas: lista vacia y sin error",
                      "error" not in r and isinstance(r.get("hallazgos"), list),
                      json.dumps(r)[:250])

    print("\n-- Nivel 2 con plan basico: el analisis exige Plus --")
    cliente_b2 = httpx2.AsyncClient(headers={"X-API-Key": CLAVE_B2}, timeout=30)
    async with streamable_http_client(f"{BASE}/mcp", http_client=cliente_b2) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            await sesion.initialize()
            r = await llamar(sesion, "resumen_negocio", {})
            verificar("plan basico: error Plus", "error" in r and "Plus" in r["error"], json.dumps(r))
            r = await llamar(sesion, "ventas_recientes", {})
            verificar("plan basico: ventas_recientes tambien exige Plus",
                      "error" in r and "Plus" in r["error"], json.dumps(r))
            r = await llamar(sesion, "top_vendidos", {})
            verificar("plan basico: top_vendidos tambien exige Plus",
                      "error" in r and "Plus" in r["error"], json.dumps(r))

    print("\n-- Nivel 3: crear productos (borrador -> confirmar) --")
    cliente_n3 = httpx2.AsyncClient(headers={"X-API-Key": CLAVE_A3}, timeout=30)
    async with streamable_http_client(f"{BASE}/mcp", http_client=cliente_n3) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            await sesion.initialize()
            herramientas = await sesion.list_tools()
            nombres = {t.name for t in herramientas.tools}
            verificar("nivel 3 (inventario+disenador): 17 herramientas", len(nombres) == 17
                      and {"proponer_producto", "confirmar_producto",
                           "proponer_edicion", "confirmar_edicion"} <= nombres, str(nombres))
            confirmar = next(t for t in herramientas.tools if t.name == "confirmar_producto")
            verificar("confirmar_producto es destructiveHint",
                      bool(confirmar.annotations and confirmar.annotations.destructive_hint))
            confirmar_e = next(t for t in herramientas.tools if t.name == "confirmar_edicion")
            verificar("confirmar_edicion tambien es destructiveHint",
                      bool(confirmar_e.annotations and confirmar_e.annotations.destructive_hint))

            # Datos invalidos: el error sale ANTES de crear nada
            r = await llamar(sesion, "proponer_producto", {"nombre": "", "precio_venta": 5})
            verificar("proponer sin nombre: error claro", "error" in r, json.dumps(r))
            r = await llamar(sesion, "proponer_producto", {"nombre": "X", "precio_venta": -5})
            verificar("proponer precio negativo: error claro", "error" in r)
            r = await llamar(sesion, "proponer_producto",
                             {"nombre": "X", "precio_venta": 10, "stock": 5,
                              "variaciones": [{"nombre": "Chico", "precio": 10, "stock_inicial": 5}]})
            verificar("proponer stock + variaciones mezclados: error", "error" in r, json.dumps(r))

            # Flujo feliz simple
            r = await llamar(sesion, "proponer_producto",
                             {"nombre": "Postre Matcha", "precio_venta": 55, "costo": 20,
                              "stock": 10, "descripcion": "Postre de te matcha",
                              "categoria": ["Postres"]})
            borrador = r.get("borrador_id", "")
            verificar("proponer: devuelve borrador_id", bool(borrador), json.dumps(r)[:300])
            verificar("proponer: resumen legible", "Postre Matcha" in r.get("resumen", ""))

            r = await llamar(sesion, "confirmar_producto", {"borrador_id": borrador})
            verificar("confirmar: crea el producto", r.get("ok") is True and r.get("producto_id"), json.dumps(r))
            producto_nuevo = r.get("producto_id")

            r = await llamar(sesion, "confirmar_producto", {"borrador_id": borrador})
            verificar("confirmar 2 veces con el mismo token: error", "error" in r, json.dumps(r))

            r = await llamar(sesion, "confirmar_producto", {"borrador_id": "token-falso-123"})
            verificar("confirmar token inexistente: error", "error" in r)

            r = await llamar(sesion, "buscar_productos", {"consulta": "matcha"})
            verificar("producto creado visible en el catalogo",
                      r.get("total") == 1 and r["productos"][0]["id"] == producto_nuevo, json.dumps(r))

            # Flujo con variaciones (stock por variacion, sin stock general)
            r = await llamar(sesion, "proponer_producto",
                             {"nombre": "Bebida Fresa", "precio_venta": 18, "costo": 7,
                              "variaciones": [{"nombre": "Chica", "precio": 18, "stock_inicial": 4},
                                              {"nombre": "Grande", "precio": 25, "stock_inicial": 6}]})
            borrador2 = r.get("borrador_id", "")
            verificar("proponer con variaciones: borrador", bool(borrador2), json.dumps(r)[:300])
            r = await llamar(sesion, "confirmar_producto", {"borrador_id": borrador2})
            verificar("confirmar con variaciones: ok", r.get("ok") is True, json.dumps(r))

            # ── EDICIONES (borrador -> confirmar) ──
            # Borrador cruzado: un borrador de CREACION no se confirma con
            # confirmar_edicion (se consume, pero da error y no toca nada).
            r = await llamar(sesion, "proponer_producto",
                             {"nombre": "Borrador Cruce", "precio_venta": 10})
            r = await llamar(sesion, "confirmar_edicion", {"borrador_id": r.get("borrador_id")})
            verificar("confirmar_edicion con borrador de creacion: error",
                      "error" in r and "confirmar_producto" in r["error"], json.dumps(r))

            # Campos no editables y tipo invalido
            r = await llamar(sesion, "proponer_edicion",
                             {"tipo": "producto", "entidad_id": camisa,
                              "cambios": {"imagen": "https://x.com/f.jpg"}})
            verificar("editar imagen: no permitido", "error" in r and "no editables" in r["error"], json.dumps(r))
            r = await llamar(sesion, "proponer_edicion",
                             {"tipo": "cliente", "entidad_id": 1, "cambios": {"x": 1}})
            verificar("tipo invalido: error claro", "error" in r, json.dumps(r))
            r = await llamar(sesion, "proponer_edicion",
                             {"tipo": "producto", "entidad_id": camisa, "cambios": {}})
            verificar("cambios vacios: error claro", "error" in r)
            r = await llamar(sesion, "proponer_edicion",
                             {"tipo": "producto", "entidad_id": camisa,
                              "cambios": {"costo": 38}})
            verificar("editar costo SIN permiso ver_costos: error",
                      "error" in r and "costos" in r["error"], json.dumps(r))
            r = await llamar(sesion, "proponer_edicion",
                             {"tipo": "venta", "entidad_id": 999999, "cambios": {"cantidad": 2}})
            verificar("venta inexistente: error claro", "error" in r)
            r = await llamar(sesion, "proponer_edicion",
                             {"tipo": "producto", "entidad_id": camisa, "cambios": {"descripcion": "Camisa polo de algodon, talla M, azul marino"}})
            verificar("cambios identicos: nada que editar", "error" in r and "igual" in r["error"], json.dumps(r))

            # Flujo feliz PRODUCTO: descripcion + precio de venta
            r = await llamar(sesion, "proponer_edicion",
                             {"tipo": "producto", "entidad_id": camisa,
                              "cambios": {"descripcion": "Camisa polo premium, talla M",
                                          "precio_venta": 99.9, "estado": "Activo"}})
            borrador_e = r.get("borrador_id", "")
            verificar("proponer_edicion producto: borrador con diff",
                      bool(borrador_e) and "99.9" in r.get("resumen", ""), json.dumps(r)[:400])
            verificar("proponer_edicion: diff incluye antes y despues",
                      all(k in r["cambios_propuestos"] for k in ("descripcion", "precio_venta")),
                      json.dumps(r.get("cambios_propuestos")))

            r = await llamar(sesion, "confirmar_edicion", {"borrador_id": borrador_e})
            verificar("confirmar_edicion producto: ok", r.get("ok") is True and r.get("entidad") == "producto",
                      json.dumps(r))
            fila = q("SELECT Descripcion AS d FROM productos WHERE id = %s", (camisa,))
            verificar("producto editado en la base", fila[0]["d"] == "Camisa polo premium, talla M")
            precios = q("SELECT Precio_Venta AS pv FROM lotes WHERE producto_id = %s AND Estado='Activo'", (camisa,))
            verificar("precio del lote actualizado a 99.9", any(float(p["pv"]) == 99.9 for p in precios),
                      json.dumps(precios, default=str))
            r = await llamar(sesion, "confirmar_edicion", {"borrador_id": borrador_e})
            verificar("confirmar_edicion 2 veces: error", "error" in r)

            # Historial: la edicion del producto queda atribuida al agente
            act = q("SELECT dominio, resumen, detalle FROM registro_actividad "
                    "WHERE tenant_id = %s AND dominio = 'producto' AND accion = 'editar' "
                    "ORDER BY fecha DESC LIMIT 1", (TENANT_A,))
            verificar("historial producto: 'El agente IA editó' + origen",
                      act and "agente IA" in act[0]["resumen"]
                      and act[0]["detalle"].get("origen") == "agente_ia",
                      json.dumps(act, default=str))

            # Flujo feliz VENTA: cantidad 1 -> 3 y precio 89.9 -> 95
            venta_id = q("SELECT id FROM ventas WHERE Producto = 'Camisa Polo Azul' "
                         "AND tenant_id = %s AND ID_Lote = 'L1' ORDER BY id LIMIT 1",
                         (TENANT_A,))[0]["id"]
            stock_antes = q("SELECT COALESCE(SUM(Stock_Lote),0) AS s FROM lotes WHERE producto_id=%s", (camisa,))[0]["s"]
            r = await llamar(sesion, "proponer_edicion",
                             {"tipo": "venta", "entidad_id": venta_id,
                              "cambios": {"cantidad": 3, "precio_real": 95}})
            borrador_v = r.get("borrador_id", "")
            verificar("proponer_edicion venta: borrador",
                      bool(borrador_v) and "95" in r.get("resumen", ""), json.dumps(r)[:300])

            r = await llamar(sesion, "confirmar_edicion", {"borrador_id": borrador_v})
            verificar("confirmar_edicion venta: ok", r.get("ok") is True and r.get("entidad") == "venta",
                      json.dumps(r))
            fila = q("SELECT Cantidad AS c, Precio_Real AS pr, Total_Venta AS t, Ganancia_Bruta AS g FROM ventas WHERE id = %s", (venta_id,))
            verificar("venta editada en la base con totales recalculados",
                      float(fila[0]["c"]) == 3 and float(fila[0]["pr"]) == 95
                      and float(fila[0]["t"]) == 285,
                      json.dumps(fila, default=str))
            stock_despues = q("SELECT COALESCE(SUM(Stock_Lote),0) AS s FROM lotes WHERE producto_id=%s", (camisa,))[0]["s"]
            verificar("stock ajustado por la edicion de venta (-2)",
                      float(stock_despues) == float(stock_antes) - 2,
                      f"{stock_antes} -> {stock_despues}")
            act = q("SELECT dominio, resumen, detalle FROM registro_actividad "
                    "WHERE tenant_id = %s AND dominio = 'venta' AND accion = 'editar' "
                    "ORDER BY fecha DESC LIMIT 1", (TENANT_A,))
            verificar("historial venta: 'El agente IA editó la venta' + diff",
                      act and "agente IA" in act[0]["resumen"]
                      and act[0]["detalle"].get("cantidad") == [1, 3],
                      json.dumps(act, default=str))

            # Flujo feliz GASTO: monto 500 -> 520
            gasto_id = q("SELECT id FROM gastos WHERE tenant_id = %s AND Categoria = 'Renta' ORDER BY id LIMIT 1",
                         (TENANT_A,))[0]["id"]
            r = await llamar(sesion, "proponer_edicion",
                             {"tipo": "gasto", "entidad_id": gasto_id, "cambios": {"monto": 520}})
            borrador_g = r.get("borrador_id", "")
            verificar("proponer_edicion gasto: borrador", bool(borrador_g), json.dumps(r)[:200])
            r = await llamar(sesion, "confirmar_edicion", {"borrador_id": borrador_g})
            verificar("confirmar_edicion gasto: ok", r.get("ok") is True and r.get("entidad") == "gasto",
                      json.dumps(r))
            fila = q("SELECT Monto AS m FROM gastos WHERE id = %s", (gasto_id,))
            verificar("gasto editado en la base", float(fila[0]["m"]) == 520)
            act = q("SELECT dominio, resumen FROM registro_actividad "
                    "WHERE tenant_id = %s AND dominio = 'gasto' AND accion = 'editar' "
                    "ORDER BY fecha DESC LIMIT 1", (TENANT_A,))
            verificar("historial gasto: 'El agente IA editó el gasto'",
                      act and "agente IA" in act[0]["resumen"], json.dumps(act, default=str))

            # ── Permiso ver_costos: edita costo con el switch encendido ──
            id_a3 = q("SELECT id FROM mcp_api_keys WHERE key_hash = %s",
                      (hashlib.sha256(CLAVE_A3.encode()).hexdigest(),))[0]["id"]
            claves_db.cambiar_ver_costos(id_a3, TENANT_A, True)
            # La vía del agente para el costo de un producto individual con
            # el permiso encendido: buscar_productos / listar_productos.
            r = await llamar(sesion, "buscar_productos", {"consulta": "camisa"})
            verificar("ver_costos ON: la busqueda incluye costo y margen",
                      r["productos"] and r["productos"][0].get("costo_unitario") == 40
                      and r["productos"][0].get("margen") == 59.9, json.dumps(r)[:300])
            r = await llamar(sesion, "listar_productos", {"categoria": "ropa"})
            verificar("ver_costos ON: listar tambien incluye costo",
                      r["productos"] and r["productos"][0].get("costo_unitario") == 40, json.dumps(r)[:250])
            r = await llamar(sesion, "datos_para_publicar", {"producto_id": camisa})
            verificar("datos_para_publicar JAMAS incluye costos (ni con permiso)",
                      "osto_unitario" not in json.dumps(r), json.dumps(r)[:200])
            r = await llamar(sesion, "proponer_edicion",
                             {"tipo": "producto", "entidad_id": camisa, "cambios": {"costo": 38}})
            borrador_c = r.get("borrador_id", "")
            verificar("editar costo CON permiso: borrador", bool(borrador_c), json.dumps(r)[:200])
            r = await llamar(sesion, "confirmar_edicion", {"borrador_id": borrador_c})
            verificar("confirmar_edicion costo: ok", r.get("ok") is True, json.dumps(r))
            costos = q("SELECT DISTINCT Costo AS c FROM lotes WHERE producto_id = %s AND Estado='Activo'", (camisa,))
            verificar("costo de los lotes activos actualizado a 38",
                      len(costos) == 1 and float(costos[0]["c"]) == 38, json.dumps(costos, default=str))
            claves_db.cambiar_ver_costos(id_a3, TENANT_A, False)
            r = await llamar(sesion, "buscar_productos", {"consulta": "camisa"})
            verificar("ver_costos OFF: la busqueda vuelve sin costo",
                      "costo_unitario" not in json.dumps(r), json.dumps(r)[:200])

    escrituras = q("SELECT escrituras_hoy FROM mcp_api_keys WHERE key_hash = %s",
                   (hashlib.sha256(CLAVE_A3.encode()).hexdigest(),))
    # 2 creaciones + 1 borrador cruzado consumido + 4 ediciones (producto,
    # venta, gasto, costo) = 7 escrituras contadas.
    verificar("escrituras contadas (7)", escrituras[0]["escrituras_hoy"] == 7,
              json.dumps(escrituras[0], default=str))

    print("\n-- Edicion de claves: nivel sin regenerar, limites por clave, hard delete --")
    id_a = q("SELECT id, nivel FROM mcp_api_keys WHERE key_hash = %s",
             (hashlib.sha256(CLAVE_A.encode()).hexdigest(),))[0]
    verificar("nivel inicial de la clave A = 1", id_a["nivel"] == 1, json.dumps(id_a, default=str))

    # Cambiar ROLES sin regenerar la clave: el mismo hash pasa a ver 9 tools
    # (publicador + analista) y el nivel se recalcula a 2 (rate limits).
    claves_db.cambiar_roles(id_a["id"], TENANT_A, ["publicador", "analista"])
    cliente_e = httpx2.AsyncClient(headers={"X-API-Key": CLAVE_A}, timeout=30)
    async with streamable_http_client(f"{BASE}/mcp", http_client=cliente_e) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            await sesion.initialize()
            herramientas = await sesion.list_tools()
            verificar("misma clave con rol analista: 9 herramientas sin regenerar",
                      len(herramientas.tools) == 9, str([t.name for t in herramientas.tools]))
    nivel_nuevo = q("SELECT nivel FROM mcp_api_keys WHERE id = %s", (id_a["id"],))[0]["nivel"]
    verificar("nivel derivado de los roles: 2", nivel_nuevo == 2, str(nivel_nuevo))

    # Limites por clave: override de 2 llamadas → la 3a validacion da 429
    q("UPDATE mcp_api_keys SET llamadas_hoy = 0, dia_uso = NULL WHERE id = %s", (id_a["id"],))
    claves_db.actualizar_limites(id_a["id"], TENANT_A, 2, None)
    r1 = claves_db.validar_clave(CLAVE_A, True)
    r2 = claves_db.validar_clave(CLAVE_A, True)
    r3 = claves_db.validar_clave(CLAVE_A, True)
    verificar("override 2 llamadas: 1a y 2a validacion pasan", r1[3] is None and r2[3] is None,
              f"{r1[3]} {r2[3]}")
    verificar("override 2 llamadas: 3a validacion = 429", r3[3] == 429, str(r3))

    # Reset a default (0): la validacion vuelve a pasar
    claves_db.actualizar_limites(id_a["id"], TENANT_A, 0, 0)
    r4 = claves_db.validar_clave(CLAVE_A, True)
    verificar("reset a default (0): validacion pasa", r4[3] is None, str(r4))

    # Hard delete: la fila desaparece y la clave queda invalida al instante
    filas_antes = q("SELECT COUNT(*) AS c FROM mcp_api_keys WHERE id = %s", (id_a["id"],))
    claves_db.revocar_clave(id_a["id"], TENANT_A)
    filas_despues = q("SELECT COUNT(*) AS c FROM mcp_api_keys WHERE id = %s", (id_a["id"],))
    verificar("hard delete: la fila ya no existe",
              filas_antes[0]["c"] == 1 and filas_despues[0]["c"] == 0)
    cliente_muerto = httpx2.AsyncClient(headers={"X-API-Key": CLAVE_A}, timeout=30)
    rechazada = False
    try:
        async with streamable_http_client(f"{BASE}/mcp", http_client=cliente_muerto) as (leer, escribir):
            async with ClientSession(leer, escribir) as sesion:
                await sesion.initialize()
    except Exception:
        rechazada = True
    verificar("clave eliminada: conexion rechazada (401)", rechazada)

    print("\n-- Clave del tenant B: solo ve SU catalogo --")
    cliente_b = httpx2.AsyncClient(headers={"X-API-Key": CLAVE_B}, timeout=30)
    async with streamable_http_client(
        f"{BASE}/mcp", http_client=cliente_b
    ) as (leer, escribir):
        async with ClientSession(leer, escribir) as sesion:
            await sesion.initialize()
            r = await llamar(sesion, "buscar_productos", {"consulta": ""})
            verificar("tenant B ve 1 producto (el suyo)",
                      r.get("total") == 1 and r["productos"][0]["nombre"] == "Producto De Otro Tenant",
                      json.dumps(r))


async def prueba_compuestos():
    """Fase 2 del conector: COMPUESTOS end-to-end.
    Crear platillos con receta (borrador → confirmar), venderlos por chat
    (cobrar_carrito descuenta los ingredientes, igual que el POS), receta por
    variación, ediciones y todos los casos de error."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    print("\n-- Fase 2: compuestos (crear con receta, vender, editar) --")

    # Ingredientes físicos, como los crearía el dueño en el gestor.
    pan = q(
        """INSERT INTO productos (producto, imagen, Estado, tenant_id, tipo_producto)
           VALUES ('Pan Bola','No hay foto','Activo',%s,'stock') RETURNING id""",
        (TENANT_A,))[0]["id"]
    q("INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, Estado, tenant_id, producto_id) "
      "VALUES ('LP1','Pan Bola',2,5,10,'2026-01-01','Activo',%s,%s)", (TENANT_A, pan))
    carne = q(
        """INSERT INTO productos (producto, imagen, Estado, tenant_id, tipo_producto, fraccionable)
           VALUES ('Carne Molida','No hay foto','Activo',%s,'stock',true) RETURNING id""",
        (TENANT_A,))[0]["id"]
    q("INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, Estado, tenant_id, producto_id) "
      "VALUES ('LP2','Carne Molida',40,80,1.5,'2026-01-01','Activo',%s,%s)", (TENANT_A, carne))
    queso = q(
        """INSERT INTO productos (producto, imagen, Estado, tenant_id, tipo_producto)
           VALUES ('Queso Amarillo','No hay foto','Activo',%s,'stock') RETURNING id""",
        (TENANT_A,))[0]["id"]
    q("INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote, Fecha_Entrada, Estado, tenant_id, producto_id) "
      "VALUES ('LP3','Queso Amarillo',7,15,20,'2026-01-01','Activo',%s,%s)", (TENANT_A, queso))

    global CLAVE_INV, CLAVE_CAJ
    CLAVE_INV = f"nezzura_mcp_{secrets.token_hex(24)}"
    CLAVE_CAJ = f"nezzura_mcp_{secrets.token_hex(24)}"
    for clave, roles in ((CLAVE_INV, "inventario"), (CLAVE_CAJ, "cajero")):
        q("INSERT INTO mcp_api_keys (tenant_id, nombre, key_hash, key_prefix, nivel, roles) "
          "VALUES (%s,'Fase 2',%s,%s,3,%s)",
          (TENANT_A, hashlib.sha256(clave.encode()).hexdigest(), clave[:12], roles))

    async with streamable_http_client(f"{BASE}/mcp",
                                      http_client=httpx2.AsyncClient(headers={"X-API-Key": CLAVE_INV}, timeout=30)) as (leer, escribir):
        async with ClientSession(leer, escribir) as s:
            await s.initialize()
            nombres = {t.name for t in (await s.list_tools()).tools}
            verificar("rol inventario: ve recetas y creacion",
                      {"proponer_producto", "proponer_receta", "ver_receta", "confirmar_edicion"} <= nombres,
                      str(nombres))

            # ── Crear compuesto CON receta (borrador → confirmar) ──
            r = await llamar(s, "proponer_producto", {
                "nombre": "Hamburguesa Simple", "precio_venta": 60, "costo": 22,
                "tipo": "compuesto", "descripcion": "Hamburguesa de la casa",
                "recetas": [{"material": "Pan Bola", "cantidad": 1},
                            {"material": "Carne Molida", "cantidad": 0.15},
                            {"material": "Queso Amarillo", "cantidad": 2}],
            })
            borrador = r.get("borrador_id", "")
            verificar("proponer compuesto: borrador", bool(borrador), json.dumps(r)[:300])
            verificar("proponer compuesto: resumen con receta",
                      "receta" in r.get("resumen", "") and "Pan Bola" in r.get("resumen", ""),
                      json.dumps(r))
            r = await llamar(s, "confirmar_producto", {"borrador_id": borrador})
            hamburguesa = r.get("producto_id")
            verificar("confirmar compuesto: ok", r.get("ok") is True and hamburguesa, json.dumps(r))
            verificar("confirmar compuesto: tipo + disponibilidad 10",
                      r.get("tipo") == "compuesto" and r.get("disponibilidad_estimada") == 10
                      and len(r.get("materiales") or []) == 3, json.dumps(r))
            verificar("confirmar compuesto: sin aviso (si trajo receta)", "aviso" not in r, json.dumps(r))
            fila = q("SELECT tipo_producto AS t, precio_servicio AS ps, fraccionable AS f "
                     "FROM productos WHERE id = %s", (hamburguesa,))
            verificar("compuesto en la base: tipo/precio_servicio",
                      fila[0]["t"] == "compuesto" and float(fila[0]["ps"]) == 60
                      and fila[0]["f"] is False, json.dumps(fila, default=str))
            n_rec = q("SELECT COUNT(*) AS c FROM producto_recetas WHERE producto_id = %s", (hamburguesa,))
            verificar("receta insertada (3 materiales)", n_rec[0]["c"] == 3, json.dumps(n_rec, default=str))

            # ── Errores de creación ──
            r = await llamar(s, "proponer_producto", {
                "nombre": "Hamburguesa Simple", "precio_venta": 60, "tipo": "compuesto",
                "recetas": [{"material": "Pan Bola", "cantidad": 1}]})
            verificar("compuesto duplicado: error (no upsert silencioso)",
                      "error" in r and "Ya existe" in r["error"], json.dumps(r))
            r = await llamar(s, "proponer_producto", {
                "nombre": "Nachos", "precio_venta": 45, "tipo": "compuesto",
                "recetas": [{"material": "Tortilla", "cantidad": 1}]})
            verificar("material inexistente: error claro",
                      "error" in r and "Tortilla" in r["error"], json.dumps(r))
            r = await llamar(s, "proponer_producto", {
                "nombre": "Combo Doble", "precio_venta": 100, "tipo": "compuesto",
                "recetas": [{"material": "Hamburguesa Simple", "cantidad": 2}]})
            verificar("receta con otro compuesto: error (no anidación)",
                      "error" in r and "compuesto" in r["error"], json.dumps(r))
            r = await llamar(s, "proponer_producto", {
                "nombre": "Refresco", "precio_venta": 0, "tipo": "compuesto"})
            verificar("compuesto sin precio: error", "error" in r and "precio" in r["error"])
            r = await llamar(s, "proponer_producto", {
                "nombre": "Combo Falso", "precio_venta": 50, "tipo": "stock",
                "recetas": [{"material": "Pan Bola", "cantidad": 1}]})
            verificar("recetas en un físico: error", "error" in r, json.dumps(r))

            # ── Compuesto SIN receta: se crea, pero avisando ──
            r = await llamar(s, "proponer_producto", {
                "nombre": "Agua de Horchata", "precio_venta": 25, "tipo": "compuesto"})
            borrador_h = r.get("borrador_id", "")
            verificar("compuesto sin receta: borrador", bool(borrador_h), json.dumps(r)[:200])
            r = await llamar(s, "confirmar_producto", {"borrador_id": borrador_h})
            horchata = r.get("producto_id")
            verificar("compuesto sin receta: ok con aviso",
                      r.get("ok") is True and "proponer_receta" in (r.get("aviso") or ""),
                      json.dumps(r))
            verificar("compuesto sin receta: disponibilidad None",
                      r.get("disponibilidad_estimada") is None, json.dumps(r))

            # Otro compuesto que se queda SIN receta (para probar que no se vende).
            r = await llamar(s, "proponer_producto", {
                "nombre": "Agua de Jamaica", "precio_venta": 25, "tipo": "compuesto"})
            r = await llamar(s, "confirmar_producto", {"borrador_id": r.get("borrador_id")})
            jamaica = r.get("producto_id")
            verificar("compuesto sin receta: ok con aviso",
                      r.get("ok") is True and "proponer_receta" in (r.get("aviso") or ""),
                      json.dumps(r))
            verificar("compuesto sin receta: disponibilidad None",
                      r.get("disponibilidad_estimada") is None, json.dumps(r))

            # ── ver_receta ──
            r = await llamar(s, "ver_receta", {"producto": "Agua de Horchata"})
            verificar("ver_receta sin receta: nota y materiales vacios",
                      r.get("materiales") == [] and "proponer_receta" in r.get("nota", ""),
                      json.dumps(r))
            r = await llamar(s, "ver_receta", {"producto": "Pan Bola"})
            verificar("ver_receta de un fisico: error claro",
                      "error" in r and "compuesto" in r["error"], json.dumps(r))
            r = await llamar(s, "ver_receta", {"producto": "Hamburguesa Simple"})
            verificar("ver_receta: materiales + disponibilidad",
                      len(r.get("materiales") or []) == 3 and r.get("disponibilidad_estimada") == 10,
                      json.dumps(r)[:300])

            # ── proponer_receta / confirmar_receta ──
            r = await llamar(s, "proponer_receta", {
                "producto": "Agua de Horchata",
                "cambios": [{"accion": "agregar", "material": "Hamburguesa Simple", "cantidad": 1}]})
            verificar("receta: no se anidan compuestos", "error" in r and "compuesto" in r["error"], json.dumps(r))
            r = await llamar(s, "proponer_receta", {
                "producto": "Agua de Horchata",
                "cambios": [{"accion": "cambiar", "receta_id": 999999, "cantidad": 2}]})
            verificar("receta: receta_id inventado da error con ids validos",
                      "error" in r and "Ids" in r["error"], json.dumps(r))
            r = await llamar(s, "proponer_receta", {
                "producto": "Agua de Horchata",
                "cambios": [{"accion": "agregar", "material": "Pan Bola", "cantidad": 1}]})
            borrador_r = r.get("borrador_id", "")
            verificar("proponer_receta: borrador", bool(borrador_r), json.dumps(r)[:300])
            r = await llamar(s, "confirmar_receta", {"borrador_id": borrador_r})
            verificar("confirmar_receta: ok", r.get("ok") is True, json.dumps(r))
            verificar("confirmar_receta: disponibilidad 10 (10 panes)",
                      r.get("disponibilidad_estimada") == 10, json.dumps(r))

            # ── Edición del compuesto: precio sí, costo no ──
            r = await llamar(s, "proponer_edicion", {
                "tipo": "producto", "entidad_id": hamburguesa, "cambios": {"precio_venta": 65}})
            borrador_e = r.get("borrador_id", "")
            verificar("editar precio del compuesto: borrador", bool(borrador_e), json.dumps(r)[:300])
            r = await llamar(s, "confirmar_edicion", {"borrador_id": borrador_e})
            verificar("confirmar_edicion compuesto: ok tipo compuesto",
                      r.get("ok") is True and r.get("tipo") == "compuesto", json.dumps(r))
            ps = q("SELECT precio_servicio AS ps FROM productos WHERE id = %s", (hamburguesa,))
            verificar("precio_servicio actualizado a 65", float(ps[0]["ps"]) == 65, json.dumps(ps, default=str))
            r = await llamar(s, "proponer_edicion", {
                "tipo": "producto", "entidad_id": hamburguesa, "cambios": {"costo": 20}})
            verificar("editar costo del compuesto: error (sale de la receta)",
                      "error" in r and "receta" in r["error"], json.dumps(r))

            # ── Variación con receta propia ──
            vid = q("INSERT INTO producto_variaciones (producto_id, nombre, precio, tenant_id) "
                    "VALUES (%s,'Doble',90,%s) RETURNING id", (hamburguesa, TENANT_A))[0]["id"]
            r = await llamar(s, "proponer_receta", {
                "producto": "Hamburguesa Simple",
                "cambios": [{"accion": "agregar", "material": "Carne Molida",
                             "cantidad": 0.3, "variacion": "Doble"}]})
            borrador_v = r.get("borrador_id", "")
            verificar("proponer_receta variación: borrador", bool(borrador_v), json.dumps(r)[:400])
            r = await llamar(s, "confirmar_receta", {"borrador_id": borrador_v})
            verificar("confirmar_receta variación: ok", r.get("ok") is True, json.dumps(r))
            r = await llamar(s, "proponer_receta", {
                "producto": "Hamburguesa Simple",
                "cambios": [{"accion": "agregar", "material": "Carne Molida",
                             "cantidad": 0.3, "variacion": "Triple"}]})
            verificar("variación inexistente en receta: error con opciones",
                      "error" in r and "Triple" in r["error"], json.dumps(r))

    async with streamable_http_client(f"{BASE}/mcp",
                                      http_client=httpx2.AsyncClient(headers={"X-API-Key": CLAVE_CAJ}, timeout=30)) as (leer, escribir):
        async with ClientSession(leer, escribir) as s:
            await s.initialize()
            nombres = {t.name for t in (await s.list_tools()).tools}
            verificar("rol cajero: ve ventas y gastos",
                      {"proponer_venta", "confirmar_venta"} <= nombres, str(nombres))

            # ── Vender el compuesto (2 unidades, precio de lista 65) ──
            r = await llamar(s, "proponer_venta", {
                "items": [{"producto": "Hamburguesa Simple", "cantidad": 2}]})
            renglon = (r.get("desglose") or [{}])[0]
            verificar("venta compuesto: desglose con tipo y disponibilidad",
                      renglon.get("tipo") == "compuesto"
                      and renglon.get("disponibilidad_estimada") == 10
                      and renglon.get("precio_unitario") == 65
                      and renglon.get("precio_de_lista") is True
                      and "stock" not in renglon, json.dumps(r)[:400])
            verificar("venta compuesto: total 130 y sin avisos",
                      r.get("total_estimado") == 130 and r.get("avisos") == [], json.dumps(r))
            borrador_v1 = r.get("borrador_id", "")
            r = await llamar(s, "confirmar_venta", {"borrador_id": borrador_v1})
            verificar("confirmar_venta compuesto: ok con ticket",
                      r.get("ok") is True and r.get("n_ticket"), json.dumps(r))
            v1 = q("SELECT tipo_producto AS t, total_venta AS tv, ganancia_bruta AS gb, consumo "
                   "FROM ventas WHERE tenant_id = %s AND Producto = 'Hamburguesa Simple' ORDER BY id LIMIT 1", (TENANT_A,))
            verificar("venta compuesto en la base: tipo y total",
                      v1[0]["t"] == "compuesto" and float(v1[0]["tv"]) == 130, json.dumps(v1, default=str))
            # Costo PEPS: pan 2×2 + carne 40×0.3 + queso 7×4 = 44 → (65−22)×2 = 86
            verificar("venta compuesto: ganancia con costo real (86)",
                      float(v1[0]["gb"]) == 86, json.dumps(v1, default=str))
            consumo = v1[0]["consumo"] or []
            mats = {c["material"]: float(c["cantidad"]) for c in consumo}
            verificar("consumo: pan 2, carne 0.3, queso 4",
                      mats == {"Pan Bola": 2.0, "Carne Molida": 0.3, "Queso Amarillo": 4.0},
                      json.dumps(consumo, default=str))
            stocks = {r_["p"]: float(r_["s"]) for r_ in q(
                "SELECT p.Producto AS p, SUM(l.Stock_Lote) AS s FROM lotes l "
                "JOIN productos p ON p.id = l.producto_id WHERE l.tenant_id = %s "
                "AND p.Producto IN ('Pan Bola','Carne Molida','Queso Amarillo') GROUP BY p.Producto",
                (TENANT_A,))}
            verificar("lotes descontados: pan 8, carne 1.2, queso 16",
                      stocks == {"Pan Bola": 8.0, "Carne Molida": 1.2, "Queso Amarillo": 16.0},
                      json.dumps(stocks))

            # ── Vender la variación 'Doble': usa SOLO la receta de la variación ──
            r = await llamar(s, "proponer_venta", {
                "items": [{"producto": "Hamburguesa Simple", "variacion": "Doble", "cantidad": 1}]})
            renglon = (r.get("desglose") or [{}])[0]
            verificar("venta variación: precio de la variación (90)",
                      renglon.get("precio_unitario") == 90 and renglon.get("variacion") == "Doble",
                      json.dumps(r)[:300])
            r = await llamar(s, "confirmar_venta", {"borrador_id": r.get("borrador_id")})
            verificar("confirmar variación: ok", r.get("ok") is True, json.dumps(r))
            v2 = q("SELECT consumo, ganancia_bruta AS gb FROM ventas WHERE tenant_id = %s "
                   "AND Producto = 'Hamburguesa Simple' AND variacion = 'Doble' ORDER BY id LIMIT 1", (TENANT_A,))
            mats2 = {c["material"]: float(c["cantidad"]) for c in (v2[0]["consumo"] or [])}
            verificar("consumo variación: la receta de la variación (carne 0.3)",
                      mats2 == {"Carne Molida": 0.3}, json.dumps(v2, default=str))
            verificar("ganancia variación: 90 - 12 = 78",
                      float(v2[0]["gb"]) == 78, json.dumps(v2, default=str))

            # ── Errores de venta de compuestos ──
            r = await llamar(s, "proponer_venta", {
                "items": [{"producto": "Hamburguesa Simple", "cantidad": 1, "id_lote": 123}]})
            verificar("id_lote en compuesto: error claro", "error" in r and "lote" in r["error"], json.dumps(r))
            r = await llamar(s, "proponer_venta", {
                "items": [{"producto": "Hamburguesa Simple", "variacion": "Triple", "cantidad": 1}]})
            verificar("variación inexistente al vender: error", "error" in r and "Triple" in r["error"], json.dumps(r))
            r = await llamar(s, "proponer_venta", {
                "items": [{"producto": "Agua de Jamaica", "cantidad": 1}]})
            verificar("compuesto sin receta: no se puede vender",
                      "error" in r and "proponer_receta" in r["error"], json.dumps(r))
            r = await llamar(s, "proponer_venta", {
                "items": [{"producto": "Corte de Cabello", "cantidad": 1}]})
            verificar("servicio: no se vende", "error" in r and "servicio" in r["error"], json.dumps(r))
            r = await llamar(s, "proponer_venta", {
                "items": [{"producto": "Hamburguesa Simple", "cantidad": 2.5}]})
            verificar("compuesto no fraccionable: cantidad entera", "error" in r, json.dumps(r))

            # ── Sobreventa de compuesto: aviso, no bloqueo (igual que stock) ──
            # Quedan: pan 7, carne 0.9, queso 14 → la receta alcanza para 6.
            r = await llamar(s, "proponer_venta", {
                "items": [{"producto": "Hamburguesa Simple", "cantidad": 10}]})
            renglon = (r.get("desglose") or [{}])[0]
            verificar("sobreventa: disponibilidad 6 en el desglose",
                      renglon.get("disponibilidad_estimada") == 6, json.dumps(r)[:300])
            verificar("sobreventa: aviso de receta", any("alcanza" in a for a in r.get("avisos", [])),
                      json.dumps(r.get("avisos")))
            r = await llamar(s, "confirmar_venta", {"borrador_id": r.get("borrador_id")})
            verificar("sobreventa confirmada: se registra", r.get("ok") is True, json.dumps(r))
            neg = q("SELECT p.Producto AS p, SUM(l.Stock_Lote) AS s FROM lotes l "
                    "JOIN productos p ON p.id = l.producto_id WHERE l.tenant_id = %s "
                    "AND p.Producto IN ('Pan Bola','Carne Molida') GROUP BY p.Producto", (TENANT_A,))
            mapa_neg = {n["p"]: float(n["s"]) for n in neg}
            verificar("stock negativo tras vender de más: pan -2, carne -0.6",
                      mapa_neg == {"Pan Bola": -2.0, "Carne Molida": -0.6}, json.dumps(mapa_neg))

            # ── Anti-flojera: 'hamburguesa' como venta libre avisa que existe ──
            r = await llamar(s, "proponer_venta", {
                "items": [{"venta_libre": True, "descripcion": "Hamburguesa",
                           "precio_real": 50, "motivo": "se agotó y se vendió de mostrador"}]})
            verificar("venta libre 'hamburguesa': aviso anti-flojera",
                      any("Hamburguesa Simple" in a for a in r.get("avisos", [])),
                      json.dumps(r.get("avisos")))

            # ── Regresión: un físico se sigue vendiendo igual ──
            r = await llamar(s, "proponer_venta", {
                "items": [{"producto": "Camisa Polo Azul", "cantidad": 1}]})
            renglon = (r.get("desglose") or [{}])[0]
            verificar("venta física: tipo stock + stock en el desglose",
                      renglon.get("tipo") == "stock" and renglon.get("stock") == 12
                      and renglon.get("precio_unitario") == 99.9, json.dumps(r)[:300])


async def prueba_variaciones():
    """Fase 3 del conector: VARIACIONES end-to-end (tallas/tamaños).
    El caso del vivero: una begonia chica y una grande con precio y stock
    propios. Cubre leer por talla, vender la talla correcta, reponer una talla,
    editar su precio sin aplanar la otra y crear una presentación nueva."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    print("\n-- Fase 3: variaciones (leer, vender, reponer, editar) --")

    begonia = q(
        """INSERT INTO productos (producto, Descripcion, imagen, Estado, tenant_id, tipo_producto)
           VALUES ('Begonia Rosa','Begonia de maceta','No hay foto','Activo',%s,'stock')
           RETURNING id""", (TENANT_A,))[0]["id"]
    chica = q("INSERT INTO producto_variaciones (producto_id, nombre, precio, tenant_id) "
              "VALUES (%s,'Chica',120,%s) RETURNING id", (begonia, TENANT_A))[0]["id"]
    grande = q("INSERT INTO producto_variaciones (producto_id, nombre, precio, tenant_id) "
               "VALUES (%s,'Grande',220,%s) RETURNING id", (begonia, TENANT_A))[0]["id"]
    q("""INSERT INTO lotes (ID_Lote, Producto, Costo, Precio_Venta, Stock_Lote,
                             Fecha_Entrada, Estado, tenant_id, producto_id, variacion_id)
         VALUES ('LV1','Begonia Rosa',60,120,7,'2026-01-01','Activo',%s,%s,%s),
                ('LV2','Begonia Rosa',90,220,3,'2026-01-01','Activo',%s,%s,%s)""",
      (TENANT_A, begonia, chica, TENANT_A, begonia, grande))

    # proponer_entrada vive en el rol Compras (no en Inventario).
    clave_compras = f"nezzura_mcp_{secrets.token_hex(24)}"
    q("INSERT INTO mcp_api_keys (tenant_id, nombre, key_hash, key_prefix, nivel, roles) "
      "VALUES (%s,'Fase 3',%s,%s,3,'compras')",
      (TENANT_A, hashlib.sha256(clave_compras.encode()).hexdigest(), clave_compras[:12]))

    def stocks_por_talla() -> dict:
        filas = q(
            "SELECT v.nombre, COALESCE(SUM(l.Stock_Lote), 0) AS s "
            "FROM producto_variaciones v "
            "LEFT JOIN lotes l ON l.variacion_id = v.id AND l.Estado = 'Activo' "
            "WHERE v.producto_id = %s GROUP BY v.nombre", (begonia,))
        return {f["nombre"]: float(f["s"]) for f in filas}

    # ── Inventario: lecturas, edición de precio y variación nueva ──
    async with streamable_http_client(
        f"{BASE}/mcp",
        http_client=httpx2.AsyncClient(headers={"X-API-Key": CLAVE_INV}, timeout=30),
    ) as (leer, escribir):
        async with ClientSession(leer, escribir) as s:
            await s.initialize()

            r = await llamar(s, "buscar_productos", {"consulta": "begonia"})
            p = (r.get("productos") or [{}])[0]
            verificar("buscar: tiene_variaciones + precio_desde/hasta (120/220)",
                      p.get("tiene_variaciones") is True
                      and p.get("precio_desde") == 120 and p.get("precio_hasta") == 220,
                      json.dumps(p))
            verificar("buscar: stock por talla (Chica 7, Grande 3)",
                      {v["nombre"]: v["stock"] for v in p.get("variaciones", [])}
                      == {"Chica": 7, "Grande": 3}, json.dumps(p.get("variaciones")))
            verificar("buscar: stock_disponible es la suma (10)",
                      p.get("stock_disponible") == 10, json.dumps(p))

            r = await llamar(s, "stock_actual", {"producto_id": begonia})
            p = (r.get("productos") or [{}])[0]
            verificar("stock_actual: variaciones con su stock propio",
                      {v["nombre"]: v["stock"] for v in p.get("variaciones", [])}
                      == {"Chica": 7, "Grande": 3}, json.dumps(p))

            # Editar el precio de UNA talla: la otra no se toca y los lotes
            # conservan su precio (no se aplana nada).
            r = await llamar(s, "proponer_edicion", {
                "tipo": "producto", "entidad_id": begonia,
                "cambios": {"variaciones": [{"nombre": "Grande", "precio": 250}]}})
            borrador = r.get("borrador_id", "")
            verificar("editar variación: diff precio 220 -> 250",
                      bool(borrador) and "250" in json.dumps(r.get("cambios_propuestos")),
                      json.dumps(r)[:400])
            r = await llamar(s, "confirmar_edicion", {"borrador_id": borrador})
            verificar("confirmar edición de variación: ok", r.get("ok") is True, json.dumps(r))
            precios = q("SELECT nombre, precio FROM producto_variaciones "
                        "WHERE producto_id = %s ORDER BY nombre", (begonia,))
            verificar("solo la talla editada cambió (Chica 120, Grande 250)",
                      {f["nombre"]: float(f["precio"]) for f in precios}
                      == {"Chica": 120.0, "Grande": 250.0}, json.dumps(precios, default=str))
            lotes = q("SELECT Precio_Venta FROM lotes WHERE producto_id = %s "
                      "ORDER BY variacion_id", (begonia,))
            verificar("los lotes NO se aplanaron (120 y 220)",
                      sorted(float(f["precio_venta"]) for f in lotes) == [120.0, 220.0],
                      json.dumps(lotes, default=str))

            r = await llamar(s, "proponer_edicion", {
                "tipo": "producto", "entidad_id": begonia,
                "cambios": {"precio_venta": 300}})
            verificar("precio_venta global en producto con variaciones: rechazado",
                      "error" in r and "variaciones" in r["error"], json.dumps(r))

            # Presentación NUEVA en un producto que ya existe.
            r = await llamar(s, "proponer_edicion", {
                "tipo": "producto", "entidad_id": begonia,
                "cambios": {"variaciones_nuevas": [{
                    "nombre": "Mediana", "precio": 180,
                    "stock_inicial": 4, "costo": 80}]}})
            borrador = r.get("borrador_id", "")
            verificar("variación nueva: borrador con diff legible",
                      bool(borrador) and "Mediana" in json.dumps(r.get("cambios_propuestos")),
                      json.dumps(r)[:400])
            r = await llamar(s, "confirmar_edicion", {"borrador_id": borrador})
            verificar("confirmar variación nueva: ok + creada",
                      r.get("ok") is True and r.get("variaciones_creadas") == ["Mediana"],
                      json.dumps(r))
            verificar("variación nueva en la base con su lote (4 u.)",
                      stocks_por_talla() == {"Chica": 7.0, "Grande": 3.0, "Mediana": 4.0},
                      json.dumps(stocks_por_talla()))

    # ── Cajero: vender la talla correcta ──
    async with streamable_http_client(
        f"{BASE}/mcp",
        http_client=httpx2.AsyncClient(headers={"X-API-Key": CLAVE_CAJ}, timeout=30),
    ) as (leer, escribir):
        async with ClientSession(leer, escribir) as c:
            await c.initialize()

            r = await llamar(c, "proponer_venta", {
                "items": [{"producto": "Begonia Rosa", "cantidad": 1}]})
            verificar("vender sin variación: error con opciones",
                      "error" in r and "Chica" in r["error"] and "Grande" in r["error"],
                      json.dumps(r))

            r = await llamar(c, "proponer_venta", {
                "items": [{"producto": "Begonia Rosa", "variacion": "Enana", "cantidad": 1}]})
            verificar("variación inexistente al vender: error con opciones",
                      "error" in r and "Enana" in r["error"], json.dumps(r))

            r = await llamar(c, "proponer_venta", {
                "items": [{"producto": "Begonia Rosa", "variacion": "Mediana", "cantidad": 2}]})
            renglon = (r.get("desglose") or [{}])[0]
            verificar("vender Mediana: precio 180, stock de ESA talla (4)",
                      renglon.get("precio_unitario") == 180
                      and renglon.get("stock") == 4
                      and renglon.get("variacion") == "Mediana",
                      json.dumps(r)[:400])
            verificar("venta por variación: precio de lista (no del agente)",
                      renglon.get("precio_de_lista") is True, json.dumps(renglon))
            borrador = r.get("borrador_id", "")
            r = await llamar(c, "confirmar_venta", {"borrador_id": borrador})
            verificar("confirmar venta por variación: ok", r.get("ok") is True, json.dumps(r))
            verificar("stock baja SOLO en Mediana (2) y las demás intactas",
                      stocks_por_talla() == {"Chica": 7.0, "Grande": 3.0, "Mediana": 2.0},
                      json.dumps(stocks_por_talla()))
            venta = q("SELECT variacion, Precio_Real FROM ventas WHERE producto_id = %s "
                      "AND variacion = 'Mediana' ORDER BY id DESC LIMIT 1", (begonia,))
            verificar("la venta guarda la variación y el precio real",
                      bool(venta) and venta[0]["variacion"] == "Mediana"
                      and float(venta[0]["precio_real"]) == 180,
                      json.dumps(venta, default=str))
            base = q("SELECT COUNT(*) AS c FROM lotes WHERE producto_id = %s "
                     "AND variacion_id IS NULL", (begonia,))
            verificar("no se creó un lote base negativo", base and base[0]["c"] == 0,
                      json.dumps(base, default=str))

            # Regresión: los productos SIN variaciones se siguen vendiendo.
            r = await llamar(c, "proponer_venta", {
                "items": [{"producto": "Camisa Polo Azul", "cantidad": 1}]})
            renglon = (r.get("desglose") or [{}])[0]
            verificar("producto sin variaciones: sigue vendiéndose sin 'variacion'",
                      "error" not in r and renglon.get("variacion") is None,
                      json.dumps(r)[:300])

    # ── Compras: reponer una talla ──
    async with streamable_http_client(
        f"{BASE}/mcp",
        http_client=httpx2.AsyncClient(headers={"X-API-Key": clave_compras}, timeout=30),
    ) as (leer, escribir):
        async with ClientSession(leer, escribir) as c:
            await c.initialize()

            r = await llamar(c, "proponer_entrada", {
                "producto": "Begonia Rosa", "cantidad": 5, "costo": 70})
            verificar("reponer sin variación: error pidiendo cuál",
                      "error" in r and "variaciones" in r["error"].lower(), json.dumps(r))

            r = await llamar(c, "proponer_entrada", {
                "producto": "Begonia Rosa", "cantidad": 5, "costo": 70,
                "variacion": "Chica"})
            borrador = r.get("borrador_id", "")
            verificar("reponer Chica: borrador", bool(borrador), json.dumps(r)[:300])
            r = await llamar(c, "confirmar_entrada", {"borrador_id": borrador})
            verificar("confirmar reposición de una talla: ok", r.get("ok") is True,
                      json.dumps(r))
            verificar("stock de Chica sube a 12 (y las demás siguen igual)",
                      stocks_por_talla() == {"Chica": 12.0, "Grande": 3.0, "Mediana": 2.0},
                      json.dumps(stocks_por_talla()))

    # ── Analista: ranking por variación ──
    async with streamable_http_client(
        f"{BASE}/mcp",
        http_client=httpx2.AsyncClient(headers={"X-API-Key": CLAVE_A2}, timeout=30),
    ) as (leer, escribir):
        async with ClientSession(leer, escribir) as c:
            await c.initialize()

            r = await llamar(c, "top_vendidos", {"periodo": "todo"})
            top = r.get("top") or []
            begonias = [x for x in top if x.get("producto") == "Begonia Rosa"]
            verificar("top_vendidos: fila propia por variación (Mediana, 2 u.)",
                      len(begonias) == 1 and begonias[0].get("variacion") == "Mediana"
                      and begonias[0].get("unidades") == 2,
                      json.dumps(begonias))
            verificar("top_vendidos: stock de la talla vendida (2), no el total (17)",
                      begonias and begonias[0].get("stock_actual") == 2,
                      json.dumps(begonias))

            r = await llamar(c, "ventas_recientes", {"periodo": "todo", "limite": 5})
            variaciones = [v.get("variacion") for v in r.get("ventas", [])]
            verificar("ventas_recientes expone la variación", "Mediana" in variaciones,
                      json.dumps(variaciones))


def prueba_http():
    print("\n-- HTTP plano --")
    r = httpx.get(f"{BASE}/")
    verificar("GET / (ruta propia intacta tras el Mount)", r.status_code == 200 and "funcionando" in r.text)
    r = httpx.get(f"{BASE}/docs")
    verificar("GET /docs intacto", r.status_code == 200)

    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "t", "version": "1"}}}
    r = httpx.post(f"{BASE}/mcp", json=initialize)
    verificar("POST /mcp sin clave -> 401 JSON", r.status_code == 401 and "error" in r.json(),
              f"{r.status_code} {r.text[:150]}")

    r = httpx.post(f"{BASE}/mcp", headers={"X-API-Key": "nezzura_mcp_invalida"}, json=initialize)
    verificar("POST /mcp clave invalida -> 401", r.status_code == 401, f"{r.status_code}")

    # El router de gestion exige JWT de Supabase (el agente no puede tocarlo)
    r = httpx.post(f"{BASE}/mcp-conexion/claves", json={"nombre": "x"})
    verificar("REST claves sin JWT -> 401", r.status_code == 401, f"{r.status_code}")
    r = httpx.get(f"{BASE}/mcp-conexion/claves")
    verificar("REST listar sin JWT -> 401", r.status_code == 401, f"{r.status_code}")


async def main():
    camisa, llavero, sin_foto, mymelody, pokemon = seed()
    print(f"Seed OK -- camisa={camisa} llavero={llavero} sin_foto={sin_foto}")
    await prueba_mcp(camisa, llavero, sin_foto, mymelody, pokemon)
    prueba_http()
    await prueba_compuestos()
    await prueba_variaciones()

    print("\n" + "=" * 60)
    if FALLOS:
        print(f"FALLOS ({len(FALLOS)}): {FALLOS}")
        sys.exit(1)
    print("TODAS LAS PRUEBAS PASARON")


if __name__ == "__main__":
    asyncio.run(main())



