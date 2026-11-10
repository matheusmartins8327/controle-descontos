"""
Envio de e-mails pelo Gmail (SMTP com senha de app).

O PythonAnywhere gratuito libera conexões SMTP apenas para o Gmail (smtp.gmail.com:587).
A conta Gmail precisa ter a verificação em duas etapas ativa e uma "senha de app".
"""
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

SMTP_HOST = "smtp.gmail.com"
SMTP_PORTA = 587


class FalhaEnvio(Exception):
    """Erro de envio com mensagem amigável para exibir no sistema."""


def enviar(remetente, nome_remetente, senha_app, para, copia, assunto, texto, html, timeout=30):
    """Envia um e-mail. `para` e `copia` são listas de (nome, email)."""
    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = formataddr((nome_remetente, remetente))
    msg["To"] = ", ".join(formataddr(d) for d in para)
    if copia:
        msg["Cc"] = ", ".join(formataddr(d) for d in copia)
    msg["Message-ID"] = make_msgid(domain=remetente.split("@")[-1])
    msg.set_content(texto)
    msg.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORTA, timeout=timeout) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(remetente, senha_app.replace(" ", ""))
            smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise FalhaEnvio("O Gmail recusou o login: confira o e-mail remetente e a senha de app.")
    except smtplib.SMTPRecipientsRefused:
        raise FalhaEnvio("O Gmail recusou um ou mais destinatários: confira os e-mails cadastrados.")
    except (socket.timeout, TimeoutError):
        raise FalhaEnvio("Tempo esgotado ao conectar ao Gmail. Será feita uma nova tentativa.")
    except (smtplib.SMTPException, OSError) as e:
        raise FalhaEnvio(f"Falha ao enviar pelo Gmail ({e.__class__.__name__}: {e}).")
