FROM alpine:3.23 AS stylua

ARG STYLUA_VERSION=2.2.0
ARG STYLUA_SHA256=6334c228ddf93e93e095d28206eae9fd25fd0110e568d720a5004b33d09d17b8

RUN apk add --no-cache ca-certificates curl unzip \
    && curl --fail --location --silent --show-error \
        --output /tmp/stylua.zip \
        "https://github.com/JohnnyMorganz/StyLua/releases/download/v${STYLUA_VERSION}/stylua-linux-x86_64.zip" \
    && echo "${STYLUA_SHA256}  /tmp/stylua.zip" | sha256sum -c \
    && mkdir -p /out \
    && unzip -p /tmp/stylua.zip stylua > /out/stylua \
    && chmod 0755 /out/stylua

FROM python:3.13-slim-bookworm AS python-dependencies

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv

WORKDIR /opt/lua-ci-action
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git lua5.1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-dependencies /opt/lua-ci-action/.venv /opt/lua-ci-action/.venv
COPY --from=stylua /out/stylua /usr/local/bin/stylua

ENV PATH="/opt/lua-ci-action/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "-m", "lua_ci_action"]
