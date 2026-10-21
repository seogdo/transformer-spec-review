"""Start one provider-specific web service without making an AI request.

The supplied course project selects its LLM at server startup. We preserve that
boundary: the browser displays the selected backend, but cannot mutate the
process configuration. API credentials stay in this server process, never in a
URL, browser form, command-line argument, or committed file.
"""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

ENV_KEYS = frozenset({
    "LLM_BACKEND", "OPENAI_API_KEY", "OPENAI_MODEL",
    "HANDOFF_OLLAMA_URL", "HANDOFF_OLLAMA_MODEL",
})


def load_env_file(path: Path) -> None:
    """Read a small literal KEY=VALUE file; never execute shell expressions.

    Existing process variables take precedence. Only this app's explicit keys
    are accepted. No interpolation, command substitution, shell sourcing, or
    key values in exceptions/logs. Quoted values are stripped, not evaluated.
    """
    if not path.is_file():
        return
    if path.stat().st_size > 16 * 1024:
        raise ValueError("설정 파일이 너무 큽니다. .env.example 형식을 사용하세요.")
    pending = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name not in ENV_KEYS:
            continue
        if not sep or name in pending:
            raise ValueError("설정 파일의 항목 형식 또는 중복을 확인하세요.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("설정 파일의 따옴표를 확인하세요.")
            value = value[1:-1]
        if any(ord(char) < 32 for char in value):
            raise ValueError("설정 값에는 제어 문자를 사용할 수 없습니다.")
        pending[name] = value
    for name, value in pending.items():
        os.environ.setdefault(name, value)


def configure(argv=None):
    parser = argparse.ArgumentParser(description="변압기 수주 기술사양 검토 AI")
    parser.add_argument("--backend", choices=("openai", "ollama"),
                        help="서버 시작 시 AI 선택. 생략하면 .env/환경변수, 없으면 OpenAI")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env",
                        help="서버 전용 설정 파일. 키 값을 명령줄에 입력하지 마세요.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("포트는 1024~65535 범위여야 합니다.")
    try:
        load_env_file(args.env_file)
    except (OSError, UnicodeError, ValueError):
        # Never echo a malformed line: it might contain a credential.
        parser.error("설정 파일을 읽지 못했습니다. .env.example 형식을 확인하세요.")
    backend = args.backend or os.environ.get("LLM_BACKEND", "openai")
    if backend not in {"openai", "ollama"}:
        parser.error("LLM_BACKEND는 openai 또는 ollama여야 합니다.")
    os.environ["LLM_BACKEND"] = backend
    return args


def main(argv=None):
    args = configure(argv)
    # Configure before importing the app: create_app freezes provider/model.
    import uvicorn
    from handoff_review.web_app import app
    label = "OpenAI API · 외부 전송/과금" if os.environ["LLM_BACKEND"] == "openai" else "Ollama · 로컬 추론"
    print(f"수주 인계 검토: http://127.0.0.1:{args.port} · {label}", flush=True)
    print("종료는 Ctrl+C입니다. 로컬 전용이며 공개 서버로 노출하지 마세요.", flush=True)
    print("서버 시작·페이지 조회만으로 AI를 호출하지 않습니다. 설정 상태는 화면에서 확인하세요.", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
