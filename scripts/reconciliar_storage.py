# ==============================================================================
# backend/scripts/reconciliar_storage.py
# Mantenimiento nocturno del medidor de almacenamiento (migración 071).
#
# Pensado para un Cron Job de Render (o ejecución manual):
#   python -m scripts.reconciliar_storage
#
# Hace tres cosas, en orden:
#   1. RECONCILIACIÓN — recomputa bytes_usados de cada tenant desde el ledger
#      (storage_eventos, fuente de verdad). Detecta drift de requests que
#      crashearon a medias.
#   2. AVISOS — registra los cruces de umbrales (80% / 95% / 100% excedido)
#      con dedupe por (tenant, umbral): un aviso por cruce hasta que el uso
#      vuelva a bajar (migración 072).
#   3. EMAIL — si SMTP_* está configurado (database/notificaciones.py), envía
#      un email por aviso nuevo a tenants.correo. Sin SMTP: los avisos quedan
#      registrados y el medidor de la app se encarga del resto.
# ==============================================================================

import os
import sys
from pathlib import Path

# Permitir importar database.* corriendo desde backend/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.almacenamiento import actualizar_estimaciones, procesar_avisos, reconciliar  # noqa: E402
from database.notificaciones import enviar_email, smtp_configurado  # noqa: E402

MB = 1024 * 1024

TEXTO_UMBRAL = {
    80: "ya usaste el 80% de tu espacio",
    95: "casi llenaste tu espacio",
    100: "tu espacio está LLENO",
}


def _cuerpo_aviso(a: dict) -> str:
    gb_usados = a["bytes_usados"] / (1024 * 1024 * 1024)
    gb_limite = a["limite_bytes"] / (1024 * 1024 * 1024)
    lineas = [
        f"Hola, equipo de {a['empresa']}:",
        "",
        f"En Nezzura Digital {TEXTO_UMBRAL[a['umbral']]}: "
        f"{gb_usados:.2f} GB de {gb_limite:.0f} GB ({a['porcentaje']}%), plan {a['plan']}.",
        "",
    ]
    if a["umbral"] >= 100:
        lineas += [
            "Mientras tu espacio esté lleno no podrás subir imágenes nuevas.",
            "Opciones:",
            "  1. Libera espacio: en la app entra a Ajustes → Gestor de espacio",
            "     y borra las imágenes que ya no uses (ordenadas por peso).",
            "  2. Mejora al plan Plus (10 GB) para seguir creciendo.",
        ]
    else:
        lineas += [
            "Puedes revisar tu uso y liberar espacio en la app:",
            "Ajustes → Gestor de espacio (imágenes ordenadas por peso).",
        ]
    lineas += [
        "",
        "Descarga tu respaldo cuando quieras: Gestor de espacio → "
        "«Descargar respaldo completo» (datos + fotos).",
        "",
        "— Nezzura Digital",
    ]
    return "\n".join(lineas)


def main() -> None:
    resultado = reconciliar()
    corregidos = resultado.get("corregidos") or []
    print(f"Reconciliación: {resultado.get('revisados')} tenants revisados, "
          f"{len(corregidos)} corregidos.")
    for c in corregidos:
        print(f"  - {c['tenant_id'][:8]}…: {c['antes']} → {c['despues']} bytes")

    # Estimaciones nocturnas de datos/historial/sin_uso (migración 073):
    # se cachean en tenant_storage para que /uso siga siendo una lectura barata
    estimaciones = actualizar_estimaciones()
    print(f"Estimaciones datos/historial refrescadas: {estimaciones.get('actualizados')} tenants.")

    avisos = procesar_avisos()
    print(f"Avisos nuevos: {len(avisos)}.")
    if avisos and smtp_configurado():
        enviados = 0
        for a in avisos:
            destino = a.get("correo")
            if not destino:
                continue
            asunto = f"[Nezzura Digital] {TEXTO_UMBRAL[a['umbral']]} " \
                     f"({a['porcentaje']}% de tu plan {a['plan']})"
            if enviar_email(destino, asunto, _cuerpo_aviso(a)):
                enviados += 1
            else:
                print(f"  ⚠️ No se pudo enviar a {destino} ({a['tenant_id'][:8]}…)")
        print(f"Emails enviados: {enviados} de {len(avisos)}.")
    elif avisos:
        print("(SMTP no configurado: los avisos quedan registrados; "
              "el medidor de la app se encarga del resto.)")


if __name__ == "__main__":
    main()
