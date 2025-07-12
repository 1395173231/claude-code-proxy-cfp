import json, os, textwrap
from typing import List, Dict, Any, Tuple, Optional
from cfp_codec import encode as cfp_encode, parse_block, extract_blocks, new_call_id

# 定义需要 CFP 适配的模型后缀
CFP_ENABLED_SUFFIXES = [
    "-textonly",  # 只支持文本对话的模型，需要CFP适配来实现函数调用
    "-cfp",  # 明确标记需要 CFP 的模型
    "-text",  # 纯文本模型，需要CFP适配
]


def should_use_cfp(model_name: str) -> bool:
    """
    判断指定模型是否需要使用 CFP 适配
    注意：-textonly, -cfp, -text 后缀表示需要CFP适配来实现函数调用
    """
    if not model_name:
        return False

    # 检查模型名是否包含需要 CFP 适配的后缀
    for suffix in CFP_ENABLED_SUFFIXES:
        if model_name.endswith(suffix):
            return True

    # 默认不使用 CFP（原生支持函数调用的模型）
    return False


def normalize_model_name(model_name: str) -> str:
    """
    标准化模型名，移除 CFP 相关后缀
    用于实际调用上游 API 时使用
    """
    if not model_name:
        return model_name

    for suffix in CFP_ENABLED_SUFFIXES:
        if model_name.endswith(suffix):
            return model_name[:-len(suffix)]

    return model_name


# 简化后的 CFP 指导和示例
CFP_GUIDE = """You follow the Chat-Function-Protocol (CFP).
When a tool is required, output ONLY:
<cfp>{\"role\":\"call\",\"id\":\"$UUID\",\"name\":\"$FUNC\",\"args\":$ARGS}</cfp>
After you receive a role=\"result\" CFP block, think and reply normally.
"""


def tools_to_system_prompt(tools: list) -> str:
    """
    把 OpenAI-style tools 列表转换成可读的 system prompt，
    保留 name / description / parameters(JSON)。
    """
    lines = ["Available functions:\n"]
    for t in tools:
        fn = t.get("function", t)
        name = fn["name"]
        desc = fn.get("description", "")
        schema = fn.get("parameters", fn.get("input_schema", {}))

        # 格式化参数描述
        params_str = json.dumps(schema, ensure_ascii=False, indent=2)
        lines.append(f"**{name}**")
        lines.append(f"Description: {desc}")
        lines.append(f"Parameters: {params_str}")
        lines.append("")

    return "\n".join(lines)


def build_cfp_messages(messages: List[Dict[str, Any]], tools: List[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []

    # 收集所有原始 system messages
    existing_system_content = []

    # 先处理原始 messages 中的 system 消息
    for m in messages:
        if m["role"] == "system":
            existing_system_content.append(m["content"])

    # 构建合并的 system message
    system_parts = []

    # 1. 添加原有的 system 内容
    if existing_system_content:
        system_parts.extend(existing_system_content)

    # 2. 添加工具描述（如果有工具）
    if tools:
        system_parts.append(tools_to_system_prompt(tools))

    # 3. 添加 CFP 指导和示例
    system_parts.append(CFP_GUIDE.strip())

    # 合并所有 system 内容
    if system_parts:
        combined_system = "\n\n".join(system_parts)
        out.append({"role": "system", "content": combined_system})

    # 处理非 system 消息
    for m in messages:
        role = m["role"]

        # 跳过 system 消息（已经在上面处理了）
        if role == "system":
            continue

        # 普通对话
        if role in {"user", "assistant"} and "function_call" not in m:
            out.append({"role": role, "content": m["content"]})
            continue

        # assistant 调用工具 -> CFP call
        if role == "assistant" and m.get("function_call"):
            # 简化的 CFP 调用格式
            cfp_call = {
                "role": "call",
                "id": new_call_id(),
                "name": m["function_call"]["name"],
                "args": json.loads(m["function_call"]["arguments"])
            }
            block = f"<cfp>{json.dumps(cfp_call, ensure_ascii=False)}</cfp>"
            out.append({"role": "assistant", "content": block})
            continue

        # function 结果 -> CFP result
        if role == "function":
            # 简化的 CFP 结果格式
            cfp_result = {
                "role": "result",
                "id": new_call_id(),
                "result": json.loads(m.get("content") or "{}")
            }
            block = f"<cfp>{json.dumps(cfp_result, ensure_ascii=False)}</cfp>"
            out.append({"role": "user", "content": block})
            continue

        # 兜底
        out.append({"role": role, "content": m.get("content", "")})

    return out


def adapt_request_for_cfp(req: Dict[str, Any], cfp_enabled=False) -> Tuple[Dict[str, Any], bool]:
    """
    根据模型名判断是否需要 CFP 适配
    """
    model_name = req.get("model", "")

    # 判断是否需要 CFP
    if not cfp_enabled or not req.get("tools"):
        return req, False

    # 构建 CFP 格式的消息
    new_msgs = build_cfp_messages(req["messages"], req["tools"])

    new_req = req.copy()
    new_req["messages"] = new_msgs
    # 标准化模型名（移除 CFP 后缀）
    new_req["model"] = normalize_model_name(model_name)
    new_req.pop("tools", None)
    new_req["tool_choice"] = None
    return new_req, True


def parse_cfp_response(text: str) -> Tuple[Optional[str], Optional[list]]:
    """
    解析 CFP 响应，支持简化的协议格式
    """
    try:
        blocks = extract_blocks(text)
        if not blocks:
            return text.strip(), None
        tool_calls = []
        for block in blocks:

            # 解析 CFP 块
            cfp_json = parse_block(block)

            if cfp_json["role"] == "call":
                tool_call = {
                    "id": cfp_json["id"],
                    "type": "function",
                    "function": {
                        "name": cfp_json["name"],
                        "arguments": json.dumps(cfp_json["args"], ensure_ascii=False)
                    }
                }
                tool_calls.append(tool_call)


            if cfp_json["role"] == "result":
                return json.dumps(cfp_json["result"], ensure_ascii=False), None

            if cfp_json["role"] == "error":
                return f"[CFP error] {cfp_json.get('error', 'Unknown error')}", None
        if len(tool_calls) >0 :
            return None, tool_calls
        return text.strip(), None
    except Exception as e:
        # 如果解析失败，返回原始文本
        return text.strip(), None


def adapt_response_from_cfp(resp: Any, cfp_used: bool):
    """
    适配 CFP 响应格式到标准 OpenAI 格式
    """
    if not cfp_used:
        return resp

    msg = resp.choices[0].message
    content_raw = msg.content or ""
    plain, calls = parse_cfp_response(content_raw)

    if calls:
        msg.tool_calls = calls
        msg.content = None
        resp.choices[0].finish_reason = "tool_calls"
        setattr(resp, "_from_cfp", True)
    else:
        msg.content = plain

    return resp


if __name__ == "__main__":
    text = """
    I can see that the `api_base` is currently hardcoded to `"https://easyone.eqing.tech/v1"` on lines 1198 and 1220. I'll modify the code to use an environment variable instead.

<cfp>{"role":"call","id":"1","name":"Edit","args":{"file_path":"E:\\Workspace\\claude-code-openai\\server.py","old_string":"# Get API keys from environment\nANTHROPIC_API_KEY = os.environ.get(\"ANTHROPIC_API_KEY\")\nOPENAI_API_KEY = os.environ.get(\"OPENAI_API_KEY\")\nGEMINI_API_KEY = os.environ.get(\"GEMINI_API_KEY\")\n\n# Get preferred provider (default to openai)\nPREFERRED_PROVIDER = os.environ.get(\"PREFERRED_PROVIDER\", \"openai\").lower()","new_string":"# Get API keys from environment\nANTHROPIC_API_KEY = os.environ.get(\"ANTHROPIC_API_KEY\")\nOPENAI_API_KEY = os.environ.get(\"OPENAI_API_KEY\")\nGEMINI_API_KEY = os.environ.get(\"GEMINI_API_KEY\")\n\n# Get API base URL from environment (default to hardcoded value for backward compatibility)\nAPI_BASE_URL = os.environ.get(\"API_BASE_URL\", \"https://easyone.eqing.tech/v1\")\n\n# Get preferred provider (default to openai)\nPREFERRED_PROVIDER = os.environ.get(\"PREFERRED_PROVIDER\", \"openai\").lower()"}}]</cfp>

<cfp>{"role":"call","id":"2","name":"Edit","args":{"file_path":"E:\\Workspace\\claude-code-openai\\server.py","old_string":"            # Ensure we use the async version for streaming\n            litellm_request.update({\n                \"api_base\": \"https://easyone.eqing.tech/v1\",\n            })","new_string":"            # Ensure we use the async version for streaming\n            litellm_request.update({\n                \"api_base\": API_BASE_URL,\n            })"}}]</cfp>

<cfp>{"role":"call","id":"3","name":"Edit","args":{"file_path":"E:\\Workspace\\claude-code-openai\\server.py","old_string":"            litellm_request.update({\n                # api key to your openai compatible endpoint\n                \"api_base\": \"https://easyone.eqing.tech/v1\",\n            })","new_string":"            litellm_request.update({\n                # api key to your openai compatible endpoint\n                \"api_base\": API_BASE_URL,\n            })"}}]</cfp>

现在 `api_base` 已经修改为使用环境变量 `API_BASE_URL` 读取。如果环境变量没有设置，会使用默认值 `"https://easyone.eqing.tech/v1"` 保持向后兼容性。
    """
    text, tool_calls = parse_cfp_response(text)
    print(tool_calls)