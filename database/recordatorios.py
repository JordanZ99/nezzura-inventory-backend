# ==============================================================================
# backend/database/recordatorios.py
# Recordatorios automáticos de cobros (mig 080) — best-effort, jamás rompen.
#
# Cadencia por días de atraso del VENCIMIENTO (mismo modelo que Recaudo/Kobsi):
#   -1  → vence mañana
#    0  → vence hoy
#    1, 7, 15, 30 → vencido hace 1 / 7 / 15 / 30 días
# A lo más UN email por cobro y por día (cobros.ultimo_recordatorio). Con
# promesa de pago vigente (hoy <= promesa_fecha) el recordatorio SE PAUSA:
# el cliente prometió pagar, no hay que perseguirlo.
#
# El envío usa SMTP (database/notificaciones.py): sin SMTP_* configurado,
# enviar_email devuelve False en silencio y nada se rompe — los recordatorios
# quedan pendientes para el día siguiente.
#
# Disparo: scripts/enviar_recordatorios.py (Cron Job de Render diario).
# ==============================================================================

from datetime import date, timedelta

from database.conexion import execute, query
from database.notificaciones import enviar_email
from database.tenants import _cfg_moratorios

# Días de atraso (>= 0) que disparan un email; 0 = vence HOY.
CADENCIA = (0, 1, 7, 15, 30)
DIAS_ANTES = 1  # aviso de "vence mañana"


def _asunto_y_cuerpo(fila: dict, empresa: str) -> tuple[str, str]:
    hoy = date.today()
    venc = fila["vencimiento"]
    dias = (hoy - venc).days
    if dias == 0:
        asunto = f"Vence hoy tu pago de {fila['concepto']}"
        cuerpo = (f"Hola {fila['cliente_nombre']}:\n\n"
                  f"Hoy vence tu pago de {fila['concepto']} por {fila['saldo']:.2f}.\n\n"
                  f"Cuando puedas, me avisas. ¡Gracias!\n— {empresa}")
    elif dias < 0:
        asunto = f"Mañana vence tu pago de {fila['concepto']}"
        cuerpo = (f"Hola {fila['cliente_nombre']}:\n\n"
                  f"Un recordatorio amable: mañana {venc.isoformat()} vence tu pago "
                  f"de {fila['concepto']} por {fila['saldo']:.2f}.\n\n¡Saludos!\n— {empresa}")
    else:
        mora = float(fila.get("moratorios") or 0)
        total = float(fila.get("total_con_moratorios") or fila["saldo"])
        mora_txt = f" + {mora:.2f} de moratorios" if mora > 0 else ""
        asunto = f"Recordatorio: tu pago de {fila['concepto']} está pendiente ({dias} días)"
        cuerpo = (f"Hola {fila['cliente_nombre']}:\n\n"
                  f"Tu pago de {fila['concepto']} venció el {venc.isoformat()} "
                  f"({dias} día(s) atrás). Saldo: {fila['saldo']:.2f}{mora_txt} = {total:.2f}.\n\n"
                  f"¿Me confirmas cuándo me lo puedes pasar? 🙏\n— {empresa}")
    return asunto, cuerpo


def enviar_recordatorios_del_tenant(tenant_id: str) -> dict:
    """Recorre los cobros pendientes del tenant y manda los emails que tocan
    hoy (cadencia arriba). Idempotente por día: ultimo_recordatorio evita
    dobles envíos si el cron corre dos veces. Devuelve el conteo."""
    hoy = date.today()
    empresa_filas = query("SELECT empresa FROM tenants WHERE id = %s", (tenant_id,))
    empresa = (empresa_filas[0].get("empresa") or "Tu negocio") if empresa_filas else "Tu negocio"

    from database.cobros import listar_cobros  # import tardío: evita ciclo
    cobros = listar_cobros(tenant_id, solo_pendientes=True)

    enviados = 0
    for c in cobros:
        venc = c.get("vencimiento")
        if not hasattr(venc, "isoformat"):
            continue
        dias = (hoy - venc).days
        if dias not in CADENCIA and dias != -DIAS_ANTES:
            continue
        # Promesa vigente: pausa (el cliente prometió pagar a partir de esa fecha).
        prom = c.get("promesa_fecha")
        if prom and str(prom)[:10] >= hoy.isoformat():
            continue
        # Dedupe por día.
        if c.get("ultimo_recordatorio") == hoy:
            continue
        email = (c.get("cliente_email") or "").strip()
        if not email or "@" not in email:
            continue
        asunto, cuerpo = _asunto_y_cuerpo(c, empresa)
        if enviar_email(email, asunto, cuerpo):
            execute(
                "UPDATE cobros SET ultimo_recordatorio = %s "
                "WHERE tenant_id = %s AND id = %s",
                (hoy, tenant_id, c["id"])
            )
            enviados += 1
    return {"ok": True, "enviados": enviados, "fecha": hoy.isoformat()}
