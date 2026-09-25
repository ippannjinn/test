"""Local CA + server certificate management for LAN HTTPS."""
from __future__ import annotations

import datetime as dt
import ipaddress
import socket
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def ensure_ca(certs_dir: Path) -> tuple[Path, Path]:
    certs_dir.mkdir(parents=True, exist_ok=True)
    ca_crt, ca_key = certs_dir / "ca.crt", certs_dir / "ca.key"
    if ca_crt.exists() and ca_key.exists():
        return ca_crt, ca_key
    key = _key()
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "NextAI Platform"),
        x509.NameAttribute(NameOID.COMMON_NAME, f"NextAI Local CA ({socket.gethostname()})"),
    ])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                     content_commitment=False, key_encipherment=False, data_encipherment=False,
                                     key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_key(ca_key, key)
    ca_crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return ca_crt, ca_key


def local_names_and_ips(extra_ips: list[str] | None = None) -> tuple[list[str], list[str]]:
    host = socket.gethostname()
    names = sorted({host, host.lower(), f"{host.lower()}.local", "localhost"})
    ips = {"127.0.0.1", "::1"}
    for ip in extra_ips or []:
        ips.add(ip)
    return names, sorted(ips)


def ensure_server_cert(certs_dir: Path, ips: list[str], names: list[str] | None = None,
                       force: bool = False) -> tuple[Path, Path]:
    ca_crt_path, ca_key_path = ensure_ca(certs_dir)
    crt, key_path = certs_dir / "server.crt", certs_dir / "server.key"
    base_names, base_ips = local_names_and_ips(ips)
    names = sorted(set(base_names) | set(names or []))
    if crt.exists() and key_path.exists() and not force:
        existing = x509.load_pem_x509_certificate(crt.read_bytes())
        try:
            san = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have_ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
            have_names = set(san.get_values_for_type(x509.DNSName))
        except x509.ExtensionNotFound:
            have_ips, have_names = set(), set()
        expires = existing.not_valid_after_utc
        fresh = expires - dt.datetime.now(dt.timezone.utc) > dt.timedelta(days=30)
        if fresh and set(base_ips) <= have_ips and set(names) <= have_names:
            return crt, key_path
    ca_cert = x509.load_pem_x509_certificate(ca_crt_path.read_bytes())
    ca_key = serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None)
    key = _key()
    now = dt.datetime.now(dt.timezone.utc)
    alt = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in base_ips]
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=800))
        .add_extension(x509.SubjectAlternativeName(alt), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    _write_key(key_path, key)
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca_crt_path.read_bytes())
    return crt, key_path


def fingerprint_sha256(cert_path: Path) -> str:
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    fp = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))
