import json, re, uuid
from typing import Dict, Any, Tuple, List
import json_repair


# CFP 标记字符定义（在 <cfp> 标签内使用）
class CFPMarkers:
    """CFP 标记字符定义 - 在 <cfp> 标签内使用特殊字符前缀"""
    # 使用可见的特殊字符作为角色标识
    CALL_MARKER = "⚡"  # 函数调用
    ARGS_DELTA_MARKER = "📝"  # 参数增量
    ARGS_COMPLETE_MARKER = "✅"  # 参数完成
    RESULT_MARKER = "🔄"  # 结果
    ERROR_MARKER = "❌"  # 错误

    @classmethod
    def get_marker_for_role(cls, role: str) -> str:
        """根据角色获取对应的标记字符"""
        markers_map = {
            "call": cls.CALL_MARKER,
            "args_delta": cls.ARGS_DELTA_MARKER,
            "args_complete": cls.ARGS_COMPLETE_MARKER,
            "result": cls.RESULT_MARKER,
            "error": cls.ERROR_MARKER,
        }
        return markers_map.get(role, cls.CALL_MARKER)

    @classmethod
    def get_role_from_marker(cls, marker: str) -> str:
        """从标记字符获取角色"""
        role_map = {
            cls.CALL_MARKER: "call",
            cls.ARGS_DELTA_MARKER: "args_delta",
            cls.ARGS_COMPLETE_MARKER: "args_complete",
            cls.RESULT_MARKER: "result",
            cls.ERROR_MARKER: "error",
        }
        return role_map.get(marker, "unknown")

    @classmethod
    def get_all_markers(cls) -> List[str]:
        """获取所有标记字符"""
        return [
            cls.CALL_MARKER,
            cls.ARGS_DELTA_MARKER,
            cls.ARGS_COMPLETE_MARKER,
            cls.RESULT_MARKER,
            cls.ERROR_MARKER,
        ]

    @classmethod
    def create_pattern(cls) -> re.Pattern:
        """创建匹配带标记的 CFP 块的正则表达式"""
        # 匹配 <cfp{marker}>content</cfp> 格式
        markers = '|'.join(re.escape(marker) for marker in cls.get_all_markers())
        return re.compile(rf'<cfp([{markers}])>(.*?)</cfp>', re.DOTALL)

    @classmethod
    def detect_marker_in_text(cls, text: str) -> Tuple[str, str]:
        """从文本中检测标记和内容"""
        pattern = cls.create_pattern()
        match = pattern.search(text)
        if match:
            marker = match.group(1)
            content = match.group(2)
            return marker, content
        return "", ""


# 传统标记（保持向后兼容）
TAG_OPEN = "<cfp>"
TAG_CLOSE = "</cfp>"


def new_call_id() -> str:
    return str(uuid.uuid4())


# ----------- 编码 -----------
def encode(role: str,
           call_id: str,
           name: str | None = None,
           args: dict | None = None,
           result: dict | None = None,
           err: dict | None = None,
           version: int = 1,
           use_role_markers: bool = True) -> str:
    """
    生成 CFP 文本片段

    Args:
        role: 角色类型 (call, result, error, args_delta, args_complete)
        call_id: 调用ID
        name: 函数名 (仅 call 角色需要)
        args: 参数 (仅 call 角色需要)
        result: 结果 (仅 result 角色需要)
        err: 错误 (仅 error 角色需要)
        version: 版本号
        use_role_markers: 是否使用角色标记字符
    """
    doc: Dict[str, Any] = dict(v=version, role=role, id=call_id)

    if role == "call":
        doc.update(name=name, args=args or {})
    elif role == "result":
        doc.update(result=result or {})
    elif role == "error":
        doc.update(err=err or {})
    elif role == "args_delta":
        doc.update(delta=args or "")
    elif role == "args_complete":
        pass  # 只需要基本信息
    else:
        raise ValueError(f"unsupported role: {role}")

    payload = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)

    if use_role_markers:
        marker = CFPMarkers.get_marker_for_role(role)
        return f"<cfp{marker}>{payload}</cfp>"
    else:
        return f"{TAG_OPEN}{payload}{TAG_CLOSE}"


def encode_call(call_id: str, name: str, args: dict | None = None,
                use_role_markers: bool = True) -> str:
    """编码函数调用"""
    return encode("call", call_id, name=name, args=args,
                  use_role_markers=use_role_markers)


def encode_result(call_id: str, result: dict | None = None,
                  use_role_markers: bool = True) -> str:
    """编码函数结果"""
    return encode("result", call_id, result=result,
                  use_role_markers=use_role_markers)


def encode_error(call_id: str, err: dict | None = None,
                 use_role_markers: bool = True) -> str:
    """编码错误"""
    return encode("error", call_id, err=err,
                  use_role_markers=use_role_markers)


def encode_args_delta(call_id: str, delta: str,
                      use_role_markers: bool = True) -> str:
    """编码参数增量"""
    return encode("args_delta", call_id, args=delta,
                  use_role_markers=use_role_markers)


def encode_args_complete(call_id: str, use_role_markers: bool = True) -> str:
    """编码参数完成"""
    return encode("args_complete", call_id,
                  use_role_markers=use_role_markers)


# ----------- 解码 -----------
# 传统标记的正则表达式
_cfp_re = re.compile(rf"{re.escape(TAG_OPEN)}\s*(.+?)\s*{re.escape(TAG_CLOSE)}", re.S)

# 带标记的正则表达式
_cfp_marker_re = CFPMarkers.create_pattern()


def extract_blocks(txt: str) -> list[str]:
    """
    从文本中提取 CFP 块内容
    支持传统标记和角色标记
    """
    blocks = []

    # 提取传统标记的块
    traditional_blocks = _cfp_re.findall(txt)
    blocks.extend(traditional_blocks)

    # 提取带角色标记的块
    marker_matches = _cfp_marker_re.finditer(txt)
    for match in marker_matches:
        marker = match.group(1)
        content = match.group(2)
        blocks.append(content)

    return blocks


def extract_blocks_with_markers(txt: str) -> list[tuple[str, str]]:
    """
    从文本中提取 CFP 块内容及其标记类型
    返回 (marker_type, content) 的列表
    """
    blocks = []

    # 提取传统标记的块
    traditional_blocks = _cfp_re.findall(txt)
    for block in traditional_blocks:
        blocks.append(("traditional", block))

    # 提取带角色标记的块
    marker_matches = _cfp_marker_re.finditer(txt)
    for match in marker_matches:
        marker = match.group(1)
        content = match.group(2)
        role = CFPMarkers.get_role_from_marker(marker)
        blocks.append((role, content))

    return blocks


def parse_block(raw: str) -> dict:
    """
    解析 CFP 块内容
    """
    try:
        doc = json_repair.loads(raw)
        return doc
    except Exception:
        # 如果 json_repair 失败，尝试标准 json 解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 如果都失败，返回原始内容
            return {"raw": raw, "error": "parse_failed"}


def parse_blocks(txt: str) -> list[dict]:
    """
    解析文本中的所有 CFP 块
    """
    blocks = extract_blocks(txt)
    parsed_blocks = []

    for block in blocks:
        try:
            parsed = parse_block(block)
            parsed_blocks.append(parsed)
        except Exception:
            # 解析失败时跳过该块
            continue

    return parsed_blocks


def clean_cfp_text(txt: str) -> str:
    """
    清理文本中的 CFP 标记，返回纯文本内容
    """
    # 移除传统标记
    txt = _cfp_re.sub('', txt)

    # 移除带角色标记的块
    txt = _cfp_marker_re.sub('', txt)

    return txt


def has_cfp_blocks(txt: str) -> bool:
    """
    检查文本是否包含 CFP 块
    """
    # 检查传统标记
    if _cfp_re.search(txt):
        return True

    # 检查带角色标记
    if _cfp_marker_re.search(txt):
        return True

    return False


def split_text_and_cfp(txt: str) -> list[tuple[str, str]]:
    """
    分割文本为普通文本和 CFP 块
    返回 (type, content) 的列表，type 为 'text' 或 'cfp'
    """
    parts = []
    current_pos = 0

    # 收集所有 CFP 块的位置
    cfp_positions = []

    # 收集传统标记的位置
    for match in _cfp_re.finditer(txt):
        cfp_positions.append((match.start(), match.end(), match.group(1), "traditional"))

    # 收集带角色标记的位置
    for match in _cfp_marker_re.finditer(txt):
        cfp_positions.append((match.start(), match.end(), match.group(2), "marker"))

    # 按位置排序
    cfp_positions.sort(key=lambda x: x[0])

    # 处理所有位置
    for start, end, content, block_type in cfp_positions:
        # 添加之前的文本
        if start > current_pos:
            text_content = txt[current_pos:start]
            if text_content:
                parts.append(("text", text_content))

        # 添加 CFP 块
        parts.append(("cfp", content))
        current_pos = end

    # 添加剩余的文本
    if current_pos < len(txt):
        remaining_text = txt[current_pos:]
        if remaining_text:
            parts.append(("text", remaining_text))

    return parts


def get_cfp_blocks_with_positions(txt: str) -> list[tuple[int, int, str, str]]:
    """
    获取文本中所有 CFP 块的位置信息
    返回 (start, end, content, marker_type) 的列表
    """
    blocks = []

    # 收集传统标记的块
    for match in _cfp_re.finditer(txt):
        blocks.append((match.start(), match.end(), match.group(1), "traditional"))

    # 收集带角色标记的块
    for match in _cfp_marker_re.finditer(txt):
        marker = match.group(1)
        content = match.group(2)
        role = CFPMarkers.get_role_from_marker(marker)
        blocks.append((match.start(), match.end(), content, role))

    # 按开始位置排序
    blocks.sort(key=lambda x: x[0])
    return blocks


def replace_cfp_blocks(txt: str, replacement: str = "") -> str:
    """
    替换文本中的所有 CFP 块
    """
    # 先替换带角色标记的块
    txt = _cfp_marker_re.sub(replacement, txt)

    # 再替换传统标记的块
    txt = _cfp_re.sub(replacement, txt)

    return txt


# 向后兼容的函数别名
def encode_call_traditional(call_id: str, name: str, args: dict | None = None) -> str:
    """使用传统标记编码函数调用"""
    return encode("call", call_id, name=name, args=args, use_role_markers=False)


def encode_result_traditional(call_id: str, result: dict | None = None) -> str:
    """使用传统标记编码函数结果"""
    return encode("result", call_id, result=result, use_role_markers=False)


def encode_error_traditional(call_id: str, err: dict | None = None) -> str:
    """使用传统标记编码错误"""
    return encode("error", call_id, err=err, use_role_markers=False)


# 调试和测试函数
def demo_cfp_blocks():
    """演示不同类型的 CFP 块"""
    call_id = new_call_id()

    print("=== CFP 块演示 ===")
    print("1. 函数调用:", encode_call(call_id, "get_weather", {"city": "Beijing"}))
    print("2. 参数增量:", encode_args_delta(call_id, '{"temperature": 25}'))
    print("3. 参数完成:", encode_args_complete(call_id))
    print("4. 结果:", encode_result(call_id, {"temperature": 25, "condition": "sunny"}))
    print("5. 错误:", encode_error(call_id, {"message": "API timeout"}))

    print("\n=== 传统格式 ===")
    print("1. 函数调用:", encode_call_traditional(call_id, "get_weather", {"city": "Beijing"}))


if __name__ == "__main__":
    demo_cfp_blocks()
