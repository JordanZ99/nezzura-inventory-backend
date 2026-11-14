# ==============================================================================
# backend/database/notificaciones.py
# Envío de email genérico vía SMTP, configurado 100% por variables de entorno.
#
# Si SMTP_* no está configurado, enviar_email devuelve False en silencio: la
# app nunca se rompe por notificaciones y la funcionalidad queda lista para
# cuando se contrate el servicio (Gmail con app-password, Resend SMTP, etc.).
#
# Variables de entorno (Render):
#   SMTP_HOST  — ej. smtp.gmail.com
#   SMTP_PORT  — ej. 587 (STARTTLS) o 465 (SSL)
#   SMTP_USER  — usuario
#   SMTP_PASS  — contraseña / app-password
#   SMTP_FROM  — remitente visible (default: SMTP_USER)
# ==============================================================================

import os
import smtplib
from email.mime.text import MIMEText


def smtp_configurado() -> bool:
    return bool(
        os.getenv("SMTP_HOST")
        and os.getenv("SMTP_USER")
        and os.getenv("SMTP_PASS")
    )


def enviar_email(destino: str, asunto: str, cuerpo: str) -> bool:
    """
    Envía un email de texto plano. True = enviado; False = sin SMTP
    configurado o error (se loguea, nunca lanza: las notificaciones son
    best-effort y jamás rompen un flujo de negocio).
    """
    destino = (destino or "").strip()
    if not destino or "@" not in destino:
        return False
    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASS", "")
    from_ = os.getenv("SMTP_FROM", user)
    if not (host and user and password):
        return False

    try:
        mensaje = MIMEText(cuerpo, "plain", "utf-8")
        mensaje["Subject"] = asunto
        mensaje["From"] = from_
        mensaje["To"] = destino

        puerto = int(os.getenv("SMTP_PORT", "587"))
        if puerto == 465:
            with smtplib.SMTP_SSL(host, puerto, timeout=15) as smtp:
                smtp.login(user, password)
                smtp.send_message(mensaje)
        else:
            with smtplib.SMTP(host, puerto, timeout=15) as smtp:
                smtp.starttls()
                smtp.login(user, password)
                smtp.send_message(mensaje)
        return True
    except Exception as e:
        print(f"⚠️ Error enviando email a {destino}: {e}")
        return False
