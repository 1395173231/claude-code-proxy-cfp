from fastapi import FastAPI, Request, HTTPException
import uvicorn
import logging
import json
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional, Union, Literal
import httpx
import os
from fastapi.responses import JSONResponse, StreamingResponse
import litellm
import uuid
import time
import sys
from dotenv import load_dotenv
from cfp_adapter import build_cfp_messages, adapt_request_for_cfp, adapt_response_from_cfp, parse_cfp_response
if os.environ.get("DEBUG","").lower() == "true":
    litellm._turn_on_debug()
# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.WARN,  # Change to INFO level to show more details
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# Configure uvicorn to be quieter
import uvicorn
# Tell uvicorn's loggers to be quiet
logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)

# Create a filter to block any log messages containing specific strings
class MessageFilter(logging.Filter):
    def filter(self, record):
        # Block messages containing these strings
        blocked_phrases = [
            "LiteLLM completion()",
            "HTTP Request:", 
            "selected model name for cost calculation",
            "utils.py",
            "cost_calculator"
        ]
        
        if hasattr(record, 'msg') and isinstance(record.msg, str):
            for phrase in blocked_phrases:
                if phrase in record.msg:
                    return False
        return True

# Apply the filter to the root logger to catch all messages
root_logger = logging.getLogger()
root_logger.addFilter(MessageFilter())

# Custom formatter for model mapping logs
class ColorizedFormatter(logging.Formatter):
    """Custom formatter to highlight model mappings"""
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    
    def format(self, record):
        if record.levelno == logging.debug and "MODEL MAPPING" in record.msg:
            # Apply colors and formatting to model mapping logs
            return f"{self.BOLD}{self.GREEN}{record.msg}{self.RESET}"
        return super().format(record)

# Apply custom formatter to console handler
for handler in logger.handlers:
    if isinstance(handler, logging.StreamHandler):
        handler.setFormatter(ColorizedFormatter('%(asctime)s - %(levelname)s - %(message)s'))

app = FastAPI()

# Get API keys from environment
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Get preferred provider (default to openai)
PREFERRED_PROVIDER = os.environ.get("PREFERRED_PROVIDER", "openai").lower()

# Get model mapping configuration from environment
# Default to latest OpenAI models if not set
BIG_MODEL = os.environ.get("BIG_MODEL", "gpt-4.1")
SMALL_MODEL = os.environ.get("SMALL_MODEL", "gpt-4.1-mini")

# List of OpenAI models
OPENAI_MODELS = [
    "o3-mini",
    "o1",
    "o1-mini",
    "o1-pro",
    "gpt-4.5-preview",
    "gpt-4o",
    "gpt-4o-audio-preview",
    "chatgpt-4o-latest",
    "gpt-4o-mini",
    "gpt-4o-mini-audio-preview",
    "gpt-4.1",  # Added default big model
    "gpt-4.1-mini", # Added default small model
    "kimi-k2-0711-preview",
    "moonshot-v1-32k",
    "claude-3.7-sonnet",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "claude-4-sonnet",
    "gpt-4o-all",
    "o3",
    "o4-mini"
]

# List of Gemini models
GEMINI_MODELS = [
    "gemini-2.5-pro-preview-03-25",
    "gemini-2.0-flash",
]

# Helper function to clean schema for Gemini
def clean_gemini_schema(schema: Any) -> Any:
    """Recursively removes unsupported fields from a JSON schema for Gemini."""
    if isinstance(schema, dict):
        # Remove specific keys unsupported by Gemini tool parameters
        schema.pop("additionalProperties", None)
        schema.pop("default", None)

        # Check for unsupported 'format' in string types
        if schema.get("type") == "string" and "format" in schema:
            allowed_formats = {"enum", "date-time"}
            if schema["format"] not in allowed_formats:
                logger.debug(f"Removing unsupported format '{schema['format']}' for string type in Gemini schema.")
                schema.pop("format")

        # Recursively clean nested schemas (properties, items, etc.)
        for key, value in list(schema.items()): # Use list() to allow modification during iteration
            schema[key] = clean_gemini_schema(value)
    elif isinstance(schema, list):
        # Recursively clean items in a list
        return [clean_gemini_schema(item) for item in schema]
    return schema

# Models for Anthropic API requests
class ContentBlockText(BaseModel):
    type: Literal["text"]
    text: str

class ContentBlockImage(BaseModel):
    type: Literal["image"]
    source: Dict[str, Any]

class ContentBlockToolUse(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: Dict[str, Any]

class ContentBlockToolResult(BaseModel):
    type: Literal["tool_result"]
    tool_use_id: str
    content: Union[str, List[Dict[str, Any]], Dict[str, Any], List[Any], Any]

class SystemContent(BaseModel):
    type: Literal["text"]
    text: str

class Message(BaseModel):
    role: Literal["user", "assistant","system"]
    content: Union[str, List[Union[ContentBlockText, ContentBlockImage, ContentBlockToolUse, ContentBlockToolResult]]]

class Tool(BaseModel):
    name: str
    description: Optional[str] = None
    input_schema: Dict[str, Any]

class ThinkingConfig(BaseModel):
    enabled: bool


class MessagesRequest(BaseModel):
    model: str
    max_tokens: int
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    stop_sequences: Optional[List[str]] = None
    stream: Optional[bool] = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
    tools: Optional[List[Tool]] = None
    tool_choice: Optional[Dict[str, Any]] = None
    thinking: Optional[ThinkingConfig] = None
    original_model: Optional[str] = None  # Will store the original model name
    _cfp_enabled: Optional[bool] = False  # Track if CFP is enabled for this model

    @field_validator('model')
    def validate_model_field(cls, v, info):
        original_model = v
        new_model = v  # Default to original value

        logger.debug(
            f"📋 MODEL VALIDATION: Original='{original_model}', Preferred='{PREFERRED_PROVIDER}', BIG='{BIG_MODEL}', SMALL='{SMALL_MODEL}'")

        # Remove provider prefixes for easier matching
        clean_v = v
        if clean_v.startswith('anthropic/'):
            clean_v = clean_v[10:]
        elif clean_v.startswith('openai/'):
            clean_v = clean_v[7:]
        elif clean_v.startswith('gemini/'):
            clean_v = clean_v[7:]

        # --- Simplified Mapping Logic --- START ---
        mapped = False

        # Map Haiku to SMALL_MODEL
        if 'haiku' in clean_v.lower():
            # If SMALL_MODEL already has a provider prefix, use it directly
            if SMALL_MODEL.startswith(('openai/', 'gemini/', 'anthropic/')):
                new_model = SMALL_MODEL
            else:
                # Otherwise, use PREFERRED_PROVIDER logic
                if PREFERRED_PROVIDER in ["google", "gemini"]:
                    new_model = f"gemini/{SMALL_MODEL}"
                elif PREFERRED_PROVIDER == "anthropic":
                    new_model = f"anthropic/{SMALL_MODEL}"
                else:
                    new_model = f"openai/{SMALL_MODEL}"
            mapped = True

        # Map Sonnet to BIG_MODEL
        elif 'sonnet' in clean_v.lower():
            # If BIG_MODEL already has a provider prefix, use it directly
            if BIG_MODEL.startswith(('openai/', 'gemini/', 'anthropic/')):
                new_model = BIG_MODEL
            else:
                # Otherwise, use PREFERRED_PROVIDER logic
                if PREFERRED_PROVIDER in ["google", "gemini"]:
                    new_model = f"gemini/{BIG_MODEL}"
                elif PREFERRED_PROVIDER == "anthropic":
                    new_model = f"anthropic/{BIG_MODEL}"
                else:
                    new_model = f"openai/{BIG_MODEL}"
            mapped = True

        # Add prefixes based on PREFERRED_PROVIDER if no specific mapping
        elif not mapped:
            if not v.startswith(('openai/', 'gemini/', 'anthropic/')):
                if PREFERRED_PROVIDER in ["google", "gemini"]:
                    new_model = f"gemini/{clean_v}"
                elif PREFERRED_PROVIDER == "anthropic":
                    new_model = f"anthropic/{clean_v}"
                else:  # Default to openai
                    new_model = f"openai/{clean_v}"
                mapped = True
        # --- Simplified Mapping Logic --- END ---

        if mapped:
            logger.debug(f"📌 MODEL MAPPING: '{original_model}' ➡️ '{new_model}'")
        else:
            logger.debug(f"📌 MODEL: No mapping needed for '{original_model}'")

        # Check if model has CFP-enabled flags (-textonly, -cfp, -text)
        cfp_enabled = any(suffix in new_model for suffix in ['-textonly', '-cfp', '-text'])
        if cfp_enabled:
            # Remove the CFP flag from the model name for actual API call
            for suffix in ['-textonly', '-cfp', '-text']:
                new_model = new_model.replace(suffix, '')

        # Store the original model and CFP status in the values dictionary
        values = info.data
        if isinstance(values, dict):
            values['original_model'] = original_model
            values['_cfp_enabled'] = cfp_enabled

        return new_model


class TokenCountRequest(BaseModel):
    model: str
    messages: List[Message]
    system: Optional[Union[str, List[SystemContent]]] = None
    tools: Optional[List[Tool]] = None
    thinking: Optional[ThinkingConfig] = None
    tool_choice: Optional[Dict[str, Any]] = None
    original_model: Optional[str] = None  # Will store the original model name
    _cfp_enabled: Optional[bool] = False  # Track if CFP is enabled for this model

    @field_validator('model')
    def validate_model_token_count(cls, v, info):
        original_model = v
        new_model = v  # Default to original value

        logger.debug(
            f"📋 TOKEN COUNT VALIDATION: Original='{original_model}', Preferred='{PREFERRED_PROVIDER}', BIG='{BIG_MODEL}', SMALL='{SMALL_MODEL}'")

        # Remove provider prefixes for easier matching
        clean_v = v
        if clean_v.startswith('anthropic/'):
            clean_v = clean_v[10:]
        elif clean_v.startswith('openai/'):
            clean_v = clean_v[7:]
        elif clean_v.startswith('gemini/'):
            clean_v = clean_v[7:]

        # --- Simplified Mapping Logic --- START ---
        mapped = False

        # Map Haiku to SMALL_MODEL
        if 'haiku' in clean_v.lower():
            # If SMALL_MODEL already has a provider prefix, use it directly
            if SMALL_MODEL.startswith(('openai/', 'gemini/', 'anthropic/')):
                new_model = SMALL_MODEL
            else:
                # Otherwise, use PREFERRED_PROVIDER logic
                if PREFERRED_PROVIDER in ["google", "gemini"]:
                    new_model = f"gemini/{SMALL_MODEL}"
                elif PREFERRED_PROVIDER == "anthropic":
                    new_model = f"anthropic/{SMALL_MODEL}"
                else:
                    new_model = f"openai/{SMALL_MODEL}"
            mapped = True

        # Map Sonnet to BIG_MODEL
        elif 'sonnet' in clean_v.lower():
            # If BIG_MODEL already has a provider prefix, use it directly
            if BIG_MODEL.startswith(('openai/', 'gemini/', 'anthropic/')):
                new_model = BIG_MODEL
            else:
                # Otherwise, use PREFERRED_PROVIDER logic
                if PREFERRED_PROVIDER in ["google", "gemini"]:
                    new_model = f"gemini/{BIG_MODEL}"
                elif PREFERRED_PROVIDER == "anthropic":
                    new_model = f"anthropic/{BIG_MODEL}"
                else:
                    new_model = f"openai/{BIG_MODEL}"
            mapped = True

        # Add prefixes based on PREFERRED_PROVIDER if no specific mapping
        elif not mapped:
            if not v.startswith(('openai/', 'gemini/', 'anthropic/')):
                if PREFERRED_PROVIDER in ["google", "gemini"]:
                    new_model = f"gemini/{clean_v}"
                elif PREFERRED_PROVIDER == "anthropic":
                    new_model = f"anthropic/{clean_v}"
                else:  # Default to openai
                    new_model = f"openai/{clean_v}"
                mapped = True
        # --- Simplified Mapping Logic --- END ---

        if mapped:
            logger.debug(f"📌 TOKEN COUNT MAPPING: '{original_model}' ➡️ '{new_model}'")
        else:
            logger.debug(f"📌 TOKEN COUNT: No mapping needed for '{original_model}'")

        # Check if model has CFP-enabled flags (-textonly, -cfp, -text)
        cfp_enabled = any(suffix in new_model for suffix in ['-textonly', '-cfp', '-text'])
        if cfp_enabled:
            # Remove the CFP flag from the model name for actual API call
            for suffix in ['-textonly', '-cfp', '-text']:
                new_model = new_model.replace(suffix, '')

        # Store the original model and CFP status in the values dictionary
        values = info.data
        if isinstance(values, dict):
            values['original_model'] = original_model
            values['_cfp_enabled'] = cfp_enabled

        return new_model


class TokenCountResponse(BaseModel):
    input_tokens: int

class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

class MessagesResponse(BaseModel):
    id: str
    model: str
    role: Literal["assistant"] = "assistant"
    content: List[Union[ContentBlockText, ContentBlockToolUse]]
    type: Literal["message"] = "message"
    stop_reason: Optional[Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]] = None
    stop_sequence: Optional[str] = None
    usage: Usage

@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Get request details
    method = request.method
    path = request.url.path
    
    # Log only basic request details at debug level
    logger.debug(f"Request: {method} {path}")
    
    # Process the request and get the response
    response = await call_next(request)
    
    return response

# Not using validation function as we're using the environment API key

def parse_tool_result_content(content):
    """Helper function to properly parse and normalize tool result content."""
    if content is None:
        return "No content provided"
        
    if isinstance(content, str):
        return content
        
    if isinstance(content, list):
        result = ""
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                result += item.get("text", "") + "\n"
            elif isinstance(item, str):
                result += item + "\n"
            elif isinstance(item, dict):
                if "text" in item:
                    result += item.get("text", "") + "\n"
                else:
                    try:
                        result += json.dumps(item) + "\n"
                    except:
                        result += str(item) + "\n"
            else:
                try:
                    result += str(item) + "\n"
                except:
                    result += "Unparseable content\n"
        return result.strip()
        
    if isinstance(content, dict):
        if content.get("type") == "text":
            return content.get("text", "")
        try:
            return json.dumps(content)
        except:
            return str(content)
            
    # Fallback for any other type
    try:
        return str(content)
    except:
        return "Unparseable content"

def convert_anthropic_to_litellm(anthropic_request: MessagesRequest) -> Dict[str, Any]:
    """Convert Anthropic API request format to LiteLLM format (which follows OpenAI)."""
    # LiteLLM already handles Anthropic models when using the format model="anthropic/claude-3-opus-20240229"
    # So we just need to convert our Pydantic model to a dict in the expected format
    
    messages = []
    
    # Add system message if present
    if anthropic_request.system:
        # Handle different formats of system messages
        if isinstance(anthropic_request.system, str):
            # Simple string format
            messages.append({"role": "system", "content": anthropic_request.system})
        elif isinstance(anthropic_request.system, list):
            # List of content blocks
            system_text = ""
            for block in anthropic_request.system:
                if hasattr(block, 'type') and block.type == "text":
                    system_text += block.text + "\n\n"
                elif isinstance(block, dict) and block.get("type") == "text":
                    system_text += block.get("text", "") + "\n\n"
            
            if system_text:
                messages.append({"role": "system", "content": system_text.strip()})
    
    # Add conversation messages
    for idx, msg in enumerate(anthropic_request.messages):
        content = msg.content
        if isinstance(content, str):
            messages.append({"role": msg.role, "content": content})
        else:
            # Special handling for tool_result in user messages
            # OpenAI/LiteLLM format expects the assistant to call the tool, 
            # and the user's next message to include the result as plain text
            if msg.role == "user" and any(block.type == "tool_result" for block in content if hasattr(block, "type")):
                # For user messages with tool_result, split into separate messages
                text_content = ""
                
                # Extract all text parts and concatenate them
                for block in content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            text_content += block.text + "\n"
                        elif block.type == "tool_result":
                            # Add tool result as a message by itself - simulate the normal flow
                            tool_id = block.tool_use_id if hasattr(block, "tool_use_id") else ""
                            
                            # Handle different formats of tool result content
                            result_content = ""
                            if hasattr(block, "content"):
                                if isinstance(block.content, str):
                                    result_content = block.content
                                elif isinstance(block.content, list):
                                    # If content is a list of blocks, extract text from each
                                    for content_block in block.content:
                                        if hasattr(content_block, "type") and content_block.type == "text":
                                            result_content += content_block.text + "\n"
                                        elif isinstance(content_block, dict) and content_block.get("type") == "text":
                                            result_content += content_block.get("text", "") + "\n"
                                        elif isinstance(content_block, dict):
                                            # Handle any dict by trying to extract text or convert to JSON
                                            if "text" in content_block:
                                                result_content += content_block.get("text", "") + "\n"
                                            else:
                                                try:
                                                    result_content += json.dumps(content_block) + "\n"
                                                except:
                                                    result_content += str(content_block) + "\n"
                                elif isinstance(block.content, dict):
                                    # Handle dictionary content
                                    if block.content.get("type") == "text":
                                        result_content = block.content.get("text", "")
                                    else:
                                        try:
                                            result_content = json.dumps(block.content)
                                        except:
                                            result_content = str(block.content)
                                else:
                                    # Handle any other type by converting to string
                                    try:
                                        result_content = str(block.content)
                                    except:
                                        result_content = "Unparseable content"
                            
                            # In OpenAI format, tool results come from the user (rather than being content blocks)
                            text_content += f"Tool result for {tool_id}:\n{result_content}\n"
                
                # Add as a single user message with all the content
                messages.append({"role": "user", "content": text_content.strip()})
            else:
                # Regular handling for other message types
                processed_content = []
                for block in content:
                    if hasattr(block, "type"):
                        if block.type == "text":
                            processed_content.append({"type": "text", "text": block.text})
                        elif block.type == "image":
                            processed_content.append({"type": "image", "source": block.source})
                        elif block.type == "tool_use":
                            # Handle tool use blocks if needed
                            processed_content.append({
                                "type": "tool_use",
                                "id": block.id,
                                "name": block.name,
                                "input": block.input
                            })
                        elif block.type == "tool_result":
                            # Handle different formats of tool result content
                            processed_content_block = {
                                "type": "tool_result",
                                "tool_use_id": block.tool_use_id if hasattr(block, "tool_use_id") else ""
                            }
                            
                            # Process the content field properly
                            if hasattr(block, "content"):
                                if isinstance(block.content, str):
                                    # If it's a simple string, create a text block for it
                                    processed_content_block["content"] = [{"type": "text", "text": block.content}]
                                elif isinstance(block.content, list):
                                    # If it's already a list of blocks, keep it
                                    processed_content_block["content"] = block.content
                                else:
                                    # Default fallback
                                    processed_content_block["content"] = [{"type": "text", "text": str(block.content)}]
                            else:
                                # Default empty content
                                processed_content_block["content"] = [{"type": "text", "text": ""}]
                                
                            processed_content.append(processed_content_block)
                
                messages.append({"role": msg.role, "content": processed_content})
    
    # Cap max_tokens for OpenAI models to their limit of 16384
    max_tokens = anthropic_request.max_tokens
    if anthropic_request.model.startswith("openai/") or anthropic_request.model.startswith("gemini/"):
        max_tokens = min(max_tokens, 16384)
        logger.debug(f"Capping max_tokens to 16384 for OpenAI/Gemini model (original value: {anthropic_request.max_tokens})")
    
    # Create LiteLLM request dict
    litellm_request = {
        "model": anthropic_request.model,  # t understands "anthropic/claude-x" format
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": anthropic_request.temperature,
        "stream": anthropic_request.stream,
    }
    
    # Add optional parameters if present
    if anthropic_request.stop_sequences:
        litellm_request["stop"] = anthropic_request.stop_sequences
    
    if anthropic_request.top_p:
        litellm_request["top_p"] = anthropic_request.top_p
    
    if anthropic_request.top_k:
        litellm_request["top_k"] = anthropic_request.top_k
    
    # Convert tools to OpenAI format
    if anthropic_request.tools:
        openai_tools = []
        is_gemini_model = anthropic_request.model.startswith("gemini/")

        for tool in anthropic_request.tools:
            # Convert to dict if it's a pydantic model
            if hasattr(tool, 'dict'):
                tool_dict = tool.dict()
            else:
                # Ensure tool_dict is a dictionary, handle potential errors if 'tool' isn't dict-like
                try:
                    tool_dict = dict(tool) if not isinstance(tool, dict) else tool
                except (TypeError, ValueError):
                     logger.error(f"Could not convert tool to dict: {tool}")
                     continue # Skip this tool if conversion fails

            # Clean the schema if targeting a Gemini model
            input_schema = tool_dict.get("input_schema", {})
            if is_gemini_model:
                 logger.debug(f"Cleaning schema for Gemini tool: {tool_dict.get('name')}")
                 input_schema = clean_gemini_schema(input_schema)

            # Create OpenAI-compatible function tool
            openai_tool = {
                "type": "function",
                "function": {
                    "name": tool_dict["name"],
                    "description": tool_dict.get("description", ""),
                    "parameters": input_schema # Use potentially cleaned schema
                }
            }
            openai_tools.append(openai_tool)

        litellm_request["tools"] = openai_tools
    
    # Convert tool_choice to OpenAI format if present
    if anthropic_request.tool_choice:
        if hasattr(anthropic_request.tool_choice, 'dict'):
            tool_choice_dict = anthropic_request.tool_choice.dict()
        else:
            tool_choice_dict = anthropic_request.tool_choice
            
        # Handle Anthropic's tool_choice format
        choice_type = tool_choice_dict.get("type")
        if choice_type == "auto":
            litellm_request["tool_choice"] = "auto"
        elif choice_type == "any":
            litellm_request["tool_choice"] = "any"
        elif choice_type == "tool" and "name" in tool_choice_dict:
            litellm_request["tool_choice"] = {
                "type": "function",
                "function": {"name": tool_choice_dict["name"]}
            }
        else:
            # Default to auto if we can't determine
            litellm_request["tool_choice"] = "auto"
    
    return litellm_request

def convert_litellm_to_anthropic(litellm_response: Union[Dict[str, Any], Any], 
                                 original_request: MessagesRequest) -> MessagesResponse:
    """Convert LiteLLM (OpenAI format) response to Anthropic API response format."""
    
    # Enhanced response extraction with better error handling
    try:
        # Get the clean model name to check capabilities
        clean_model = original_request.model
        if clean_model.startswith("anthropic/"):
            clean_model = clean_model[len("anthropic/"):]
        elif clean_model.startswith("openai/"):
            clean_model = clean_model[len("openai/"):]
        
        # Check if this is a Claude model (which supports content blocks)
        is_claude_model = clean_model.startswith("claude-")
        
        # Handle ModelResponse object from LiteLLM
        if hasattr(litellm_response, 'choices') and hasattr(litellm_response, 'usage'):
            # Extract data from ModelResponse object directly
            choices = litellm_response.choices
            message = choices[0].message if choices and len(choices) > 0 else None
            content_text = message.content if message and hasattr(message, 'content') else ""
            tool_calls = message.tool_calls if message and hasattr(message, 'tool_calls') else None
            finish_reason = choices[0].finish_reason if choices and len(choices) > 0 else "stop"
            usage_info = litellm_response.usage
            response_id = getattr(litellm_response, 'id', f"msg_{uuid.uuid4()}")
        else:
            # For backward compatibility - handle dict responses
            # If response is a dict, use it, otherwise try to convert to dict
            try:
                response_dict = litellm_response if isinstance(litellm_response, dict) else litellm_response.dict()
            except AttributeError:
                # If .dict() fails, try to use model_dump or __dict__ 
                try:
                    response_dict = litellm_response.model_dump() if hasattr(litellm_response, 'model_dump') else litellm_response.__dict__
                except AttributeError:
                    # Fallback - manually extract attributes
                    response_dict = {
                        "id": getattr(litellm_response, 'id', f"msg_{uuid.uuid4()}"),
                        "choices": getattr(litellm_response, 'choices', [{}]),
                        "usage": getattr(litellm_response, 'usage', {})
                    }
                    
            # Extract the content from the response dict
            choices = response_dict.get("choices", [{}])
            message = choices[0].get("message", {}) if choices and len(choices) > 0 else {}
            content_text = message.get("content", "")
            tool_calls = message.get("tool_calls", None)
            finish_reason = choices[0].get("finish_reason", "stop") if choices and len(choices) > 0 else "stop"
            usage_info = response_dict.get("usage", {})
            response_id = response_dict.get("id", f"msg_{uuid.uuid4()}")
        
        # Create content list for Anthropic format
        content = []
        
        # Add text content block if present (text might be None or empty for pure tool call responses)
        if content_text is not None and content_text != "":
            content.append({"type": "text", "text": content_text})
        
        # Add tool calls if present (tool_use in Anthropic format)
        if tool_calls:
            # --- 新增：CFP 路径下所有模型都输出 tool_use 块 ---
            if getattr(litellm_response, "_from_cfp", False):
                logger.debug("CFP tool_calls ➜ Anthropic tool_use (all models)")
                if not isinstance(tool_calls, list):
                    tool_calls = [tool_calls]
                for tc in tool_calls:
                    fn  = tc.get("function", {})
                    args = fn.get("arguments", "{}")
                    # 解析字符串 JSON
                    try:
                        args = json.loads(args) if isinstance(args, str) else args
                    except json.JSONDecodeError:
                        args = {"raw": args}
                    content.append({
                        "type": "tool_use",
                        "id": tc.get("id", f"tool_{uuid.uuid4()}"),
                        "name": fn.get("name", ""),
                        "input": args
                    })
                # 让 stop_reason 成为 "tool_use"
                if finish_reason == "tool_calls":
                    finish_reason = "tool_use"
            # --- 旧逻辑：仅 Claude 输出 tool_use，其它降级为文本 ---
            elif is_claude_model:
                logger.debug(f"Processing tool calls: {tool_calls}")
                if not isinstance(tool_calls, list):
                    tool_calls = [tool_calls]
                for idx, tool_call in enumerate(tool_calls):
                    logger.debug(f"Processing tool call {idx}: {tool_call}")
                    if isinstance(tool_call, dict):
                        function = tool_call.get("function", {})
                        tool_id = tool_call.get("id", f"tool_{uuid.uuid4()}")
                        name = function.get("name", "")
                        arguments = function.get("arguments", "{}")
                    else:
                        function = getattr(tool_call, "function", None)
                        tool_id = getattr(tool_call, "id", f"tool_{uuid.uuid4()}")
                        name = getattr(function, "name", "") if function else ""
                        arguments = getattr(function, "arguments", "{}") if function else "{}"
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse tool arguments as JSON: {arguments}")
                            arguments = {"raw": arguments}
                    logger.debug(f"Adding tool_use block: id={tool_id}, name={name}, input={arguments}")
                    content.append({
                        "type": "tool_use",
                        "id": tool_id,
                        "name": name,
                        "input": arguments
                    })
            else:
                # For non-Claude models, convert tool calls to text format
                logger.debug(f"Converting tool calls to text for non-Claude model: {clean_model}")
                tool_text = "\n\nTool usage:\n"
                if not isinstance(tool_calls, list):
                    tool_calls = [tool_calls]
                for idx, tool_call in enumerate(tool_calls):
                    if isinstance(tool_call, dict):
                        function = tool_call.get("function", {})
                        tool_id = tool_call.get("id", f"tool_{uuid.uuid4()}")
                        name = function.get("name", "")
                        arguments = function.get("arguments", "{}")
                    else:
                        function = getattr(tool_call, "function", None)
                        tool_id = getattr(tool_call, "id", f"tool_{uuid.uuid4()}")
                        name = getattr(function, "name", "") if function else ""
                        arguments = getattr(function, "arguments", "{}") if function else "{}"
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse tool arguments as JSON: {arguments}")
                            arguments = {"raw": arguments}
                    arguments_str = json.dumps(arguments, indent=2)
                    tool_text += f"Tool: {name}\nArguments: {arguments_str}\n\n"
                if content and content[0]["type"] == "text":
                    content[0]["text"] += tool_text
                else:
                    content.append({"type": "text", "text": tool_text})
        
        # Get usage information - extract values safely from object or dict
        if isinstance(usage_info, dict):
            prompt_tokens = usage_info.get("prompt_tokens", 0)
            completion_tokens = usage_info.get("completion_tokens", 0)
        else:
            prompt_tokens = getattr(usage_info, "prompt_tokens", 0)
            completion_tokens = getattr(usage_info, "completion_tokens", 0)
        
        # Map OpenAI finish_reason to Anthropic stop_reason
        stop_reason = None
        if finish_reason == "stop":
            stop_reason = "end_turn"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        elif finish_reason == "tool_calls":
            stop_reason = "tool_use"
        else:
            stop_reason = "end_turn"  # Default
        
        # Make sure content is never empty
        if not content:
            content.append({"type": "text", "text": ""})
        
        # Create Anthropic-style response
        anthropic_response = MessagesResponse(
            id=response_id,
            model=original_request.model,
            role="assistant",
            content=content,
            stop_reason=stop_reason,
            stop_sequence=None,
            usage=Usage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens
            )
        )
        
        return anthropic_response
        
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        error_message = f"Error converting response: {str(e)}\n\nFull traceback:\n{error_traceback}"
        logger.error(error_message)
        
        # In case of any error, create a fallback response
        return MessagesResponse(
            id=f"msg_{uuid.uuid4()}",
            model=original_request.model,
            role="assistant",
            content=[{"type": "text", "text": f"Error converting response: {str(e)}. Please check server logs."}],
            stop_reason="end_turn",
            usage=Usage(input_tokens=0, output_tokens=0)
        )


async def handle_streaming(response_generator, original_request: MessagesRequest, cfp_used: bool = False):
    """Handle streaming responses from LiteLLM and convert to Anthropic format."""
    import json
    try:
        import uuid
        message_id = f"msg_{uuid.uuid4().hex[:24]}"
        message_data = {
            'type': 'message_start',
            'message': {
                'id': message_id,
                'type': 'message',
                'role': 'assistant',
                'model': original_request.model,
                'content': [],
                'stop_reason': None,
                'stop_sequence': None,
                'usage': {
                    'input_tokens': 0,
                    'cache_creation_input_tokens': 0,
                    'cache_read_input_tokens': 0,
                    'output_tokens': 0
                }
            }
        }
        yield f"event: message_start\ndata: {json.dumps(message_data)}\n\n"

        tool_index = None
        current_tool_call = None
        tool_content = ""
        accumulated_text = ""
        text_sent = False
        text_block_closed = False
        input_tokens = 0
        output_tokens = 0
        has_sent_stop_reason = False
        last_tool_index = 0
        cfp_buffer = ""
        cfp_text_sent = False
        text_block_started = False

        sse_interrupt = True

        # 用于跟踪工具调用状态
        tool_calls_in_progress = {}  # {index: {id, name, arguments}}

        # CFP v2 增量解析器 - 修改导入方式
        from cfp_adapter import CFPStreamParser
        cfp_parser = CFPStreamParser()
        cfp_active_calls = {}  # {call_id: {index, anthropic_id, name, arguments_buffer}}
        cfp_call_index = 0
        cfp_has_tool_calls = False
        cfp_text_accumulated = ""
        finish_reason = "stop"
        async for chunk in response_generator:
            try:
                if hasattr(chunk, 'usage') and chunk.usage is not None:
                    if hasattr(chunk.usage, 'prompt_tokens'):
                        input_tokens = chunk.usage.prompt_tokens
                    if hasattr(chunk.usage, 'completion_tokens'):
                        output_tokens = chunk.usage.completion_tokens

                if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                    choice = chunk.choices[0]
                    if hasattr(choice, 'delta'):
                        delta = choice.delta
                    else:
                        delta = getattr(choice, 'message', {})
                    finish_reason = getattr(choice, 'finish_reason', None)

                    delta_content = None
                    if hasattr(delta, 'content'):
                        delta_content = delta.content
                        if os.environ.get("DEBUG", "").lower() == "true":  # 修复环境变量检查
                            with open("data/chunk_old_data.txt", "a", encoding="utf-8") as f:
                                f.write(delta_content+"\n")
                    elif isinstance(delta, dict) and 'content' in delta:
                        delta_content = delta['content']

                    # 处理工具调用
                    tool_calls = None
                    if hasattr(delta, 'tool_calls'):
                        tool_calls = delta.tool_calls
                    elif isinstance(delta, dict) and 'tool_calls' in delta:
                        tool_calls = delta['tool_calls']

                    # ============ 处理上游直接返回的 tool_calls ============
                    if tool_calls and not cfp_used:
                        for tool_call in tool_calls:
                            # 获取工具调用的基本信息
                            tool_id = getattr(tool_call, 'id', '') or f'toolu_{uuid.uuid4().hex[:24]}'
                            tool_type = getattr(tool_call, 'type', 'function')
                            tool_index = getattr(tool_call, 'index', 0)

                            if tool_type == 'function' and hasattr(tool_call, 'function'):
                                function = tool_call.function
                                function_name = getattr(function, 'name', '')
                                function_arguments = getattr(function, 'arguments', '')

                                # 如果是新的工具调用，初始化状态
                                if tool_index not in tool_calls_in_progress:
                                    tool_calls_in_progress[tool_index] = {
                                        'id': tool_id,
                                        'name': function_name,
                                        'arguments': '',
                                        'started': False
                                    }

                                # 更新工具调用状态
                                tool_call_state = tool_calls_in_progress[tool_index]
                                if function_name and not tool_call_state['name']:
                                    tool_call_state['name'] = function_name
                                if function_arguments:
                                    tool_call_state['arguments'] += function_arguments

                                # 发送 content_block_start 事件（只发送一次）
                                if not tool_call_state['started']:
                                    tool_call_state['started'] = True
                                    # 确保先关闭文本块
                                    if text_block_started and not text_block_closed:
                                        text_block_closed = True
                                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

                                    # 计算实际的块索引
                                    actual_index = tool_index + (1 if text_block_started else 0)
                                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': actual_index, 'content_block': {'type': 'tool_use', 'id': tool_id, 'name': function_name, 'input': {}}})}\n\n"

                                # 发送增量数据
                                if function_arguments:
                                    actual_index = tool_index + (1 if text_block_started else 0)
                                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': actual_index, 'delta': {'type': 'input_json_delta', 'partial_json': function_arguments}})}\n\n"

                        # 如果有工具调用，设置完成原因
                        if tool_calls:
                            finish_reason = "tool_use"

                    elif cfp_used and delta_content is not None and delta_content != "":
                        accumulated_text += delta_content

                        # 使用 CFP v2 解析器处理增量数据
                        try:
                            events = cfp_parser.parse_stream_chunk(delta_content)

                            for event in events:
                                if event["type"] == "call_start":
                                    # 函数调用开始
                                    cfp_has_tool_calls = True
                                    call_id = event["id"]
                                    function_name = event["name"]

                                    # 确保先关闭文本块
                                    if text_block_started and not text_block_closed:
                                        text_block_closed = True
                                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

                                    # 分配新的工具调用索引
                                    actual_index = cfp_call_index + (1 if text_block_started else 0)
                                    anthropic_tool_id = f'toolu_{uuid.uuid4().hex[:24]}'

                                    # 记录活跃的调用
                                    cfp_active_calls[call_id] = {
                                        'index': actual_index,
                                        'anthropic_id': anthropic_tool_id,
                                        'name': function_name,
                                        'arguments_buffer': "",
                                        'started': True
                                    }

                                    # 发送 content_block_start 事件
                                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': actual_index, 'content_block': {'type': 'tool_use', 'id': anthropic_tool_id, 'name': function_name, 'input': {}}})}\n\n"

                                    cfp_call_index += 1

                                elif event["type"] == "args_delta":
                                    # 参数增量
                                    call_id = event["id"]
                                    delta_args = event["delta"]

                                    if call_id in cfp_active_calls:
                                        call_info = cfp_active_calls[call_id]
                                        call_info['arguments_buffer'] += delta_args

                                        # 发送参数增量
                                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': call_info['index'], 'delta': {'type': 'input_json_delta', 'partial_json': delta_args}})}\n\n"

                                elif event["type"] == "call_complete":
                                    # 函数调用完成
                                    call_id = event["id"]

                                    if call_id in cfp_active_calls:
                                        call_info = cfp_active_calls[call_id]

                                        # 发送 content_block_stop 事件
                                        yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': call_info['index']})}\n\n"

                                        # 从活跃调用中移除
                                        del cfp_active_calls[call_id]

                                elif event["type"] == "text":
                                    # 处理文本内容
                                    text_content = event["content"]
                                    if text_content and not cfp_has_tool_calls:
                                        cfp_text_accumulated += text_content

                                        if not text_block_started:
                                            text_block_started = True
                                            yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

                                        yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text_content}})}\n\n"
                                        text_sent = True

                                elif event["type"] == "result":
                                    # 处理函数执行结果
                                    result_content = json.dumps(event["result"], ensure_ascii=False)
                                    if not text_block_started:
                                        text_block_started = True
                                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

                                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': result_content}})}\n\n"
                                    text_sent = True

                        except Exception as cfp_error:
                            logger.error(f"CFP parsing error: {cfp_error}")
                            # CFP 解析失败，累积为文本
                            if not cfp_has_tool_calls:
                                cfp_text_accumulated += delta_content

                                if not text_block_started:
                                    text_block_started = True
                                    yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"

                                yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': delta_content}})}\n\n"
                                text_sent = True

                    # ============ 非 CFP 模式处理普通文本 ============
                    elif not cfp_used and delta_content is not None and delta_content != "" and not tool_calls:
                        accumulated_text += delta_content
                        if tool_index is None and not text_block_closed:
                            if not text_block_started:
                                text_block_started = True
                                yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                            text_sent = True
                            yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': delta_content}})}\n\n"

                    # ============ 处理完成原因 ============
                    if finish_reason and not has_sent_stop_reason:
                        has_sent_stop_reason = True

                        # 完成所有正在进行的工具调用
                        for tool_idx, tool_call_state in tool_calls_in_progress.items():
                            if tool_call_state['started']:
                                actual_index = tool_idx + (1 if text_block_started else 0)
                                yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': actual_index})}\n\n"

                        # 完成所有 CFP 活跃调用
                        for call_id, call_info in cfp_active_calls.items():
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': call_info['index']})}\n\n"

                        # CFP 模式下，如果没有检测到工具调用，则输出纯文本
                        if cfp_used and not cfp_has_tool_calls and accumulated_text.strip():
                            try:
                                # 修改为使用 cfp_adapter 的函数
                                from cfp_adapter import parse_cfp_response
                                plain_text, _ = parse_cfp_response(accumulated_text)
                                if plain_text and plain_text.strip():
                                    if not text_block_started:
                                        text_block_started = True
                                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': plain_text}})}\n\n"
                                    text_sent = True
                            except Exception:
                                # 如果解析失败，使用 cfp_codec 清理文本
                                from cfp_codec import clean_cfp_text
                                clean_text = clean_cfp_text(accumulated_text)
                                if clean_text.strip():
                                    if not text_block_started:
                                        text_block_started = True
                                        yield f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                                    yield f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': clean_text}})}\n\n"
                                    text_sent = True

                        # 关闭文本块
                        if text_sent and not text_block_closed:
                            text_block_closed = True
                            yield f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"

                        stop_reason = "end_turn"
                        if finish_reason == "length":
                            stop_reason = "max_tokens"
                        elif finish_reason == "tool_calls" or cfp_has_tool_calls or tool_calls_in_progress:
                            stop_reason = "tool_use"
                        elif finish_reason == "stop":
                            stop_reason = "end_turn"

                        usage = {"output_tokens": output_tokens}
                        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': usage})}\n\n"
                        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
                        yield "data: [DONE]\n\n"
                        sse_interrupt = False
                        return

            except Exception as e:
                logger.error(f"Error processing chunk: {str(e)}")
                continue

        if sse_interrupt:
            # 上游中断时, 补充完成sse块
            stop_reason = "end_turn"
            if finish_reason == "length":
                stop_reason = "max_tokens"
            elif finish_reason == "tool_calls" or cfp_has_tool_calls or tool_calls_in_progress:
                stop_reason = "tool_use"
            elif finish_reason == "stop":
                stop_reason = "end_turn"
            usage = {"output_tokens": output_tokens}
            yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': stop_reason, 'stop_sequence': None}, 'usage': usage})}\n\n"
            yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
            yield "data: [DONE]\n\n"
            return
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        error_message = f"Error in streaming: {str(e)}\n\nFull traceback:\n{error_traceback}"
        logger.error(error_message)
        yield f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'error', 'stop_sequence': None}, 'usage': {'output_tokens': 0}})}\n\n"
        yield f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
        yield "data: [DONE]\n\n"

# ========== 新增多渠道配置类 ==========
class ProviderConfig:
    def __init__(self):
        self.providers = {}
        self._load_config()
    
    def _load_config(self):
        """从环境变量加载多供应商配置"""
        # 默认配置（当前单一配置）
        base_url = os.environ.get("BASE_URL", "https://api.openai.com/v1")
        api_key = os.environ.get("API_KEY", "")
        
        # 设置默认供应商
        self.providers["default"] = {
            "name": "default",
            "base_url": base_url,
            "api_key": api_key
        }
        
        # 加载以渠道名称为前缀的供应商配置
        # 支持格式：CHANNEL_<name>_BASE_URL 和 CHANNEL_<name>_API_KEY
        for env_key in os.environ:
            if env_key.startswith("CHANNEL_") and env_key.endswith("_BASE_URL"):
                # 提取渠道名称，如 CHANNEL_GEMINI_BASE_URL -> gemini
                channel_name = env_key[8:-9].lower()  # 去掉 CHANNEL_ 和 _BASE_URL
                
                channel_base_url = os.environ.get(env_key)
                channel_api_key = os.environ.get(f"CHANNEL_{channel_name.upper()}_API_KEY", api_key)
                
                if channel_base_url:
                    self.providers[channel_name] = {
                        "name": channel_name,
                        "base_url": channel_base_url,
                        "api_key": channel_api_key
                    }
                    logger.debug(f"Loaded channel {channel_name}: {channel_base_url}")
    
    def parse_model_and_channel(self, model_name):
        """解析模型名称和渠道信息，支持 model:channel 格式，保持 litellm 前缀"""
        # 检查新的渠道指定格式：model:channel
        if ":" in model_name:
            model_part, channel_part = model_name.split(":", 1)
            channel_name = channel_part.lower()
            if channel_name in self.providers:
                return model_part, self.providers[channel_name]
            else:
                logger.warning(f"Channel '{channel_name}' not found, using default")
                return model_part, self.providers["default"]
        
        # 没有指定渠道，返回原模型名和默认供应商
        return model_name, self.providers["default"]
    
    def get_provider_for_model(self, model_name):
        """根据模型名称获取对应的供应商配置"""
        _, provider = self.parse_model_and_channel(model_name)
        return provider
    
    def get_clean_model_name(self, model_name):
        """获取去除渠道标识后的模型名称，保持 litellm 前缀"""
        clean_model, _ = self.parse_model_and_channel(model_name)
        return clean_model

# 创建全局供应商配置实例
provider_config = ProviderConfig()

@app.post("/v1/messages")
async def create_message(
    request: MessagesRequest,
    raw_request: Request
):
    try:
        body = await raw_request.body()
        body_json = json.loads(body.decode('utf-8'))
        original_model = body_json.get("model", "unknown")
        display_model = original_model
        if "/" in display_model:
            display_model = display_model.split("/")[-1]
        elif ":" in display_model:
            display_model = display_model.split(":")[0]
            if "/" in display_model:
                display_model = display_model.split("/")[-1]
        
        # ============ 获取渠道配置，但保持完整的模型名 ============
        provider_info = provider_config.get_provider_for_model(request.model)
        clean_model_with_prefix = provider_config.get_clean_model_name(request.model)
        
        # 提取纯模型名（用于URL构建等）
        if "/" in clean_model_with_prefix:
            clean_model = clean_model_with_prefix.split("/")[-1]
        else:
            clean_model = clean_model_with_prefix
        
        # 更新 request.model 为清理后的模型名（保持 litellm 前缀）
        request.model = clean_model_with_prefix
        
        logger.debug(f"🔌 CHANNEL: {provider_info['name']} - Original: {original_model} -> Clean: {clean_model_with_prefix}")

        logger.debug(f"📊 PROCESSING REQUEST: Model={request.model}, Stream={request.stream}")

        # 原有逻辑
        litellm_request = convert_anthropic_to_litellm(request)
        
        # ============ 使用渠道配置，覆盖默认配置 ============
        if provider_info["name"] != "default":
            litellm_request["api_key"] = provider_info["api_key"]
            base_url = provider_info["base_url"]
            
            # 特殊处理不同类型模型的URL格式
            if request.model.startswith("gemini/"):
                if "/v1" in base_url:
                    api_url = f"{base_url}/models/{clean_model}"
                else:
                    api_url = f"{base_url}/v1beta/models/{clean_model}"
            else:
                api_url = base_url
            
            litellm_request["api_base"] = api_url
            litellm_request["base_url"] = api_url
            
            logger.debug(f"Using channel: {provider_info['name']} with URL: {api_url}")
        else:
            # 使用原有的默认逻辑
            if request.model.startswith("openai/"):
                litellm_request["api_key"] = OPENAI_API_KEY
                logger.debug(f"Using OpenAI API key for model: {request.model}")
            elif request.model.startswith("gemini/"):
                litellm_request["api_key"] = GEMINI_API_KEY
                logger.debug(f"Using Gemini API key for model: {request.model}")
            else:
                litellm_request["api_key"] = ANTHROPIC_API_KEY
                logger.debug(f"Using Anthropic API key for model: {request.model}")

        # For OpenAI models - modify request format to work with limitations
        if "openai" in litellm_request["model"] and "messages" in litellm_request:
            logger.debug(f"Processing OpenAI model request: {litellm_request['model']}")
            
            # For OpenAI models, we need to convert content blocks to simple strings
            # and handle other requirements
            for i, msg in enumerate(litellm_request["messages"]):
                # Special case - handle message content directly when it's a list of tool_result
                # This is a specific case we're seeing in the error
                if "content" in msg and isinstance(msg["content"], list):
                    is_only_tool_result = True
                    for block in msg["content"]:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            is_only_tool_result = False
                            break
                    
                    if is_only_tool_result and len(msg["content"]) > 0:
                        logger.warning(f"Found message with only tool_result content - special handling required")
                        # Extract the content from all tool_result blocks
                        all_text = ""
                        for block in msg["content"]:
                            all_text += "Tool Result:\n"
                            result_content = block.get("content", [])
                            
                            # Handle different formats of content
                            if isinstance(result_content, list):
                                for item in result_content:
                                    if isinstance(item, dict) and item.get("type") == "text":
                                        all_text += item.get("text", "") + "\n"
                                    elif isinstance(item, dict):
                                        # Fall back to string representation of any dict
                                        try:
                                            item_text = item.get("text", json.dumps(item))
                                            all_text += item_text + "\n"
                                        except:
                                            all_text += str(item) + "\n"
                            elif isinstance(result_content, str):
                                all_text += result_content + "\n"
                            else:
                                try:
                                    all_text += json.dumps(result_content) + "\n"
                                except:
                                    all_text += str(result_content) + "\n"
                        
                        # Replace the list with extracted text
                        litellm_request["messages"][i]["content"] = all_text.strip() or "..."
                        logger.warning(f"Converted tool_result to plain text: {all_text.strip()[:200]}...")
                        continue  # Skip normal processing for this message
                
                # 1. Handle content field - normal case
                if "content" in msg:
                    # Check if content is a list (content blocks)
                    if isinstance(msg["content"], list):
                        # Convert complex content blocks to simple string
                        text_content = ""
                        for block in msg["content"]:
                            if isinstance(block, dict):
                                # Handle different content block types
                                if block.get("type") == "text":
                                    text_content += block.get("text", "") + "\n"
                                
                                # Handle tool_result content blocks - extract nested text
                                elif block.get("type") == "tool_result":
                                    tool_id = block.get("tool_use_id", "unknown")
                                    text_content += f"[Tool Result ID: {tool_id}]\n"
                                    
                                    # Extract text from the tool_result content
                                    result_content = block.get("content", [])
                                    if isinstance(result_content, list):
                                        for item in result_content:
                                            if isinstance(item, dict) and item.get("type") == "text":
                                                text_content += item.get("text", "") + "\n"
                                            elif isinstance(item, dict):
                                                # Handle any dict by trying to extract text or convert to JSON
                                                if "text" in item:
                                                    text_content += item.get("text", "") + "\n"
                                                else:
                                                    try:
                                                        text_content += json.dumps(item) + "\n"
                                                    except:
                                                        text_content += str(item) + "\n"
                                    elif isinstance(result_content, dict):
                                        # Handle dictionary content
                                        if result_content.get("type") == "text":
                                            text_content += result_content.get("text", "") + "\n"
                                        else:
                                            try:
                                                text_content += json.dumps(result_content) + "\n"
                                            except:
                                                text_content += str(result_content) + "\n"
                                    elif isinstance(result_content, str):
                                        text_content += result_content + "\n"
                                    else:
                                        try:
                                            text_content += json.dumps(result_content) + "\n"
                                        except:
                                            text_content += str(result_content) + "\n"
                                
                                # Handle tool_use content blocks
                                elif block.get("type") == "tool_use":
                                    tool_name = block.get("name", "unknown")
                                    tool_id = block.get("id", "unknown")
                                    tool_input = json.dumps(block.get("input", {}))
                                    text_content += f"[Tool: {tool_name} (ID: {tool_id})]\nInput: {tool_input}\n\n"
                                
                                # Handle image content blocks
                                elif block.get("type") == "image":
                                    text_content += "[Image content - not displayed in text format]\n"
                        
                        # Make sure content is never empty for OpenAI models
                        if not text_content.strip():
                            text_content = "..."
                        
                        litellm_request["messages"][i]["content"] = text_content.strip()
                    # Also check for None or empty string content
                    elif msg["content"] is None:
                        litellm_request["messages"][i]["content"] = "..." # Empty content not allowed
                
                # 2. Remove any fields OpenAI doesn't support in messages
                for key in list(msg.keys()):
                    if key not in ["role", "content", "name", "tool_call_id", "tool_calls"]:
                        logger.warning(f"Removing unsupported field from message: {key}")
                        del msg[key]
            
            # 3. Final validation - check for any remaining invalid values and dump full message details
            for i, msg in enumerate(litellm_request["messages"]):
                # Log the message format for debugging
                logger.debug(f"Message {i} format check - role: {msg.get('role')}, content type: {type(msg.get('content'))}")
                
                # If content is still a list or None, replace with placeholder
                if isinstance(msg.get("content"), list):
                    logger.warning(f"CRITICAL: Message {i} still has list content after processing: {json.dumps(msg.get('content'))}")
                    # Last resort - stringify the entire content as JSON
                    litellm_request["messages"][i]["content"] = f"Content as JSON: {json.dumps(msg.get('content'))}"
                elif msg.get("content") is None:
                    logger.warning(f"Message {i} has None content - replacing with placeholder")
                    litellm_request["messages"][i]["content"] = "..." # Fallback placeholder

        # ---------- CFP 请求适配 ----------
        # Use per-model CFP configuration instead of global setting
        cfp_enabled = getattr(request, '_cfp_enabled', False)
        if cfp_enabled:
            # Use CFP adaptation for models with CFP-enabled flags
            litellm_request, _cfp_used = adapt_request_for_cfp(litellm_request, cfp_enabled)
        else:
            _cfp_used = False
        # Only log basic info about the request, not the full details
        logger.debug(f"Request for model: {litellm_request.get('model')}, stream: {litellm_request.get('stream', False)}")
        # 只有在使用默认渠道时才应用原有的URL逻辑
        if provider_info["name"] == "default":
            api_base = os.environ.get("BASE_URL", os.environ.get("API_BASE", "https://api.openai.com/v1"))
            if request.model.startswith("gemini"):
                if "/v1" in api_base:
                    api_base += f"/models/{clean_model}"
                else:
                    api_base += f"/v1beta/models/{clean_model}"
            litellm_request.update({
                "api_base": api_base,
                "base_url": api_base,
                "api_key": os.environ.get("API_KEY", "<KEY>"),
            })
        # Handle streaming mode
        if request.stream:
            # Use LiteLLM for streaming
            num_tools = len(request.tools) if request.tools else 0
            
            log_request_beautifully(
                "POST", 
                raw_request.url.path, 
                display_model, 
                litellm_request.get('model'),
                len(litellm_request['messages']),
                num_tools,
                200  # Assuming success at this point
            )
            # Ensure we use the async version for streaming
            response_generator = await litellm.acompletion(**litellm_request)
            
            return StreamingResponse(
                handle_streaming(response_generator, request, cfp_used=_cfp_used),
                media_type="text/event-stream"
            )
        else:
            # Use LiteLLM for regular completion
            num_tools = len(request.tools) if request.tools else 0
            log_request_beautifully(
                "POST", 
                raw_request.url.path, 
                display_model, 
                litellm_request.get('model'),
                len(litellm_request['messages']),
                num_tools,
                200  # Assuming success at this point
            )
            start_time = time.time()
            litellm_response = litellm.completion(**litellm_request)
            logger.debug(f"✅ RESPONSE RECEIVED: Model={litellm_request.get('model')}, Time={time.time() - start_time:.2f}s")

            # ---------- CFP 响应适配 ----------
            litellm_response = adapt_response_from_cfp(litellm_response, _cfp_used)

            # Convert LiteLLM response to Anthropic format
            anthropic_response = convert_litellm_to_anthropic(litellm_response, request)
            return anthropic_response
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        
        # Capture as much info as possible about the error
        error_details = {
            "error": str(e),
            "type": type(e).__name__,
            "traceback": error_traceback
        }
        
        # Check for LiteLLM-specific attributes
        for attr in ['message', 'status_code', 'response', 'llm_provider', 'model']:
            if hasattr(e, attr):
                error_details[attr] = getattr(e, attr)
        
        # Check for additional exception details in dictionaries
        if hasattr(e, '__dict__'):
            for key, value in e.__dict__.items():
                if key not in error_details and key not in ['args', '__traceback__']:
                    error_details[key] = str(value)
        
        # Log all error details
        logger.error(f"Error processing request: {str(e)}")
        
        # Format error for response
        error_message = f"Error: {str(e)}"
        if 'message' in error_details and error_details['message']:
            error_message += f"\nMessage: {error_details['message']}"
        if 'response' in error_details and error_details['response']:
            error_message += f"\nResponse: {error_details['response']}"
        
        # Return detailed error
        status_code = error_details.get('status_code', 500)
        raise HTTPException(status_code=status_code, detail=error_message)

@app.post("/v1/messages/count_tokens")
async def count_tokens(
    request: TokenCountRequest,
    raw_request: Request
):
    try:
        # Log the incoming token count request
        original_model = request.original_model or request.model
        
        # Get the display name for logging, just the model name without provider prefix
        display_model = original_model
        if "/" in display_model:
            display_model = display_model.split("/")[-1]
        
        # Clean model name for capability check
        clean_model = request.model
        if "/" in clean_model:
            clean_model = clean_model.split("/")[-1]
        
        # Convert the messages to a format LiteLLM can understand
        converted_request = convert_anthropic_to_litellm(
            MessagesRequest(
                model=request.model,
                max_tokens=100,  # Arbitrary value not used for token counting
                messages=request.messages,
                system=request.system,
                tools=request.tools,
                tool_choice=request.tool_choice,
                thinking=request.thinking
            )
        )
        
        # Use LiteLLM's token_counter function
        try:
            # Import token_counter function
            from litellm import token_counter
            
            # Log the request beautifully
            num_tools = len(request.tools) if request.tools else 0
            
            log_request_beautifully(
                "POST",
                raw_request.url.path,
                display_model,
                converted_request.get('model'),
                len(converted_request['messages']),
                num_tools,
                200  # Assuming success at this point
            )
            
            # Count tokens
            token_count = token_counter(
                model=converted_request["model"],
                messages=converted_request["messages"],
            )
            
            # Return Anthropic-style response
            return TokenCountResponse(input_tokens=token_count)
            
        except ImportError:
            logger.error("Could not import token_counter from litellm")
            # Fallback to a simple approximation
            return TokenCountResponse(input_tokens=1000)  # Default fallback
            
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        logger.error(f"Error counting tokens: {str(e)}\n{error_traceback}")
        raise HTTPException(status_code=500, detail=f"Error counting tokens: {str(e)}")

@app.get("/")
async def root():
    return {"message": "Anthropic Proxy for LiteLLM"}

# Define ANSI color codes for terminal output
class Colors:
    CYAN = "\033[96m"
    BLUE = "\033[94m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"
    DIM = "\033[2m"
def log_request_beautifully(method, path, claude_model, openai_model, num_messages, num_tools, status_code):
    """Log requests in a beautiful, twitter-friendly format showing Claude to OpenAI mapping."""
    # Format the Claude model name nicely
    claude_display = f"{Colors.CYAN}{claude_model}{Colors.RESET}"
    
    # Extract endpoint name
    endpoint = path
    if "?" in endpoint:
        endpoint = endpoint.split("?")[0]
    
    # Extract just the OpenAI model name without provider prefix
    openai_display = openai_model
    if "/" in openai_display:
        openai_display = openai_display.split("/")[-1]
    openai_display = f"{Colors.GREEN}{openai_display}{Colors.RESET}"
    
    # Format tools and messages
    tools_str = f"{Colors.MAGENTA}{num_tools} tools{Colors.RESET}"
    messages_str = f"{Colors.BLUE}{num_messages} messages{Colors.RESET}"
    
    # Format status code
    status_str = f"{Colors.GREEN}✓ {status_code} OK{Colors.RESET}" if status_code == 200 else f"{Colors.RED}✗ {status_code}{Colors.RESET}"
    

    # Put it all together in a clear, beautiful format
    log_line = f"{Colors.BOLD}{method} {endpoint}{Colors.RESET} {status_str}"
    model_line = f"{claude_display} → {openai_display} {tools_str} {messages_str}"
    
    # Print to console
    print(log_line)
    print(model_line)
    sys.stdout.flush()

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--help":
        print("Run with: uvicorn server:app --reload --host 0.0.0.0 --port 8082")
        sys.exit(0)
    
    # Configure uvicorn to run with minimal logs
    uvicorn.run(app, host="0.0.0.0", port=8082, log_level="error")