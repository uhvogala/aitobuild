#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE_FOLDER:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TRUST_DIR="${DEVCONTAINER_CA_TRUST_DIR:-/usr/local/share/ca-certificates/aitobuild}"
staging_dir="$(mktemp -d)"
trap 'rm -rf -- "${staging_dir}"' EXIT

for cert_dir in \
  "${WORKSPACE_DIR}/.devcontainer/certs" \
  "/tmp/devcontainer-certs" \
  "/tmp/host-ca-certs"; do
  [[ -d "${cert_dir}" ]] || continue

  while IFS= read -r -d '' cert_file; do
    normalized="${staging_dir}/certificate.pem"
    if ! openssl x509 -in "${cert_file}" -out "${normalized}" 2>/dev/null; then
      if ! openssl x509 -inform DER -in "${cert_file}" -out "${normalized}" 2>/dev/null; then
        echo "Invalid certificate: ${cert_file}" >&2
        exit 1
      fi
    fi
    if ! openssl x509 -in "${normalized}" -noout -ext basicConstraints | grep -q 'CA:TRUE'; then
      continue
    fi
    fingerprint="$(openssl x509 -in "${normalized}" -noout -fingerprint -sha256)"
    fingerprint="${fingerprint#*=}"
    fingerprint="${fingerprint//:/}"
    mv "${normalized}" "${staging_dir}/${fingerprint}.crt"
  done < <(find "${cert_dir}" -maxdepth 4 -type f \( -iname '*.crt' -o -iname '*.pem' -o -iname '*.cer' \) -print0)
done

shopt -s nullglob
certificates=("${staging_dir}"/*.crt)
if [[ "${#certificates[@]}" -eq 0 ]]; then
  echo "No custom CA certificates found. Export host certificates before rebuilding."
  exit 0
fi

privileged=()
if [[ "$(id -u)" -ne 0 ]]; then
  privileged=(sudo -n)
fi
"${privileged[@]}" mkdir -p "${TRUST_DIR}"
"${privileged[@]}" find "${TRUST_DIR}" -maxdepth 1 -type f -name '*.crt' -delete
"${privileged[@]}" install -m 0644 "${certificates[@]}" "${TRUST_DIR}/"
"${privileged[@]}" update-ca-certificates --fresh
echo "Imported ${#certificates[@]} CA certificates into the container trust store."