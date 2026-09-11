# Nezzura Backend

Backend del sistema de gestion de inventarios y punto de venta Nezzura. Es una API FastAPI sobre PostgreSQL (Supabase) que expone los recursos de inventario, ventas, mesas, gastos, clientes, terminales/turnos, estadisticas y catalogo publico. Cada negocio (tenant) opera sobre su propia base de datos.

## Stack

- FastAPI + Uvicorn
- PostgreSQL via psycopg2 (consultas SQL directas, sin ORM)
- Autenticacion con JWT de Supabase (verificacion via PyJWKClient)
- Cloudinary para el manejo de imagenes de productos
- Openpyxl para exportaciones a Excel

## Requisitos

- Python 3.11 o superior
- Una base de datos PostgreSQL (Supabase u otro proveedor)
- Credenciales de Supabase para la validacion de tokens

## Configuracion

Crea un archivo `.env` en la raiz con estas variables:

```env
DATABASE_URL=postgresql://usuario:password@host:puerto/base_de_datos
SUPABASE_URL=https://<tu-proyecto>.supabase.co
SUPABASE_ANON_KEY=<clave anon de supabase>
FRONTEND_URL=https://<url del frontend>   # para CORS; por defecto https://nezzura-digital.vercel.app
```

Nota: el backend tambien acepta `NEXT_PUBLIC_SUPABASE_URL` y `NEXT_PUBLIC_SUPABASE_ANON_KEY` como nombres alternativos de las claves de Supabase.

## Ejecucion

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows (Linux/Mac: source .venv/bin/activate)
pip install -r requirements.txt
uvicorn main:app --reload
```

La API queda disponible en `http://127.0.0.1:8000` con la documentacion de endpoints en `/docs`.

## Base de datos y migraciones

- El esquema inicial lo crea `inicializar_db` (database/conexion.py) al arrancar la aplicacion.
- Cambios incrementales del esquema se registran como archivos numerados en `migrations/` (por ejemplo `032_ordenes_de_venta.sql`). Se agrupan por dominio: inventario, ventas, mesas, clientes, puntos, catalogo, terminales/turnos.

## Estructura

```
main.py            App FastAPI: CORS, middlewares, registro de routers
dependencies.py    Autenticacion: validacion de JWT de Supabase por tenant
database/          Acceso a datos por dominio (productos, lotes, ventas, mesas, gastos, clientes, puntos, stats, etc.)
routers/           Endpoints por modulo (inventario, ventas, turnos, stats, mesas, clientes, catalogo_gestion, publico, exportacion)
schemas/           Modelos Pydantic de entrada/salida
migrations/        SQL numerado de cambios incrementales
scripts/           Utilidades operativas (auditoria de imagenes en Cloudinary)
```

## Seguridad

- Cada request autenticado valida el JWT contra Supabase y se resuelve el tenant a partir del token.
- Las credenciales viven exclusivamente en variables de entorno; no se commitean.
- El catalogo publico de un negocio se sirve por `business_name`/slug sin exponer credenciales de la sesion.
