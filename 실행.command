#!/bin/zsh
# 설치나 모델 다운로드 없이 이미 준비된 프로젝트 환경만 실행합니다.
set -eu
HANDOFF_PROJECT_DIR=${0:A:h}
cd "$HANDOFF_PROJECT_DIR"
if [[ ! -x .venv-app/bin/python ]]; then
  print '실행 환경이 없습니다. docs/local-web-guide.md의 최초 설치 안내를 확인하세요.'
  read '?Enter를 누르면 종료합니다. '
  exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
exec .venv-app/bin/python scripts/run_web.py
