#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_DIR="${WORKSPACE_FOLDER:-$(pwd)}"
CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
SYSTEM_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

import_custom_certs() {
  local cert_dir imported cert_file cert_name
  imported=0

  for cert_dir in \
    "${WORKSPACE_DIR}/.devcontainer/certs" \
    "/tmp/devcontainer-certs" \
    "/tmp/host-ca-certs"; do
    [[ -d "${cert_dir}" ]] || continue

    while IFS= read -r -d '' cert_file; do
      cert_name="$(basename "${cert_file}")"
      if [[ "${cert_name}" != *.crt ]]; then
        cert_name="${cert_name}.crt"
      fi
      sudo install -m 0644 "${cert_file}" "/usr/local/share/ca-certificates/${cert_name}"
      imported=1
    done < <(find "${cert_dir}" -maxdepth 4 -type f \( -name '*.crt' -o -name '*.pem' \) -print0)
  done

  if [[ "${imported}" -eq 1 ]]; then
    echo "Imported custom CA certs. Updating trust store..."
    sudo update-ca-certificates
  fi
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi

  if curl --fail --silent --show-error --location https://astral.sh/uv/install.sh | sh; then
    return
  fi

  echo "astral.sh installer failed, falling back to pip install --user uv"

  if ! env -u VIRTUAL_ENV PATH="${SYSTEM_PATH}" python3 -m pip --version >/dev/null 2>&1; then
    env -u VIRTUAL_ENV PATH="${SYSTEM_PATH}" python3 -m ensurepip --upgrade
  fi

  env -u VIRTUAL_ENV PATH="${SYSTEM_PATH}" python3 -m pip install --user --disable-pip-version-check uv
}

import_custom_certs

export SSL_CERT_FILE="${CA_BUNDLE}"
export CURL_CA_BUNDLE="${CA_BUNDLE}"
export REQUESTS_CA_BUNDLE="${CA_BUNDLE}"
export GIT_SSL_CAINFO="${CA_BUNDLE}"

install_uv

# Keep persisted Azure auth cache writable when mounted from a Docker volume.
mkdir -p "${HOME}/.azure"
sudo chown -R "$(id -u):$(id -g)" "${HOME}/.azure"

if [[ ! -x "${WORKSPACE_DIR}/.venv/bin/python" ]]; then
  "${HOME}/.local/bin/uv" venv --seed "${WORKSPACE_DIR}/.venv"
fi
