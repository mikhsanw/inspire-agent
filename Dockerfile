FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# ffmpeg: kecilkan audio video untuk Whisper. git: versi skill. ssh: fitur web (ke server).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg git openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

CMD ["inspire", "run"]
