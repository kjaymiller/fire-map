ARG VARIANT="3.10"
FROM --platform=amd64 mcr.microsoft.com/vscode/devcontainers/python:0-${VARIANT}

ENV PYTHONUNBUFFERED 1


RUN apt-get update && export DEBIAN_FRONTEND=noninteractive