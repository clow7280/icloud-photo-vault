# NAS 是 x86_64，构建时使用 --platform linux/amd64
FROM python:3.13-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends 7zip tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

ENV PATH=/opt/venv/bin:$PATH \
    TZ=Asia/Shanghai \
    VAULT_SEVENZIP=7zz
# 以 root 启动，绑定网关 Socket 后降权到 VAULT_RUN_UID 和 VAULT_RUN_GID
ENTRYPOINT ["icloud-photo-vault"]
CMD ["serve"]
