"""Start the local prototype; no models or packages are downloaded automatically."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    import uvicorn
    from handoff_review.web_app import app
    print("수주 인계 검토: http://127.0.0.1:8765", flush=True)
    print("종료는 Ctrl+C입니다. 로컬 전용이며 공개 서버로 노출하지 마세요.", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=8765, access_log=False)
