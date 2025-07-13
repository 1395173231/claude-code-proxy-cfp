import json, os, textwrap, uuid
import re
from typing import List, Dict, Any, Tuple, Optional, Generator
from cfp_codec import (
    CFPMarkers, new_call_id, encode_call, encode_result, encode_error,
    encode_args_delta, encode_args_complete, extract_blocks, parse_block,
    clean_cfp_text, has_cfp_blocks, split_text_and_cfp, parse_blocks,
    get_cfp_blocks_with_positions
)

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


def generate_cfp_guide() -> str:
    """生成 CFP 指导文档"""
    call_example = encode_call("$UUID", "$FUNC", {})
    delta_example = encode_args_delta("$UUID", "$JSON_DELTA")
    complete_example = encode_args_complete("$UUID")

    return f"""You follow the Chat-Function-Protocol (CFP) v2.
When a tool is required, output in this order:
1. First output: {call_example}
2. Then incrementally output args as JSON deltas: {delta_example}
3. Finally signal completion: {complete_example}

This allows clients to get function call intent early in streaming responses.
After you receive a result CFP block, think and reply normally.
The CFP blocks use special marker characters within <cfp> tags and won't interfere with normal text.
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
    system_parts.append(generate_cfp_guide())

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

        # assistant 调用工具 -> CFP call 序列
        if role == "assistant" and m.get("function_call"):
            call_id = new_call_id()

            # 使用 cfp_codec 生成 CFP 块
            cfp_call = encode_call(call_id, m["function_call"]["name"], {})
            cfp_args_delta = encode_args_delta(call_id, m["function_call"]["arguments"])
            cfp_complete = encode_args_complete(call_id)

            # 组合成完整的 CFP 消息
            cfp_content = cfp_call + cfp_args_delta + cfp_complete
            out.append({"role": "assistant", "content": cfp_content})
            continue

        # function 结果 -> CFP result
        if role == "function":
            call_id = new_call_id()
            result_data = json.loads(m.get("content") or "{}")
            cfp_result = encode_result(call_id, result_data)
            out.append({"role": "user", "content": cfp_result})
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


class CFPStreamParser:
    """CFP 流式解析器，支持流式和非流式处理"""

    def __init__(self, strict_validation: bool = True):
        self.active_calls: Dict[str, Dict] = {}  # call_id -> call_info
        self.completed_calls: List[Dict] = []
        self.buffer = ""
        self.strict_validation = strict_validation

    def _validate_cfp_content(self, content: str) -> bool:
        """严格验证 CFP 内容是否符合协议格式"""
        if not self.strict_validation:
            return True

        try:
            data = parse_block(content)

            # 检查必需字段
            if not isinstance(data, dict):
                return False

            # 检查版本号
            if data.get("v") != 1:
                return False

            # 检查角色字段
            role = data.get("role")
            valid_roles = ["call", "result", "error", "args_delta", "args_complete"]
            if role not in valid_roles:
                return False

            # 检查 ID 格式
            call_id = data.get("id")
            if not call_id or not isinstance(call_id, str):
                return False

            # 根据角色验证必需字段
            if role == "call":
                if not data.get("name") or not isinstance(data.get("args"), dict):
                    return False
            elif role == "result":
                if "result" not in data:
                    return False
            elif role == "args_delta":
                if "delta" not in data:
                    return False

            return True

        except Exception:
            return False

    def _extract_complete_cfp_blocks(self, text: str) -> Tuple[List[Tuple[str, str]], str]:
        """提取完整的CFP块，返回(块列表, 剩余文本)"""
        parts = []
        remaining = text
        current_pos = 0

        # 找到所有完整的 CFP 块
        pattern = re.compile(r'<cfp([^>]*)>(.*?)</cfp>', re.DOTALL)

        for match in pattern.finditer(text):
            start_pos = match.start()
            end_pos = match.end()
            content = match.group(2)

            # 添加CFP前的文本
            if start_pos > current_pos:
                text_content = text[current_pos:start_pos]
                if text_content:
                    parts.append(("text", text_content))

            # 验证CFP内容
            if self._validate_cfp_content(content):
                parts.append(("cfp", content))
            else:
                # 验证失败，作为普通文本
                parts.append(("text", match.group(0)))

            current_pos = end_pos

        # 剩余部分
        remaining = text[current_pos:] if current_pos < len(text) else ""

        return parts, remaining

    def _has_incomplete_cfp_block(self) -> bool:
        """检查是否有不完整的 CFP 块"""
        buffer = self.buffer.strip()
        if not buffer:
            return False

        # 只检查明显的不完整情况
        incomplete_patterns = [
            '<', '<c', '<cf', '<cfp',  # 不完整的开始标记
            '</', '</c', '</cf'        # 不完整的结束标记（但不包括 '</cfp'）
        ]

        for pattern in incomplete_patterns:
            if buffer.endswith(pattern):
                return True

        # 检查是否有未配对的开始标记
        start_count = len(re.findall(r'<cfp[^>]*>', buffer))
        end_count = len(re.findall(r'</cfp>', buffer))

        # 如果有明显未配对且末尾不是完整JSON，才认为不完整
        if start_count > end_count:
            # 检查最后一个未闭合的块是否可能是完整的JSON
            last_cfp_match = re.search(r'<cfp[^>]*>([^<]*)$', buffer)
            if last_cfp_match:
                content = last_cfp_match.group(1)
                try:
                    json.loads(content)
                    # JSON完整，可能只是缺少</cfp>，在流式中直接处理
                    return False
                except:
                    # JSON不完整，等待更多数据
                    return True

        return False

    def parse_stream_chunk(self, chunk: str) -> List[Dict]:
        """解析流式数据块 - 立即处理能处理的部分"""
        self.buffer += chunk
        events = []

        try:
            # 提取所有完整的CFP块
            parts, remaining = self._extract_complete_cfp_blocks(self.buffer)

            # 处理完整的部分
            for part_type, content in parts:
                if part_type == "text":
                    if content:
                        events.append({
                            "type": "text",
                            "content": content
                        })
                elif part_type == "cfp":
                    try:
                        cfp_data = parse_block(content)
                        event = self._process_cfp_block(cfp_data)
                        if event:
                            events.append(event)
                    except Exception:
                        # 解析失败，作为文本输出
                        events.append({
                            "type": "text",
                            "content": f"<cfp>{content}</cfp>"
                        })

            # 更新缓冲区为剩余部分
            self.buffer = remaining

        except Exception:
            # 解析出错，保持缓冲区不变
            pass

        return events

    def finalize_stream(self) -> List[Dict]:
        """
        流结束时调用，处理缓冲区中的剩余内容
        只在确定流已结束时调用（非流式或流式结束）
        """
        events = []

        if self.buffer:
            buffer = self.buffer.strip()

            # 尝试修复最后可能不完整的CFP块
            if buffer and not buffer.endswith('</cfp>'):
                # 检查是否是不完整的CFP块
                last_cfp_match = re.search(r'<cfp[^>]*>([^<]*)$', buffer)
                if last_cfp_match:
                    content = last_cfp_match.group(1)
                    try:
                        # 如果JSON完整，添加结束标签
                        json.loads(content)
                        buffer += '</cfp>'
                    except:
                        pass

            # 最后一次解析
            try:
                parts, remaining = self._extract_complete_cfp_blocks(buffer)

                for part_type, content in parts:
                    if part_type == "text":
                        if content:
                            events.append({
                                "type": "text",
                                "content": content
                            })
                    elif part_type == "cfp":
                        try:
                            cfp_data = parse_block(content)
                            event = self._process_cfp_block(cfp_data)
                            if event:
                                events.append(event)
                        except Exception:
                            events.append({
                                "type": "text",
                                "content": f"<cfp>{content}</cfp>"
                            })

                # 处理最后的剩余部分
                if remaining:
                    events.append({
                        "type": "text",
                        "content": remaining
                    })

            except Exception:
                # 如果仍然失败，将整个缓冲区作为文本输出
                events.append({
                    "type": "text",
                    "content": buffer
                })

            self.buffer = ""

        return events

    def _process_cfp_block(self, cfp_data: Dict) -> Optional[Dict]:
        """处理单个 CFP 块，修复参数累积问题"""
        role = cfp_data.get("role")
        call_id = cfp_data.get("id")

        if role == "call":
            # 函数调用开始 - 只记录初始状态，不预设完整参数
            initial_args = cfp_data.get("args", {})

            # 初始参数应该是空对象，参数通过 args_delta 累积
            self.active_calls[call_id] = {
                "id": call_id,
                "name": cfp_data.get("name"),
                "args": "",  # 改为空字符串，等待 args_delta 填充
                "complete": False
            }
            return {
                "type": "call_start",
                "id": call_id,
                "name": cfp_data.get("name"),
                "args": ""  # 初始为空
            }

        elif role == "args_delta":
            # 参数增量 - 这里是关键修复点
            if call_id in self.active_calls:
                delta = cfp_data.get("delta", "")
                current_args = self.active_calls[call_id]["args"]

                # 如果当前参数为空，直接使用 delta
                if not current_args:
                    self.active_calls[call_id]["args"] = delta
                else:
                    # 否则拼接
                    self.active_calls[call_id]["args"] += delta

                return {
                    "type": "args_delta",
                    "id": call_id,
                    "delta": delta
                }

        elif role == "args_complete":
            # 参数传输完成
            if call_id in self.active_calls:
                call_info = self.active_calls[call_id]
                final_args = call_info["args"]

                # 验证和清理最终的 JSON 格式
                try:
                    if final_args:
                        # 尝试解析 JSON 以验证格式
                        parsed_args = json.loads(final_args)
                        # 重新序列化以确保格式正确
                        clean_args = json.dumps(parsed_args, ensure_ascii=False)
                        call_info["args"] = clean_args
                    else:
                        # 如果没有参数，使用空对象
                        call_info["args"] = "{}"
                except json.JSONDecodeError:
                    # JSON 格式错误，使用空对象
                    call_info["args"] = "{}"

                call_info["complete"] = True
                self.completed_calls.append(call_info.copy())

                return {
                    "type": "call_complete",
                    "id": call_id,
                    "full_args": call_info["args"]
                }

        elif role == "result":
            # 函数执行结果
            return {
                "type": "result",
                "result": cfp_data.get("result")
            }

        return None

    def get_completed_tool_calls(self) -> List[Dict]:
        """获取所有完成的工具调用，转换为 OpenAI 格式，修复参数格式"""
        tool_calls = []
        for call_info in self.completed_calls:
            args_str = call_info["args"]

            # 额外的参数清理步骤
            try:
                # 修复 "{}{...}" 格式问题
                if args_str.startswith("{}"):
                    args_str = args_str[2:]  # 移除前面多余的 "{}"

                # 验证 JSON 格式
                parsed_args = json.loads(args_str)
                clean_args = json.dumps(parsed_args, ensure_ascii=False)

            except json.JSONDecodeError:
                # 如果仍然无法解析，使用空对象
                clean_args = "{}"

            tool_call = {
                "id": call_info.get("id", f"call_{len(tool_calls)}"),
                "type": "function",
                "function": {
                    "name": call_info["name"],
                    "arguments": clean_args
                }
            }
            tool_calls.append(tool_call)
        return tool_calls

    def has_active_calls(self) -> bool:
        """检查是否有活跃的调用"""
        return len(self.active_calls) > 0

    def has_completed_calls(self) -> bool:
        """检查是否有完成的调用"""
        return len(self.completed_calls) > 0

    def reset(self):
        """重置解析器状态"""
        self.active_calls.clear()
        self.completed_calls.clear()
        self.buffer = ""


def parse_cfp_response(text: str, strict_validation: bool = True) -> Tuple[Optional[str], Optional[list]]:
    """
    解析 CFP 响应（非流式），支持不完整响应的修复
    """
    try:
        # 使用 cfp_codec 检查是否有 CFP 块
        if not has_cfp_blocks(text):
            return text.strip(), None

        # 使用带验证的解析器处理
        parser = CFPStreamParser(strict_validation=strict_validation)
        events = parser.parse_stream_chunk(text)

        # 强制完成解析（处理可能不完整的响应）
        final_events = parser.finalize_stream()
        events.extend(final_events)

        # 检查是否有完成的工具调用
        tool_calls = parser.get_completed_tool_calls()
        if tool_calls:
            return None, tool_calls

        # 检查是否有结果
        for event in events:
            if event["type"] == "result":
                return json.dumps(event["result"], ensure_ascii=False), None

        # 提取文本内容
        text_parts = [event["content"] for event in events if event["type"] == "text"]
        if text_parts:
            return "".join(text_parts), None

        return text.strip(), None
    except Exception:
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
    plain, calls = parse_cfp_response(content_raw, strict_validation=True)

    if calls:
        msg.tool_calls = calls
        msg.content = None
        resp.choices[0].finish_reason = "tool_calls"
        setattr(resp, "_from_cfp", True)
    else:
        # 使用 cfp_codec 清理 CFP 标记字符
        msg.content = clean_cfp_text(plain) if plain else None

    return resp


def adapt_streaming_response_from_cfp(chunk_generator: Generator, cfp_used: bool, strict_validation: bool = True):
    """
    适配流式 CFP 响应，真正的流式处理
    """
    if not cfp_used:
        yield from chunk_generator
        return

    parser = CFPStreamParser(strict_validation=strict_validation)

    try:
        for chunk in chunk_generator:
            if hasattr(chunk, 'choices') and chunk.choices:
                delta = chunk.choices[0].delta
                if hasattr(delta, 'content') and delta.content:
                    events = parser.parse_stream_chunk(delta.content)

                    for event in events:
                        if event["type"] == "call_start":
                            yield create_tool_call_start_chunk(event)
                        elif event["type"] == "args_delta":
                            yield create_tool_call_delta_chunk(event)
                        elif event["type"] == "call_complete":
                            yield create_tool_call_complete_chunk(event)
                        elif event["type"] == "text":
                            clean_content = clean_cfp_text(event["content"])
                            if clean_content:
                                yield create_text_chunk(clean_content)
                else:
                    yield chunk
            else:
                yield chunk

    finally:
        # 流结束时处理剩余内容
        final_events = parser.finalize_stream()
        for event in final_events:
            if event["type"] == "text":
                clean_content = clean_cfp_text(event["content"])
                if clean_content:
                    yield create_text_chunk(clean_content)
            elif event["type"] == "call_complete":
                yield create_tool_call_complete_chunk(event)


def create_tool_call_start_chunk(event: Dict) -> Dict:
    """创建工具调用开始的流式响应块"""
    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "id": event["id"],
                    "type": "function",
                    "function": {
                        "name": event["name"],
                        "arguments": ""
                    }
                }]
            },
            "finish_reason": None
        }]
    }


def create_tool_call_delta_chunk(event: Dict) -> Dict:
    """创建工具调用参数增量的流式响应块"""
    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "function": {
                        "arguments": event["delta"]
                    }
                }]
            },
            "finish_reason": None
        }]
    }


def create_tool_call_complete_chunk(event: Dict) -> Dict:
    """创建工具调用完成的流式响应块"""
    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "tool_calls"
        }]
    }


def create_text_chunk(content: str) -> Dict:
    """创建普通文本的流式响应块"""
    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {
                "content": content
            },
            "finish_reason": None
        }]
    }
