"""
bot/email_backends.py
=====================
Custom Django email backend that forces IPv4 for the SMTP connection.

Why this exists
---------------
smtp.gmail.com publishes both A (IPv4) and AAAA (IPv6) DNS records.
Python's socket.create_connection tries the resolved addresses in the
order getaddrinfo returns them. On hosts without IPv6 egress (e.g.
Railway containers), an IPv6 attempt fails immediately with
"OSError: [Errno 101] Network is unreachable" instead of falling
through to the working IPv4 address.

IPv4SMTPBackend filters getaddrinfo to AF_INET for the duration of
the connection, so smtplib only ever sees IPv4 addresses. The hostname
is left untouched, so TLS SNI / certificate verification against
smtp.gmail.com still works (resolving to a raw IP would break that).

Set EMAIL_BACKEND=bot.email_backends.IPv4SMTPBackend to use it.
"""

import socket

from django.core.mail.backends.smtp import EmailBackend as _SMTPBackend


class IPv4SMTPBackend(_SMTPBackend):
    def open(self):
        original_getaddrinfo = socket.getaddrinfo

        def _ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
            # Force IPv4 regardless of what the caller asked for.
            return original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

        socket.getaddrinfo = _ipv4_only
        try:
            return super().open()
        finally:
            socket.getaddrinfo = original_getaddrinfo
