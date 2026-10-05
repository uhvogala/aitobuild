#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import platform
import re
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"
PEM_CERTIFICATE = re.compile(
    rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL
)


def certificates_from_bytes(content: bytes) -> list[bytes]:
    blocks = PEM_CERTIFICATE.findall(content)
    if blocks:
        certificates = [ssl.PEM_cert_to_DER_cert(block.decode("ascii")) for block in blocks]
    else:
        certificates = [content]
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    for certificate in certificates:
        try:
            context.load_verify_locations(cadata=certificate)
        except ssl.SSLError as error:
            raise ValueError("Expected PEM certificate blocks or a DER-encoded certificate") from error
    return certificates


def macos_certificates() -> list[bytes]:
    certificates = []
    keychains = [
        "/System/Library/Keychains/SystemRootCertificates.keychain",
        "/Library/Keychains/System.keychain",
        None,
    ]
    for keychain in keychains:
        command = ["security", "find-certificate", "-a", "-p"]
        if keychain is not None:
            command.append(keychain)
        result = subprocess.run(command, check=True, capture_output=True, timeout=60)
        if result.stdout.strip():
            certificates.extend(certificates_from_bytes(result.stdout))

    trusted = []
    with tempfile.TemporaryDirectory() as temporary_dir:
        certificate_file = Path(temporary_dir) / "certificate.pem"
        for certificate in dict.fromkeys(certificates):
            certificate_file.write_text(ssl.DER_cert_to_PEM_cert(certificate), encoding="ascii")
            result = subprocess.run(
                ["security", "verify-cert", "-L", "-q", "-p", "ssl", "-c", str(certificate_file)],
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                trusted.append(certificate)
    return trusted


def windows_certificates() -> list[bytes]:
    certificates = []
    enumerate_certificates = getattr(ssl, "enum_certificates")
    for store in ("ROOT", "CA"):
        for certificate, encoding, trust in enumerate_certificates(store):
            if encoding == "x509_asn" and (trust is True or SERVER_AUTH_OID in trust):
                certificates.append(certificate)
    return certificates


def host_certificates(system: str) -> list[bytes]:
    if system == "Darwin":
        return macos_certificates()
    if system == "Windows":
        return windows_certificates()
    if system == "Linux":
        certificates = ssl.create_default_context().get_ca_certs(binary_form=True)
        certificate_path = ssl.get_default_verify_paths().capath
        if certificate_path:
            for path in Path(certificate_path).iterdir():
                if path.is_file() and re.fullmatch(r"[0-9a-fA-F]{8}\.\d+", path.name):
                    certificates.extend(certificates_from_bytes(path.read_bytes()))
        return certificates
    raise ValueError(f"Unsupported host operating system: {system}")


def export_certificates(certificates: list[bytes], output_dir: Path) -> int:
    unique = {
        f"host-{hashlib.sha256(certificate).hexdigest()}.crt": ssl.DER_cert_to_PEM_cert(certificate)
        for certificate in certificates
    }
    if not unique:
        raise ValueError("No trusted host certificates found; existing exports were not changed")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, content in unique.items():
        (output_dir / name).write_text(content, encoding="ascii")
    for old_file in output_dir.glob("host-*.crt"):
        if old_file.name not in unique:
            old_file.unlink()
    return len(unique)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export public host CA certificates for dev containers")
    parser.add_argument(
        "--output-dir", type=Path, default=Path(__file__).resolve().parent / "certs" / "host"
    )
    parser.add_argument(
        "--source", type=Path, action="append", help="Use explicit PEM/DER files instead of host stores"
    )
    parser.add_argument(
        "--allow-container", action="store_true",
        help="Allow initialization to export the bootstrap container's inherited CA trust",
    )
    args = parser.parse_args()
    try:
        if args.source:
            certificates = [
                certificate
                for source in args.source
                for certificate in certificates_from_bytes(source.read_bytes())
            ]
        else:
            in_container = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
            if in_container and not args.allow_container:
                existing = list(args.output_dir.glob("host-*.crt"))
                if existing:
                    for certificate_file in existing:
                        certificates_from_bytes(certificate_file.read_bytes())
                    print(
                        f"Reusing {len(existing)} existing host certificate exports from "
                        f"{args.output_dir}; container trust was not exported. "
                        "To refresh them, run the exporter on the HOST."
                    )
                    return 0
                raise ValueError(
                    "Run the exporter on the HOST, not inside a container: "
                    "python3 .devcontainer/export-host-certs.py. "
                    "Then rebuild the dev container."
                )
            certificates = host_certificates(platform.system())
        count = export_certificates(certificates, args.output_dir)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Host certificate export failed: {error}", file=sys.stderr)
        return 1
    print(f"Exported {count} public certificates to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())