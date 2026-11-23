"""为外部维护状态与最终备份提供原子文件写入。"""

import json
import os
from pathlib import Path
import tempfile


def write_json_atomically(path: Path, value: dict) -> None:
    """同目录内写入并同步临时文件，完整写入后才替换现有文件。"""
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as out:
            temp = Path(out.name)
            json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
