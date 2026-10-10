# faiserver-gpu

HUFS Finance & AI 융합학부 GPU 서버 예약 시스템([faiserver.hufs.ac.kr](https://faiserver.hufs.ac.kr))을
코딩 에이전트(Claude Code·Codex)에서 쓰기 위한 도구 모음입니다.

- **MCP 서버 `labres`**: 내 예약 조회, 이 서버에서 내 GPU 사용이 예약과 맞는지 점검, 빈 GPU 찾기, 예약·연장·반납
- **스킬 `gpu-reservation`**: GPU 작업 전에 예약을 확인하고, 없으면 사용자 확인을 받아 예약하는 규칙
- **노드 경고 에이전트**(관리자용): 예약 없이·남의 예약 GPU 를 쓰면 그 사용자의 터미널과 코딩 에이전트에 경고

## 학생·연구원: Claude Code 플러그인 설치

```
/plugin marketplace add ripepersimmon/faiserver-gpu
/plugin install faiserver-gpu@faiserver
```

**토큰 저장 (한 번만)** — MCP 가 내 권한으로 예약을 다루려면 개인 API 토큰이 필요합니다.

1. [faiserver.hufs.ac.kr/tokens](https://faiserver.hufs.ac.kr/tokens) 에서 학교 계정으로 로그인 → **발급**
2. 터미널에서 저장 (토큰은 비밀번호와 같습니다. 대화창·코드·깃에 붙여넣지 마세요)
   - GPU 서버에서: `labres-mcp --save-token` → 붙여넣고 Enter
   - 그 밖의 컴퓨터: `mkdir -p ~/.config/labres && (umask 077; cat > ~/.config/labres/token)` → 붙여넣고 Enter, Ctrl-D
3. 저장 즉시 반영됩니다(재시작 불필요). *"내 GPU 예약 확인해 줘"*, *"예약 없으면 지금부터 3시간 GPU 하나 예약해 줘"*

토큰으로는 **본인 예약** 관련 기능만 쓸 수 있습니다(관리자 계정 토큰도 마찬가지). 기본 유효기간 180일, 포털에서 언제든 폐기할 수 있습니다.
학생 직접 예약이 막힌 기간에는 학생 계정의 예약 생성은 거부되고 조회만 됩니다(수업 배정 시간에 실습).

### Codex

GPU 서버에는 `labres-mcp` 가 설치되어 있습니다. `~/.codex/config.toml`:

```toml
[mcp_servers.labres]
command = "labres-mcp"
```

다른 컴퓨터에서는 이 저장소를 받아 `command = "python3"`, `args = ["-I", "/경로/faiserver-gpu/node-agent/labres_mcp.py"]`.

### MCP 도구

| 도구 | 하는 일 |
|---|---|
| `check_my_gpu_usage` | 이 서버에서 지금 내 예약 GPU(→ `CUDA_VISIBLE_DEVICES`), 내 GPU 프로세스가 예약과 맞는지, 서버 경고 |
| `get_my_reservations` | 진행 중·예정 예약 목록(id·서버·GPU·시간·수업 배정 여부) |
| `find_available_gpus` | 시간대(기본 지금부터 1시간)에 예약 가능한 GPU |
| `reserve_gpu` | 예약. 같은 시간대에 내 예약이 있으면 만들지 않음. GPU 생략 시 빈 GPU 자동 선택 |
| `extend_reservation` / `release_reservation` | 연장 / 반납(되돌릴 수 없음) |

시각 입력은 `now`, `14:30`(오늘 KST), `2026-10-11 09:00`(KST), ISO8601 을 받습니다.
예약한 GPU 로 실행할 때는 `CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=<번호>` 를 함께 지정하세요.

## 관리자: GPU 노드 경고 에이전트

각 GPU 노드에서 root 로 한 번 설치합니다(재실행 안전, `--uninstall` 로 제거).

```bash
git clone https://github.com/ripepersimmon/faiserver-gpu.git
cd faiserver-gpu && git -c advice.detachedHead=false checkout v1.0.2   # 검토된 태그를 고정해서 설치 (main 을 그대로 쓰지 않음)
sudo bash node-agent/install.sh          # 예약 웹앱의 NODE_AGENT_TOKEN 을 숨김 입력으로 묻습니다
# 호스트명이 예약 시스템 서버 이름과 다르면: sudo NODE_NAME=<서버이름> bash ...
```

root 로 실행되는 코드이므로 `install.sh` 는 번들이 root 또는 sudo 를 실행한 관리자 소유이고 남이 쓸 수 없는 파일뿐일 때만,
인터프리터가 root 소유 `/usr/bin/python3` 일 때만 진행합니다. 설치 끝에 샌드박스가 적용된 systemd 서비스로 한 번 실제 실행해 확인합니다.

설치되는 것:

| 구성 | 역할 |
|---|---|
| `labres-gpu-warn` (systemd 타이머 1분) | 예약 ↔ 실제 GPU 프로세스 사용자 대조 → 그 사용자 터미널(tmux 포함)에 경고 배너 |
| `/etc/profile.d/labres-gpu.sh` | SSH 로그인 시 GPU 예약 현황·내 예약·경고. 비대화형 로그인 셸(Codex `bash -lc`)엔 경고만 stderr |
| `labres-agent-hook` + `/etc/claude-code/managed-settings.json` | Claude Code 세션에 경고 주입(SessionStart·UserPromptSubmit·PostToolUse) |
| `/etc/claude-code/CLAUDE.md` 블록 | 전 사용자 Claude Code 규칙: GPU 작업 전 예약 확인 |
| `labres-mcp`, `labres-check` | 사용자용 MCP 서버·점검 명령 |

경고 대상은 그 GPU 에 256MiB 이상 올렸거나 GPU 사용률 5% 이상인 사용자입니다. 경고 내용은 본인만 볼 수 있고
(터미널 · `/run/labres-gpu-warn/user/<본인>/warn`), 다른 사용자에겐 이름 없는 현황(`GPU2 예약 없음 · 사용 중`)만 보입니다.
사용자 디렉터리는 모든 사람 계정에 미리 만들어 두어 디렉터리 존재로 누가 경고 중인지 알 수 없고, 계정이 개인 전용 그룹이면
남이 `stat` 조차 할 수 없게 둡니다(공용 그룹 계정은 파일 존재 여부만 보임).
서버에서 온 사유·프로세스 이름은 제어문자를 지운 뒤에만 터미널·에이전트에 보여 주고, 서버 응답이나 `nvidia-smi` 를
읽지 못하면 경고하지 않으며, 프로세스를 종료하지 않습니다. 웹앱 주소는 https 만 허용하고 리다이렉트를 따라가지 않습니다(토큰 보호).

이 저장소에는 예약 웹앱 자체는 들어 있지 않습니다. 에이전트는 웹앱의 `/agent/node/<node>`, MCP 는 개인 토큰용 예약 API 를 사용합니다.

## 보안 메모

- 토큰은 `~/.config/labres/token` 이 본인 소유·`600` 일 때만 읽습니다. `labres-mcp --save-token` 이 그렇게 만듭니다.
- 이 저장소의 코드가 노드에서 root 로 돌므로 GitHub 계정에 2단계 인증과 `main` 브랜치 보호를 켜 두고, 노드에는 태그를 고정해 설치하세요.

## 개발

Python 3.7+ 표준 라이브러리만 씁니다. 테스트: `python3 -m pytest tests -q`
