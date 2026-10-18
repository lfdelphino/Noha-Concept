"""Password records shared by the installer, CLI, and privileged helper."""
import hashlib
import hmac
import secrets


def password_record(password):
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise ValueError("A senha precisa ter de 1 a 256 caracteres")
    salt = secrets.token_hex(16)
    return {"salt": salt, "hash": hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex(),
            "revision": secrets.token_hex(16)}


def verify_password(password, record):
    if not isinstance(password, str) or len(password) > 256:
        return False
    hashed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record["salt"]), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(hashed, record["hash"])
