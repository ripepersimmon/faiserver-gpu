#!/bin/sh
# labres-check — 이 GPU 서버의 예약 현황 + 내 예약 + 내 경고. 경고가 있으면 종료코드 1.
# (labres-gpu-warn 이 1분마다 쓰는 /run/labres-gpu-warn 파일을 읽는다. 에이전트가 멈췄으면 2)
d=/run/labres-gpu-warn
u=$(id -un)
fresh() { [ -f "$1" ] && [ -n "$(find "$1" -mmin -3 2>/dev/null)" ]; }
if ! fresh "$d/status.txt"; then
  echo "예약 경고 에이전트 정보가 없습니다(labres-gpu-warn 멈춤?). 포털이나 'labres-mcp --check' 로 확인하세요." >&2
  exit 2
fi
cat "$d/status.txt"
fresh "$d/user/$u/info" && cat "$d/user/$u/info"
if fresh "$d/user/$u/warn"; then
  cat "$d/user/$u/warn"
  exit 1
fi
echo "✓ 내 GPU 사용에 대한 경고 없음"
