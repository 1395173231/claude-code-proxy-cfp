import json, re, uuid
from typing import Dict, Any
import json_repair

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
           version: int = 1) -> str:
    """
    生成带 <cfp>...</cfp> 的文本片段
    """
    doc: Dict[str, Any] = dict(v=version, role=role, id=call_id)

    if role == "call":
        doc.update(name=name, args=args or {})
    elif role == "result":
        doc.update(result=result or {})
    elif role == "error":
        doc.update(err=err or {})
    else:
        raise ValueError(f"unsupported role: {role}")

    payload = json.dumps(doc, separators=(",", ":"))
    return f"{TAG_OPEN}{payload}{TAG_CLOSE}"


# ----------- 解码 -----------
_cfp_re = re.compile(rf"{re.escape(TAG_OPEN)}\s*(.+?)\s*{re.escape(TAG_CLOSE)}", re.S)


def extract_blocks(txt: str) -> list[str]:
    return _cfp_re.findall(txt)


def parse_block(raw: str) -> dict:
    doc = json_repair.loads(raw)
    return doc


