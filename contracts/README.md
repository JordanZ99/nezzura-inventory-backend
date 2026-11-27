# Contratos de Entidades — Goyangi / Nezzura Digital

> **Para agentes de IA:** Este directorio es tu **fuente de verdad** sobre los campos de cada entidad del sistema. Lee los contratos aquí antes de inferir la estructura de ninguna tabla o endpoint leyendo código SQL.

## ¿Qué hay aquí?

Cada archivo `.contract.json` documenta exactamente qué campos existen, su tipo, si son requeridos y si son de lectura o escritura:

| Archivo | Dominio |
|---|---|
| [inventario.contract.json](./inventario.contract.json) | Productos, Lotes, Variaciones, Recetas y Categorías |
| [ventas.contract.json](./ventas.contract.json) | Ventas, Órdenes/Tickets y Cobros (carrito, pagos mixtos) |
| [gastos.contract.json](./gastos.contract.json) | Gastos directos, Categorías de Gasto y Gastos Programados |
| [cobros.contract.json](./cobros.contract.json) | Cobros del giro 'servicios': Cuotas mensuales, Cobros (extras) y Abonos |
| [actividad.contract.json](./actividad.contract.json) | Historial completo (migración 062): registro de actividad NO-stock — ediciones de producto, CRM y cobros |
| [catalogo_terminales_turnos.contract.json](./catalogo_terminales_turnos.contract.json) | Catálogo público, Posts automáticos, Terminales bancarias y Turnos de caja |
| [ordenes.contract.json](./ordenes.contract.json) | Órdenes/tickets de venta y su ciclo de vida |
| [planes_clientes.contract.json](./planes_clientes.contract.json) | Planes de suscripción y clientes del giro 'servicios' |

---

## Reglas de actualización

1. **Cuando añadas un campo en Pydantic** (`backend/schemas/*.py`), actualiza el `.contract.json` correspondiente.
2. **Cuando añadas una columna SQL** en `backend/database/conexion.py`, actualiza el contrato si ese campo es expuesto por la API.
3. Los contratos **no** reemplazan a Pydantic — son solo documentación legible por IAs. La validación real la hace FastAPI en runtime.

---

## Convenciones de los contratos

- `campos_lectura`: Campos que devuelven los endpoints GET.
- `campos_escritura_POST/PUT/PATCH`: Campos aceptados en cada método HTTP.
- `"required": true`: Campo obligatorio en ese contexto.
- `"type": ["string", "null"]`: Campo opcional (puede llegar como `null` desde el frontend).
- `tabla_sql`: Nombre exacto de la tabla en PostgreSQL.
- `tenant_id`: Nunca se envía en los requests del frontend — siempre se extrae del JWT en el backend.

---

## Estructura del proyecto

Cada carpeta de primer nivel es un repositorio git autónomo; **no existe un repo padre**.
Este directorio vive dentro del repo del backend.

```text
Goyangi Gestor de inventarios/     ← workspace local (NO es repo)
├── backend/                       ← repo: nezzura-inventory-backend
│   ├── contracts/                 ← ★ TÚ ESTÁS AQUÍ — fuente de verdad para IAs
│   │   ├── README.md
│   │   ├── inventario.contract.json
│   │   ├── ventas.contract.json
│   │   ├── gastos.contract.json
│   │   ├── cobros.contract.json
│   │   ├── actividad.contract.json
│   │   ├── catalogo_terminales_turnos.contract.json
│   │   ├── ordenes.contract.json
│   │   └── planes_clientes.contract.json
│   ├── schemas/                   ← Modelos Pydantic por dominio (validación en runtime)
│   │   ├── inventario.py
│   │   ├── ventas.py
│   │   ├── gastos.py
│   │   ├── gastos_programados.py
│   │   ├── catalogo.py
│   │   ├── terminales.py
│   │   ├── turnos.py
│   │   └── __init__.py
│   ├── routers/                   ← Solo ruteo HTTP e inyección de dependencias
│   ├── database/                  ← Solo lógica de persistencia SQL (psycopg2)
│   ├── dependencies.py            ← Validación JWT de Supabase → tenant_id
│   └── main.py
│
└── frontend/                      ← repo: nezzura-inventory-frontend
    └── src/
        ├── types/                 ← Tipos TypeScript por dominio
        │   ├── productos.ts
        │   ├── ventas.ts
        │   ├── gastos.ts
        │   ├── catalogo.ts
        │   └── index.ts
        └── lib/
            └── api/               ← Clientes API por dominio
                ├── client.ts (fetch base + auth)
                ├── productos.ts
                ├── ventas.ts
                ├── gastos.ts
                ├── catalogo.ts
                ├── turnos.ts
                ├── terminales.ts
                ├── stats.ts
                └── index.ts
```
