"""Read-only verification of current and historical Werkzeug credentials.

Legacy formats are accepted only for existing stored credentials. Generation
stays with the application's explicit modern method; verification never rewrites data.
"""

import hashlib
import hmac
import re

from werkzeug.security import check_password_hash

# Fixed-output methods accepted by the published v0.4.0 image's Werkzeug 2.3.8.
LEGACY_HMAC_METHODS = frozenset({
    'blake2b', 'blake2s', 'md5', 'md5-sha1', 'ripemd160', 'sha1', 'sha224',
    'sha256', 'sha384', 'sha3_224', 'sha3_256', 'sha3_384', 'sha3_512',
    'sha512', 'sha512_224', 'sha512_256', 'sm3',
})
_PBKDF2_METHODS = LEGACY_HMAC_METHODS - {'blake2b', 'blake2s'}
_LEGACY_PBKDF2_ITERATIONS = 600_000


def _parse(stored, *, for_api=False):
    """Return the verification method and payload, without running a KDF."""
    if not isinstance(stored, str):
        raise TypeError
    method, salt, digest = stored.split('$', 2)
    if method == 'plain':
        # v0.4.0 accepted this wrapper for API tokens, never at admin bootstrap.
        if not for_api or not digest or not stored.isascii():
            raise ValueError
        return 'plain', salt, digest
    if not salt and not for_api:
        raise ValueError
    parts = method.split(':')
    if parts[0] == 'pbkdf2':
        if len(parts) > 3:
            raise ValueError
        algorithm = parts[1] if len(parts) >= 2 else 'sha256'
        if algorithm not in _PBKDF2_METHODS:
            raise ValueError
        iterations = int(parts[2]) if len(parts) == 3 else _LEGACY_PBKDF2_ITERATIONS
        if not 1 <= iterations <= 2**31 - 1:
            raise ValueError
        method = f'pbkdf2:{algorithm}:{iterations}'
        size = hashlib.new(algorithm).digest_size
    elif parts[0] == 'scrypt':
        if len(parts) == 1:
            n, r, p = 32768, 8, 1
        elif len(parts) == 4:
            n, r, p = map(int, parts[1:])
        else:
            raise ValueError
        # These are also prerequisites of Werkzeug's hashlib.scrypt call.
        if n < 2 or n & (n - 1) or r < 1 or p < 1 or 132 * n * r * p > 2**31 - 1:
            raise ValueError
        method = f'scrypt:{n}:{r}:{p}'
        size = 64
    elif method in LEGACY_HMAC_METHODS:
        size = hashlib.new(method).digest_size
    else:
        raise ValueError
    if not re.fullmatch(r'[0-9a-f]{' + str(size * 2) + '}', digest):
        raise ValueError
    return method, salt, digest


def valid_credential_hash(stored, *, for_api=False):
    """Recognise supported stored formats without authenticating or modifying them."""
    try:
        _parse(stored, for_api=for_api)
        return True
    except (ValueError, TypeError, OverflowError):
        return False


def verify_credential(stored, supplied, *, for_api=False):
    """Fail closed on invalid input; never downgrade a failed modern check."""
    if not isinstance(supplied, str):
        return False
    try:
        method, salt, digest = _parse(stored, for_api=for_api)
        if method == 'plain':
            return supplied.isascii() and hmac.compare_digest(digest, supplied)
        if method in LEGACY_HMAC_METHODS:
            expected = hmac.new(salt.encode('utf-8'), supplied.encode('utf-8'), method).hexdigest()
            return hmac.compare_digest(expected, digest)
        # Explicit stored parameters retain their meaning. Shortened descriptors
        # above resolve v0.4.0's defaults, rather than newer library defaults.
        return check_password_hash(f'{method}${salt}${digest}', supplied)
    except (ValueError, TypeError, OverflowError, UnicodeError):
        return False
