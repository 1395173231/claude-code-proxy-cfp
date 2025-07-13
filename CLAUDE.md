# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## High-Level Architecture

This repository contains a Python-based proxy server that translates Anthropic API requests to LiteLLM, which then routes them to OpenAI or Google Gemini models. It allows Anthropic clients (like Claude Code) to interact with non-Anthropic backends.

- `server.py`: The main FastAPI application that handles incoming Anthropic API requests, translates them, and forwards them to LiteLLM. It manages model mapping and response conversion.
- `cfp_adapter.py`: Contains logic for adapting Claude Function Protocol (CFP) to LiteLLM and vice-versa.
- `cfp_codec.py`: Handles encoding/decoding of CFP messages.
- `pyproject.toml`: Defines project dependencies (FastAPI, uvicorn, litellm, python-dotenv, httpx, pydantic).

## Common Commands

### Setup

To set up the development environment:
1.  **Install `uv`**: `curl -LsSf https://astral.sh/uv/install.sh | sh`
2.  **Install dependencies and create virtual environment**: `uv sync`
3.  **Configure environment variables**: `cp .env.example .env` and edit `.env` with API keys and model preferences.

### Running the Server

To start the proxy server:
-   `uv run uvicorn server:app --host 0.0.0.0 --port 8082 --reload`

### Using with Claude Code

To use Claude Code with this proxy:
1.  **Install Claude Code CLI**: `npm install -g @anthropic-ai/claude-code`
2.  **Connect to proxy**: `ANTHROPIC_BASE_URL=http://localhost:8082 claude`

### Running with Docker

To run the proxy server using Docker:
1.  **Build and run with Docker Compose**: `docker-compose up --build`
2.  **Or run directly with Docker**: `docker build -t anthropic-proxy . && docker run -p 8082:8082 --env-file .env anthropic-proxy`

The Docker setup includes:
- Multi-stage build using `uv` for efficient dependency management
- Health checks for service monitoring
- Volume mounting for log persistence
- Environment variable configuration via `.env` file

## Key Configuration

The proxy's behavior is primarily configured via environment variables in the `.env` file:
-   `OPENAI_API_KEY`: Your OpenAI API key.
-   `GEMINI_API_KEY`: Your Google AI Studio (Gemini) API key.
-   `PREFERRED_PROVIDER`: `openai` (default) or `google`.
-   `BIG_MODEL`: Model for `sonnet` requests (e.g., `gpt-4.1` or `gemini-2.5-pro-preview-03-25`).
-   `SMALL_MODEL`: Model for `haiku` requests (e.g., `gpt-4.1-mini` or `gemini-2.0-flash`).