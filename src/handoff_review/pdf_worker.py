"""Parse one local upload in an isolated process with a wall-time cap in its caller.

This limits failure propagation; it is not a production-grade malicious-PDF sandbox.
The default is the strict synthetic row template. Optional ``text`` mode reads
only declared synthetic metadata and page text for a separate extraction stage.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys


def main() -> int:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    except (ImportError, OSError, ValueError):
        pass
    from .pdf_ingest import extract_document, PdfInputError
    try:
        arguments = sys.argv[1:]
        if len(arguments) not in (2, 3):
            raise ValueError("PDF 경로와 문서 역할, 선택적으로 text 모드를 지정해야 합니다.")
        path, role = arguments[:2]
        if len(arguments) == 3 and arguments[2] != "text":
            raise ValueError("지원하지 않는 PDF 읽기 모드입니다.")
        if len(arguments) == 3:
            from .text_pdf import read_text_document
            result = read_text_document(Path(path).read_bytes(), role)
        else:
            result = extract_document(Path(path).read_bytes(), role)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (PdfInputError, ValueError) as exc:
        print(json.dumps({"error": str(exc)[:600]}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"error": "PDF 내용을 안전하게 읽지 못했습니다."}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
