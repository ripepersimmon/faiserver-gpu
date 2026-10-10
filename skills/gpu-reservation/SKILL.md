---
name: gpu-reservation
description: HUFS Finance & AI GPU 서버(faiserver.hufs.ac.kr 예약제) 에서 GPU 를 쓰기 전 예약을 확인하고, 없으면 사용자 확인 후 예약·연장·반납한다. GPU 학습·추론·CUDA 코드를 실행하려 할 때, "[GPU 예약 경고]" 가 보일 때, 사용자가 GPU 예약·빈 GPU·연장·반납을 물을 때 사용.
---

# GPU 예약 (faiserver.hufs.ac.kr)

학교 GPU 서버는 예약제다. 예약 없이, 또는 남이 예약한 GPU 를 쓰면 서버가 터미널·에이전트에 `[GPU 예약 경고]` 를 띄운다.
이 플러그인의 `labres` MCP 도구로 예약을 다룬다(시각은 모두 한국 시간 KST).

## GPU 작업 전에

1. `check_my_gpu_usage` — 지금 이 서버에 내 예약이 있는지, 내 GPU 프로세스가 예약과 맞는지 본다.
2. 예약이 있으면 그 GPU 로만 실행한다:
   `CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=<예약한 GPU 번호> python train.py`
   (번호는 nvidia-smi·예약 시스템 기준이다. `PCI_BUS_ID` 를 같이 줘야 같은 카드를 가리킨다.)
3. 예약이 없으면 사용자에게 알리고, **사용자가 원할 때만** 예약한다:
   - `find_available_gpus` (start·end 또는 hours, node) 로 빈 GPU 를 찾아 보여 주고
   - 사용자가 서버·GPU·시간을 확인하면 `reserve_gpu` 를 부른다. 같은 시간대에 내 예약이 이미 있으면 새로 만들지 않는다.

## 규칙

- 예약·연장(`extend_reservation`)·반납(`release_reservation`)은 사용자에게 시간·GPU 를 확인받은 뒤에만 한다. 반납은 되돌릴 수 없다.
- `[GPU 예약 경고]` 가 보이면 사용자에게 그대로 전달하고, 해결 전에는 새 GPU 작업을 시작하지 않는다.
  실행 중인 작업을 임의로 종료하지 말고 사용자에게 먼저 묻는다.
- 수업 배정 예약(수업 배정 표시)은 학생이 연장·반납할 수 없다 — 담당 교수에게 문의하도록 안내한다.
- 학생 직접 예약이 막힌 기간엔 학생 계정의 `reserve_gpu` 가 거부된다. 오류 문구를 그대로 전달한다.

## 도구가 "API 토큰이 없습니다" 라고 할 때

사용자에게 다음을 안내한다(토큰을 대화창에 붙여넣게 하지 말 것 — 비밀번호와 같다):

1. https://faiserver.hufs.ac.kr/tokens 에서 학교 계정으로 로그인 → 토큰 발급
2. 터미널에서 저장:
   - GPU 서버: `labres-mcp --save-token` 실행 후 붙여넣기
   - 그 밖의 컴퓨터: `mkdir -p ~/.config/labres && (umask 077; cat > ~/.config/labres/token)` 실행 → 붙여넣고 Enter, Ctrl-D
3. Claude Code 를 다시 시작하거나 `/mcp` 에서 labres 를 재연결한다.
