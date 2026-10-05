from __future__ import annotations

import importlib.util
import os
import ssl
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

DEVCONTAINER_DIR = Path(__file__).resolve().parents[1] / ".devcontainer"
spec = importlib.util.spec_from_file_location("export_host_certs", DEVCONTAINER_DIR / "export-host-certs.py")
assert spec is not None and spec.loader is not None
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


@pytest.fixture
def certificate() -> bytes:
    return ssl.create_default_context().get_ca_certs(binary_form=True)[0]


def test_pem_bundles_and_der_are_supported(certificate: bytes) -> None:
    pem = ssl.DER_cert_to_PEM_cert(certificate).encode("ascii")
    content = b"-----BEGIN PRIVATE KEY-----\nnot-a-certificate\n-----END PRIVATE KEY-----\n"
    assert exporter.certificates_from_bytes(content + pem + pem) == [certificate, certificate]
    assert exporter.certificates_from_bytes(certificate) == [certificate]
    with pytest.raises(ValueError, match="Expected PEM"):
        exporter.certificates_from_bytes(content)
    with pytest.raises(ValueError, match="Expected PEM"):
        exporter.certificates_from_bytes(b"\x30not-a-certificate")


def test_export_deduplicates_and_removes_only_managed_files(tmp_path: Path, certificate: bytes) -> None:
    (tmp_path / "host-obsolete.crt").write_text("old", encoding="ascii")
    (tmp_path / "manual.crt").write_text("keep", encoding="ascii")

    assert exporter.export_certificates([certificate, certificate], tmp_path) == 1
    exported = list(tmp_path.glob("host-*.crt"))
    assert len(exported) == 1
    assert ssl.PEM_cert_to_DER_cert(exported[0].read_text(encoding="ascii")) == certificate
    assert (tmp_path / "manual.crt").read_text(encoding="ascii") == "keep"


def test_empty_export_preserves_existing_files(tmp_path: Path) -> None:
    existing = tmp_path / "host-existing.crt"
    existing.write_text("keep", encoding="ascii")
    with pytest.raises(ValueError, match="No trusted host certificates"):
        exporter.export_certificates([], tmp_path)
    assert existing.read_text(encoding="ascii") == "keep"


def test_windows_stores_filter_non_tls_and_non_x509_certificates(monkeypatch) -> None:
    stores = []

    def enumerate_certificates(store):
        stores.append(store)
        return [
            (b"trusted-root", "x509_asn", True),
            (b"trusted-tls", "x509_asn", {exporter.SERVER_AUTH_OID}),
            (b"untrusted-tls", "x509_asn", {"1.3.6.1.5.5.7.3.3"}),
            (b"unsupported", "pkcs_7_asn", True),
        ]

    monkeypatch.setattr(exporter.ssl, "enum_certificates", enumerate_certificates, raising=False)
    assert exporter.host_certificates("Windows") == [b"trusted-root", b"trusted-tls"] * 2
    assert stores == ["ROOT", "CA"]


def test_macos_exports_only_certificates_verified_by_keychain(monkeypatch, certificate: bytes) -> None:
    pem = ssl.DER_cert_to_PEM_cert(certificate).encode("ascii")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1] == "find-certificate":
            return subprocess.CompletedProcess(command, 0, stdout=pem)
        assert "-L" in command
        assert command[command.index("-p") + 1] == "ssl"
        assert Path(command[-1]).read_bytes() == pem
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(exporter.subprocess, "run", run)
    assert exporter.host_certificates("Darwin") == [certificate]
    assert sum(command[1] == "find-certificate" for command in commands) == 3
    assert sum(command[1] == "verify-cert" for command in commands) == 1

    monkeypatch.setattr(
        exporter.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1, stdout=pem),
    )
    assert exporter.host_certificates("Darwin") == []


def test_linux_includes_lazy_hashed_ca_directory(monkeypatch, tmp_path: Path, certificate: bytes) -> None:
    (tmp_path / "1234abcd.0").write_text(ssl.DER_cert_to_PEM_cert(certificate), encoding="ascii")
    (tmp_path / "untrusted.crt").write_text("not-active", encoding="ascii")
    monkeypatch.setattr(
        exporter.ssl,
        "create_default_context",
        lambda: SimpleNamespace(get_ca_certs=lambda **kwargs: []),
    )
    monkeypatch.setattr(
        exporter.ssl, "get_default_verify_paths", lambda: SimpleNamespace(capath=str(tmp_path))
    )
    assert exporter.host_certificates("Linux") == [certificate]


def test_unsupported_host_fails() -> None:
    with pytest.raises(ValueError, match="Unsupported host"):
        exporter.host_certificates("Unsupported")


def test_cli_explicit_source(tmp_path: Path, certificate: bytes) -> None:
    source = tmp_path / "source.cer"
    source.write_bytes(certificate)
    output = tmp_path / "output"
    result = subprocess.run(
        [
            sys.executable, str(DEVCONTAINER_DIR / "export-host-certs.py"),
            "--source", str(source), "--output-dir", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Exported 1 public certificates" in result.stdout
    assert len(list(output.glob("*.crt"))) == 1


@pytest.mark.parametrize("allow_container", [False, True])
def test_container_export_requires_opt_in(
    monkeypatch, tmp_path: Path, certificate: bytes, allow_container: bool, capsys,
) -> None:
    output = tmp_path / "output"
    arguments = ["export-host-certs.py", "--output-dir", str(output)]
    if allow_container:
        arguments.append("--allow-container")
    monkeypatch.setattr(sys, "argv", arguments)
    original_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists",
        lambda path: str(path) == "/.dockerenv" or original_exists(path),
    )
    monkeypatch.setattr(exporter.platform, "system", lambda: "Linux")
    systems = []

    def host_certificates(system):
        systems.append(system)
        return [certificate]

    monkeypatch.setattr(exporter, "host_certificates", host_certificates)
    assert exporter.main() == (0 if allow_container else 1)
    if allow_container:
        assert systems == ["Linux"]
        assert len(list(output.glob("host-*.crt"))) == 1
    else:
        assert systems == []
        assert not output.exists()
        assert "Run the exporter on the HOST" in capsys.readouterr().err


def test_container_initialization_preserves_host_exports(
    monkeypatch, tmp_path: Path, certificate: bytes, capsys,
) -> None:
    exporter.export_certificates([certificate], tmp_path)
    existing = next(tmp_path.glob("host-*.crt"))
    original_content = existing.read_bytes()
    monkeypatch.setattr(sys, "argv", ["export-host-certs.py", "--output-dir", str(tmp_path)])
    original_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists",
        lambda path: str(path) == "/.dockerenv" or original_exists(path),
    )

    def unexpected_export(system):
        pytest.fail("Container trust must not replace host certificates")

    monkeypatch.setattr(exporter, "host_certificates", unexpected_export)
    assert exporter.main() == 0
    assert existing.read_bytes() == original_content
    assert "container trust was not exported" in capsys.readouterr().out


def test_initialization_does_not_opt_in_to_container_trust() -> None:
    config = (DEVCONTAINER_DIR / "devcontainer.json").read_text(encoding="utf-8")
    initialize = next(line for line in config.splitlines() if '"initializeCommand"' in line)
    assert "--allow-container" not in initialize


def test_base_image_imports_host_certificates_before_features() -> None:
    config = (DEVCONTAINER_DIR / "devcontainer.json").read_text(encoding="utf-8")
    assert '"dockerfile": "Dockerfile"' in config
    assert '"context": ".."' in config
    assert '"image":' not in config
    dockerfile = (DEVCONTAINER_DIR / "Dockerfile").read_text(encoding="utf-8")
    copy_certificates = dockerfile.index("COPY .devcontainer/certs/")
    copy_importer = dockerfile.index("COPY .devcontainer/import-certs.sh")
    import_certificates = dockerfile.index("RUN WORKSPACE_FOLDER=/tmp/aitobuild-bootstrap")
    assert copy_certificates < import_certificates
    assert copy_importer < import_certificates
    assert "bash /tmp/aitobuild-bootstrap/.devcontainer/import-certs.sh" in dockerfile
    assert dockerfile.index("USER root") < import_certificates < dockerfile.index("USER vscode")


def _import_environment(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    workspace = tmp_path / "workspace with spaces"
    sources = workspace / ".devcontainer" / "certs"
    sources.mkdir(parents=True)
    trust_dir = tmp_path / "trust"
    trust_dir.mkdir()
    commands = tmp_path / "commands"
    commands.mkdir()
    sudo = commands / "sudo"
    sudo.write_text('#!/usr/bin/env bash\n[[ "$1" == "-n" ]] || exit 1\nshift\nexec "$@"\n')
    sudo.chmod(0o755)
    update = commands / "update-ca-certificates"
    update.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$DEVCONTAINER_UPDATE_LOG"\n')
    update.chmod(0o755)
    environment = {
        **os.environ,
        "WORKSPACE_FOLDER": str(workspace),
        "DEVCONTAINER_CA_TRUST_DIR": str(trust_dir),
        "DEVCONTAINER_UPDATE_LOG": str(tmp_path / "update.log"),
        "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
    }
    return sources, trust_dir, environment


def test_importer_normalizes_der_deduplicates_and_rotates(tmp_path: Path, certificate: bytes) -> None:
    sources, trust_dir, environment = _import_environment(tmp_path)
    pem = ssl.DER_cert_to_PEM_cert(certificate)
    (sources / "root.crt").write_text(pem, encoding="ascii")
    (sources / "same-root.CER").write_bytes(certificate)
    (trust_dir / "old.crt").write_text("old", encoding="ascii")
    (trust_dir / "keep.txt").write_text("keep", encoding="ascii")

    result = subprocess.run(
        ["bash", str(DEVCONTAINER_DIR / "import-certs.sh")],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Imported 1 CA certificates" in result.stdout
    installed = list(trust_dir.glob("*.crt"))
    assert len(installed) == 1
    assert installed[0].read_text(encoding="ascii") == pem
    assert (trust_dir / "keep.txt").exists()
    assert (tmp_path / "update.log").read_text() == "--fresh\n"


def test_invalid_import_keeps_existing_trust(tmp_path: Path) -> None:
    sources, trust_dir, environment = _import_environment(tmp_path)
    (sources / "broken.crt").write_text("not-a-certificate", encoding="ascii")
    existing = trust_dir / "existing.crt"
    existing.write_text("keep", encoding="ascii")
    result = subprocess.run(
        ["bash", str(DEVCONTAINER_DIR / "import-certs.sh")],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Invalid certificate" in result.stderr
    assert existing.read_text(encoding="ascii") == "keep"
    assert not (tmp_path / "update.log").exists()


def test_no_custom_certificates_needs_no_privileges(tmp_path: Path) -> None:
    _sources, _trust_dir, environment = _import_environment(tmp_path)
    result = subprocess.run(
        ["bash", str(DEVCONTAINER_DIR / "import-certs.sh")],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "No custom CA certificates found" in result.stdout
    assert not (tmp_path / "update.log").exists()