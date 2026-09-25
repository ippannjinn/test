from __future__ import annotations

import secrets
import string

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
_DUMMY_HASH = _hasher.hash("nextai-dummy-password-for-timing")

_COMMON = {
    "password", "password1", "password123", "123456789", "1234567890", "qwertyuiop", "iloveyou",
    "admin12345", "letmein123", "welcome123", "passw0rd", "abc1234567", "1q2w3e4r5t", "qwerty1234",
}


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> tuple[bool, bool]:
    """Returns (ok, needs_rehash). Always spends comparable time even for unknown users."""
    target = stored_hash or _DUMMY_HASH
    try:
        _hasher.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    if stored_hash is None:
        return False, False
    return True, _hasher.check_needs_rehash(stored_hash)


def password_problems(password: str, username: str, min_length: int) -> list[str]:
    problems = []
    if len(password) < min_length:
        problems.append(f"パスワードは{min_length}文字以上にしてください")
    if len(password) > 256:
        problems.append("パスワードが長すぎます")
    if username and username.lower() in password.lower():
        problems.append("パスワードにユーザー名を含めないでください")
    if password.lower() in _COMMON:
        problems.append("よく使われるパスワードは利用できません")
    if len(set(password)) < 4:
        problems.append("同じ文字の繰り返しは利用できません")
    return problems


def generate_password(length: int = 14) -> str:
    alphabet = string.ascii_letters + string.digits
    alphabet = "".join(c for c in alphabet if c not in "Il1O0o")
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isdigit() for c in pw) and any(c.isupper() for c in pw) and any(c.islower() for c in pw):
            return pw
