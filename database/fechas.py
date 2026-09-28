# ==============================================================================
# backend/database/fechas.py
# Normalización de fechas TEXT de la base de datos a zona horaria del tenant.
#
# ── ¿Por qué existe este módulo? ──
# Las columnas fecha/fecha_entrada son TEXT con formatos MIXTOS en producción:
#   1. ISO con offset:  "2026-09-26T21:01:11.214752-05:00"  (código nuevo,
#      generado con datetime.now(_TZ) en lotes.py / ventas.py)
#   2. ISO con 'Z':     "2026-09-22T12:00:00.000Z"          (frontend ISOString)
#   3. Naive legacy:    "2026-06-22 00:22:44.237222"        (registros antiguos;
#      son instantes UTC guardados sin offset)
#   4. Solo fecha:      "2026-09-22"                        (gastos del motor de
#      programados: se interpretan como FECHA LOCAL tal cual, no como UTC)
#
# El bug que corrige este módulo: usar `fecha::date` directamente interpreta
# los registros naive (UTC) como si fueran hora local, desplazando las ventas
# nocturnas al día siguiente (medido: 9.6% de las ventas del tenant Kali
# quedaban en el día equivocado).
#
# Solución validada 2026-09-28: convertir todo a timestamptz respetando cada
# formato y luego restar la zona horaria del tenant (tenants.zona_horaria).
# Cobertura: 100% de las fechas de la base (0 desconocidas). Costo: ~77ms por
# cada 333 registros.
# ==============================================================================

# ── Expresión SQL: convierte el TEXT `fecha` a timestamptz (instante UTC) ──
# Requisitos de cada rama:
#   - Con offset explícito (ISO-T o sufijo Z/±HH:MM): el cast directo a
#     timestamptz es correcto porque el offset viene embebido.
#   - Solo fecha "YYYY-MM-DD" (sin hora): NO es un instante UTC; es la fecha
#     local del negocio. Se inserta como mediodía local del tenant para que
#     ningún ajuste de zona la mueva de día. Recibe %s con la zona del tenant.
#   - Naive legacy "YYYY-MM-DD HH:MM:SS": son instantes UTC guardados sin
#     offset (así los generaba el backend antiguo), por eso se declara UTC.
FECHA_UTC_SQL = """
CASE
  WHEN fecha ~ '^\\d{4}-\\d{2}-\\d{2}T' THEN fecha::timestamptz
  WHEN fecha ~ '(Z|[-+]\\d{2}:?\\d{2})$' THEN fecha::timestamptz
  WHEN fecha ~ '^\\d{4}-\\d{2}-\\d{2}$'
       THEN (fecha || ' 12:00:00')::timestamp AT TIME ZONE %(zona)s
  ELSE fecha::timestamp AT TIME ZONE 'UTC'
END
"""


def sql_fecha_local(aliased: bool = False) -> str:
    """
    Devuelve la expresión SQL que produce la FECHA-HORA LOCAL del tenant.
    Uso: interpola en queries que hagan JOIN con tenants t y luego
    `... {sql_fecha_local()} ...` pasando params={'zona': zona_tenant}.
    - aliased=False → requiere JOIN a tenants con alias `t` en la query.
    - aliased=True  → usa el alias `tz_` (tabla tenants renombrada).
    La zona se inyecta como parámetro %(zona)s → NUNCA concatenar texto
    del usuario aquí (previene inyección SQL).
    """
    tabla = "tz_" if aliased else "t"
    return f"({FECHA_UTC_SQL}) AT TIME ZONE {tabla}.zona_horaria"


def params_zona(zona_horaria: str) -> dict:
    """Empaqueta la zona horaria del tenant como parámetro con nombre."""
    return {"zona": zona_horaria}
