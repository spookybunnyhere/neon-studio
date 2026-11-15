"""
Neon Studio - licence keys and payment settings.

How it works
------------
A licence key is an Ed25519 *signature* of the buyer's e-mail address.
  * The seller's PRIVATE key (made by seller/setup_keys.py, never shipped) creates keys.
  * The app only contains the PUBLIC key, which can check a key but cannot create one.
So nobody can generate keys from the app's code, and a key only works with the e-mail it was issued to.

Ed25519 is implemented here in plain Python (RFC 8032) so the app needs no extra package.

Honest limit: this is open Python code, so a determined person can always edit the app to skip the check.
The scheme stops casual sharing/guessing; it is not copy-protection against a programmer.
"""
import hashlib
import base64
import json
import re
from pathlib import Path
from urllib.parse import quote

# ----------------------------------------------------------------------------
# Filled in automatically by  seller/setup_keys.py  (64 hex characters).
# ----------------------------------------------------------------------------
PUBLIC_KEY_HEX = "5b076a0b4ac31898d3f30c74c57e8eb89f127418435843a976e7157a4a34ee17"

PRODUCT = "NEON-STUDIO-PREMIUM-v1"
KEY_PREFIX = "NS1"
HERE = Path(__file__).resolve().parent
PAYMENT_CONFIG_FILE = HERE / "payment_config.json"

DEFAULT_PAYMENT = {
    "upi_id": "",                 # e.g. "yourname@okaxis"
    "payee_name": "Neon Studio",
    "price_inr": 129,
    "qr_image": "upi_qr.png",     # your own UPI QR screenshot, next to neon_studio.py
    "support_email": "",          # your Gmail: buyers' payment details are sent here
    "whatsapp_number": "",        # optional, with country code and no +, e.g. 919876543210
    "form_url": "",               # optional: a form link, if you ever make one
}


def load_payment_config():
    cfg = dict(DEFAULT_PAYMENT)
    try:
        cfg.update(json.loads(PAYMENT_CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def upi_uri(cfg):
    """upi:// deep link; also the text encoded in a generated QR."""
    if not cfg.get("upi_id"):
        return ""
    return ("upi://pay?pa=%s&pn=%s&am=%s&cu=INR&tn=%s"
            % (quote(cfg["upi_id"], safe="@"), quote(cfg.get("payee_name", ""), safe=""),
               cfg.get("price_inr", 129), quote("Neon Studio Premium", safe="")))


# ----------------------------------------------------------------------------
# Ed25519 (RFC 8032 reference algorithm)
# ----------------------------------------------------------------------------
_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_sqrt_m1 = pow(2, (_p - 1) // 4, _p)


def _sha512(s):
    return hashlib.sha512(s).digest()


def _sha512_modq(s):
    return int.from_bytes(_sha512(s), "little") % _q


def _inv(x):
    return pow(x, _p - 2, _p)


def _add(P, Q):
    A, B = (P[1] - P[0]) * (Q[1] - Q[0]) % _p, (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C, D = 2 * P[3] * Q[3] * _d % _p, 2 * P[2] * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return ((P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and
            (P[1] * Q[2] - Q[1] * P[2]) % _p == 0)


def _recover_x(y, sign):
    if y >= _p:
        return None
    x2 = (y * y - 1) * _inv(_d * y * y + 1) % _p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _sqrt_m1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * _inv(5) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _compress(P):
    zinv = _inv(P[2])
    x, y = P[0] * zinv % _p, P[1] * zinv % _p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(s):
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _p)


def _expand(secret):
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_from_secret(secret32):
    a, _ = _expand(secret32)
    return _compress(_mul(a, _G))


def sign(secret32, msg):
    a, prefix = _expand(secret32)
    A = _compress(_mul(a, _G))
    r = _sha512_modq(prefix + msg)
    Rs = _compress(_mul(r, _G))
    h = _sha512_modq(Rs + A + msg)
    s = (r + h * a) % _q
    return Rs + int.to_bytes(s, 32, "little")


def verify(public32, msg, sig64):
    try:
        if len(public32) != 32 or len(sig64) != 64:
            return False
        A = _decompress(public32)
        Rs = sig64[:32]
        R = _decompress(Rs)
        if A is None or R is None:
            return False
        s = int.from_bytes(sig64[32:], "little")
        if s >= _q:
            return False
        h = _sha512_modq(Rs + public32 + msg)
        return _equal(_mul(s, _G), _add(R, _mul(h, A)))
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Licence keys
# ----------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalise_email(email):
    return (email or "").strip().lower()


def _message(email):
    return (PRODUCT + "|" + normalise_email(email)).encode("utf-8")


def make_key(secret32, email):
    """Seller side: create the key for this buyer e-mail."""
    raw = base64.b32encode(sign(secret32, _message(email))).decode().rstrip("=")
    groups = [raw[i:i + 8] for i in range(0, len(raw), 8)]
    return KEY_PREFIX + "-" + "-".join(groups)


def _decode_key(key):
    k = re.sub(r"[\s\-]", "", (key or "").upper())
    if not k.startswith(KEY_PREFIX):
        return None
    k = k[len(KEY_PREFIX):]
    k += "=" * (-len(k) % 8)
    try:
        sig = base64.b32decode(k)
    except Exception:
        return None
    return sig if len(sig) == 64 else None


def check_key(email, key, public_hex=None):
    """App side: True only if `key` was issued for `email` by the holder of the private key."""
    public_hex = PUBLIC_KEY_HEX if public_hex is None else public_hex
    if not public_hex or not EMAIL_RE.match(normalise_email(email)):
        return False
    sig = _decode_key(key)
    if sig is None:
        return False
    try:
        return verify(bytes.fromhex(public_hex), _message(email), sig)
    except ValueError:
        return False
