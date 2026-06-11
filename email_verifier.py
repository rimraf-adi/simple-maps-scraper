import dns.resolver
import smtplib
import socket
import logging

log = logging.getLogger("maps_scraper.verifier")

def verify_email_sync(email: str) -> bool:
    """
    Verify an email address without sending an email.
    1. Check MX records to ensure the domain accepts mail.
    2. Attempt a standard SMTP RCPT TO command to see if the user exists.
    """
    try:
        domain = email.split('@')[1]
    except IndexError:
        return False

    # 1. Check MX records
    try:
        records = dns.resolver.resolve(domain, 'MX')
        mx_record = str(records[0].exchange)
    except Exception as e:
        log.debug(f"MX lookup failed for {domain}: {e}")
        return False

    # 2. SMTP Verification
    try:
        server = smtplib.SMTP(timeout=5)
        server.set_debuglevel(0)
        # Connect to the mail server
        server.connect(mx_record)
        server.helo(socket.getfqdn())
        server.mail('admin@google.com')  # Safe generic sender
        code, message = server.rcpt(email)
        server.quit()

        # 250 means OK (User exists and will accept mail)
        if code == 250:
            return True
        # If code is 550, it strictly means user does not exist
        elif code >= 500:
            return False
            
    except Exception as e:
        log.debug(f"SMTP check failed or timed out for {email}: {e}")
        # If SMTP times out or drops connection (like many anti-spam firewalls do),
        # we assume the email is valid because the MX record exists.
        pass

    return True

async def verify_email(email: str) -> bool:
    """Async wrapper for the email verifier."""
    import asyncio
    return await asyncio.to_thread(verify_email_sync, email)
