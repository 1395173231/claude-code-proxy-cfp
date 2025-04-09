# Claude Code but with OpenAI Models 🧙‍♂️🔄 ¯\\_(ツ)_/¯

**Use Claude Code with OpenAI Models** 🤝

A proxy server that lets you use Claude Code with OpenAI models like GPT-4o / gpt-4.5 and o3-mini. 🌉


![Claude Code but with OpenAI Models](pic.png)

## Quick Start ⚡

### Prerequisites

- OpenAI API key 🔑

### Setup 🛠️

1. **Clone this repository**:
   ```bash
   git clone https://github.com/1rgs/claude-code-openai.git
   cd claude-code-openai
   ```

2. **Install UV**:
   ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. **Configure your API keys**:
   Create a `.env` file with:
   ```
   OPENAI_API_KEY=your-openai-key
   # Optional: customize which models are used
   # For OpenAI models (default)
   # BIG_MODEL=gpt-4o
   # SMALL_MODEL=gpt-4o-mini
   
   # For Gemini models
   # BIG_MODEL=gemini-2.5-pro-preview-03-25
   # SMALL_MODEL=gemini-2.0-flash
   ```

4. **Start the proxy server**:
   ```bash
   uv run uvicorn server:app --host 0.0.0.0 --port 8082
   ```

### Using with Claude Code 🎮

1. **Install Claude Code** (if you haven't already):
   ```bash
   npm install -g @anthropic-ai/claude-code
   ```

2. **Connect to your proxy**:
   ```bash
   ANTHROPIC_BASE_URL=http://localhost:8082 claude
   ```

3. **That's it!** Your Claude Code client will now use OpenAI models through the proxy. 🎯

## Model Mapping 🗺️

The proxy automatically maps Claude models to either OpenAI or Gemini models based on the configured model:

| Claude Model | Default Mapping | When BIG_MODEL/SMALL_MODEL is a Gemini model |
|--------------|--------------|---------------------------|
| haiku | openai/gpt-4o-mini | gemini/[model-name] |
| sonnet | openai/gpt-4o | gemini/[model-name] |

### Supported Models

#### OpenAI Models
The following OpenAI models are supported with automatic `openai/` prefix handling:
- o3-mini
- o1
- o1-mini
- o1-pro
- gpt-4.5-preview
- gpt-4o
- gpt-4o-audio-preview
- chatgpt-4o-latest
- gpt-4o-mini
- gpt-4o-mini-audio-preview

#### Gemini Models
The following Gemini models are supported with automatic `gemini/` prefix handling:
- gemini-2.5-pro-preview-03-25
- gemini-2.0-flash

### Model Prefix Handling
The proxy automatically adds the appropriate prefix to model names:
- OpenAI models get the `openai/` prefix 
- Gemini models get the `gemini/` prefix
- The BIG_MODEL and SMALL_MODEL will get the appropriate prefix based on whether they're in the OpenAI or Gemini model lists

For example:
- `gpt-4o` becomes `openai/gpt-4o`
- `gemini-2.5-pro-preview-03-25` becomes `gemini/gemini-2.5-pro-preview-03-25`
- When BIG_MODEL is set to a Gemini model, Claude Sonnet will map to `gemini/[model-name]`

### Customizing Model Mapping

You can customize which models are used via environment variables:

- `BIG_MODEL`: The model to use for Claude Sonnet models (default: "gpt-4o")
- `SMALL_MODEL`: The model to use for Claude Haiku models (default: "gpt-4o-mini")

Add these to your `.env` file to customize:
```
OPENAI_API_KEY=your-openai-key
# For OpenAI models (default)
BIG_MODEL=gpt-4o
SMALL_MODEL=gpt-4o-mini

# For Gemini models
# BIG_MODEL=gemini-2.5-pro-preview-03-25
# SMALL_MODEL=gemini-2.0-flash
```

Or set them directly when running the server:
```bash
# Using OpenAI models
BIG_MODEL=gpt-4o SMALL_MODEL=gpt-4o-mini uv run uvicorn server:app --host 0.0.0.0 --port 8082

# Using Gemini models
BIG_MODEL=gemini-2.5-pro-preview-03-25 SMALL_MODEL=gemini-2.0-flash uv run uvicorn server:app --host 0.0.0.0 --port 8082
```

To use a mix of OpenAI and Gemini models:
```bash
BIG_MODEL=gemini-2.5-pro-preview-03-25 SMALL_MODEL=gpt-4o-mini uv run uvicorn server:app --host 0.0.0.0 --port 8082
```

## How It Works 🧩

This proxy works by:

1. **Receiving requests** in Anthropic's API format 📥
2. **Translating** the requests to OpenAI format via LiteLLM 🔄
3. **Sending** the translated request to OpenAI 📤
4. **Converting** the response back to Anthropic format 🔄
5. **Returning** the formatted response to the client ✅

The proxy handles both streaming and non-streaming responses, maintaining compatibility with all Claude clients. 🌊

## Contributing 🤝

Contributions are welcome! Please feel free to submit a Pull Request. 🎁
