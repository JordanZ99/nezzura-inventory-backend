# ==============================================================================
# backend/scripts/enviar_recordatorios.py
# Recordatorios automáticos de cobros (migración 080).
#
# Pensado para un Cron Job de Render (o ejecución manual):
#   python -m scripts.enviar_recordatorios
#
# Recorre los tenants con giro 'servicios' y manda los emails que tocan HOY
# según la cadencia (vence mañana / vence hoy / vencido 1, 7, 15 y 30 días).
# - Sin SMTP_* configurado: no-op silencioso (la app nunca se rompe).
# - Con promesa de pago vigente: ese cobro se salta (pausa).
# - Dedupe por día: a lo más un email por cobro y por día.
# ==============================================================================

import sys
from pathlib import Path

# Permitir importar database.* corriendo desde backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.conexion import query  # noqa: E402
from database.recordatorios import enviar_recordatorios_del_tenant  # noqa: E402
from database.notificaciones import smtp_configurado  # noqa: E402


def main() -> None:
    if not smtp_configurado():
        print("SMTP no configurado: los recordatorios por email quedan pausados.")
        return
    tenants = query("SELECT id, empresa FROM tenants WHERE giro = 'servicios'")
    total = 0
    for t in tenants:
        r = enviar_recordatorios_del_tenant(t["id"])
        total += r.get("enviados") or 0
        print(f"  {t.get('empresa')}: {r.get('enviados')} recordatorio(s)")
    print(f"Recordatorios enviados hoy: {total}")


if __name__ == "__main__":
    main()
