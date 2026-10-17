FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# ffmpeg: kecilkan audio video sebelum dikirim ke Whisper.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

CMD ["uv", "run", "--no-dev", "inspire"]
