# AGENTS.md — backend (Nezzura Inventory Backend)

Este directorio **es un repositorio git autónomo**:

- Remote: `https://github.com/JordanZ99/nezzura-inventory-backend.git`
- Rama de trabajo y producción: `main`
- **No existe un repo padre.** El workspace que contiene esta carpeta no es un repositorio:
  nunca ejecutes `git init` ni commits desde fuera de `backend/`.
- Antes de empezar, lee `../AGENTS.md` (contexto general del proyecto).

## Estructura

```text
main.py             FastAPI: CORS, middlewares, routers, auto-migración al arrancar
dependencies.py     Validación del JWT de Supabase → tenant_id
schemas/            Modelos Pydantic (validación de entrada/salida)
routers/            Solo HTTP: rutas, parámetros y Depends()
database/           SQL con psycopg2 y lógica de negocio (sin ORM)
muse/               Conector MCP: server.py (tools), roles.py (roles/briefs), auth.py
migrations/         SQL incremental numerado (hoy 082); lo ejecuta conexion.py
contracts/          Fuente de verdad de campos por entidad (.contract.json)
docs/historico/     Registros de fases ya cerradas (no son estado actual)
tests/e2e/          Suite end-to-end del conector MCP
scripts/            Utilidades operativas
```

## Reglas de código

1. `routers/` no contiene lógica de negocio ni SQL: delega en `database/`.
2. `database/` no conoce HTTP: recibe y devuelve datos.
3. **Toda** consulta sobre tablas de negocio filtra por `tenant_id`. Nunca lo aceptes del cliente:
   sale del JWT.
4. SQL siempre parametrizado con `%s`.
5. Un cambio de esquema = un archivo nuevo en `migrations/` con el siguiente número.
   No edites migraciones ya aplicadas.
6. Si cambias un modelo Pydantic, actualiza el `.contract.json` correspondiente en `contracts/`.
7. Docstrings y comentarios explican el **porqué**; los mensajes de error que ve el usuario final
   van en español claro.

## Comandos

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload            # http://127.0.0.1:8000/docs
python -m compileall .               # verificación rápida

# Suite e2e del MCP (requiere Docker + Postgres en 54399 y uvicorn en 8001)
$env:PYTHONIOENCODING = "utf-8"
python tests/e2e/test_mcp_end_to_end.py
```

## Commits

En español, detallados, directamente en `main`:

```text
[ÁREA] Título descriptivo en presente

- Qué problema resuelve o qué funcionalidad implementa.
- Descripción técnica de los cambios.
- Archivos clave modificados.
- Comportamiento esperado.
```

## Contexto del dominio

- Tipos de producto: `stock` (lotes PEPS), `servicio` y `compuesto` (consume receta).
- Con variaciones, **todo el stock vive en lotes con `variacion_id`**; no hay lote base.
- El precio de venta de una variación sale de `producto_variaciones.precio`; editar ese precio
  no reescribe el `Precio_Venta` de los lotes.
- El conector MCP mantiene el patrón **borrador → confirmar** con cuota de escrituras y HITL.
  No agregues tools ni permisos nuevos sin pedirlo.
