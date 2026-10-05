FROM mcr.microsoft.com/devcontainers/python:3-3.14-trixie

USER root

COPY .devcontainer/certs/ /tmp/aitobuild-bootstrap/.devcontainer/certs/
COPY .devcontainer/import-certs.sh /tmp/aitobuild-bootstrap/.devcontainer/import-certs.sh

RUN WORKSPACE_FOLDER=/tmp/aitobuild-bootstrap \
    bash /tmp/aitobuild-bootstrap/.devcontainer/import-certs.sh \
    && rm -rf /tmp/aitobuild-bootstrap

USER vscode