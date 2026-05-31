FROM dailyco/pipecat-base:latest

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Sam — the AI dispatcher
COPY ./bot-sam.py bot.py
COPY ./nemotron_llm.py nemotron_llm.py
COPY ./nvidia_stt.py nvidia_stt.py
COPY ./sam ./sam
