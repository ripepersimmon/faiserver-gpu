# FAI GPU 서버 사용 지침

HUFS Finance & AI GPU 서버는 여러 사람이 함께 씁니다. 한 사람이 규칙을 어기면 다른 사람의 실험이 멈추거나,
디스크가 차서 모두가 저장에 실패합니다. 이 문서는 서버에 직접 접속(SSH)해서 쓰는 모든 사람과
그 사람이 쓰는 코딩 에이전트(Claude Code·Codex)에 적용됩니다.

각 GPU 노드의 `/etc/claude-code/CLAUDE.md` 에는 이 지침의 요약이 들어 있어, 노드에서 실행한 Claude Code 는
따로 알려 주지 않아도 이 규칙을 따릅니다.

## 1. GPU 는 예약하고 쓴다

- 예약은 [예약 포털](https://faiserver.hufs.ac.kr) 이나 MCP(`reserve_gpu`, [README](README.md))로 합니다.
- 지금 내 예약과 써도 되는 GPU 번호는 `labres-check` 로 확인합니다.
- 실행할 때는 예약한 GPU 만 보이게 합니다.
  ```bash
  CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=2 python train.py
  ```
  `CUDA_DEVICE_ORDER=PCI_BUS_ID` 를 빼면 CUDA 가 매기는 번호가 `nvidia-smi`·예약 시스템 번호와 달라져
  남의 GPU 를 쓰게 될 수 있습니다.
- 예약 목적에는 프로젝트 이름 하나만 적습니다(개별 실험 내용은 적지 않습니다).
- 더 필요하면 끝나기 전에 연장하고, 일찍 끝나면 반납합니다.
- 예약 없이 쓰거나 남이 예약한 GPU 를 쓰면 본인 터미널과 코딩 에이전트에 경고가 뜨고,
  몇 분 넘게 이어지면 관리자에게도 보고됩니다.

## 2. 모델은 공유 폴더에서 받고 함께 쓴다

같은 LLM 을 사람마다 홈에 따로 받으면 수십 GB 씩 겹쳐 쌓입니다. 노드마다 공유 폴더가 있습니다.

| 경로 | 용도 |
|---|---|
| `/data/models/hf/hub` | 공개 Hugging Face 모델 캐시. 로그인하면 `HF_HUB_CACHE` 가 여기로 잡혀 있습니다 |
| `/data/models/local/<모델 이름>/` | Hugging Face 형식이 아닌 모델(GGUF, 직접 양자화·변환한 가중치 등) |

누구나 새 모델을 받을 수 있고 누구나 읽을 수 있지만, **받은 모델은 받은 사람만 고칠 수 있습니다.**

- **받기 전에 이미 있는지 확인합니다.** `ls /data/models/hf/hub` — 폴더 이름이 `models--<조직>--<모델>` 입니다.
  있으면 오프라인으로 불러옵니다(`HF_HUB_OFFLINE=1`, 또는 코드에서 `local_files_only=True`).
  없으면 받으면 다음 사람이 함께 씁니다.
- `echo $HF_HUB_CACHE` 가 `/data/models/hf/hub` 이면 transformers·vLLM·`hf download` 가 알아서 이 폴더를 씁니다.
  코드의 `cache_dir` 나 vLLM 의 `--download-dir` 를 홈으로 지정하지 마세요.
- 자동 설정은 bash 로그인 셸(SSH 접속)에만 적용됩니다. zsh, VS Code Remote, Jupyter, cron·systemd 서비스에서는 직접 지정합니다.
  ```bash
  export HF_HUB_CACHE=/data/models/hf/hub
  ```
- **비공개·gated·토큰이 필요한 모델과 데이터셋은 공유 폴더에 받지 마세요.** 같은 노드의 누구나 읽을 수 있습니다.
  개인 캐시로 받습니다: `HF_HUB_CACHE=$HOME/.cache/huggingface/hub`.
- **공유 폴더의 모델은 다른 사람이 받은 것입니다.** 받은 사람이 파일을 바꿔 둘 수 있으므로
  `trust_remote_code=True` 나 pickle 형식(`.bin`·`.pt`, `torch.load(weights_only=False)`)으로 불러오지 마세요 —
  내 계정으로 남의 코드가 실행될 수 있습니다. safetensors(`use_safetensors=True`)만 쓰고, 원격 코드가 꼭 필요한 모델은
  개인 캐시로 따로 받습니다.
- 남이 받은 모델 폴더에 파일을 더 받으려 하면 PermissionError 가 납니다(정상입니다). 그 폴더를 chmod·삭제하지 말고
  개인 캐시로 받으세요.
- `HF_HOME` 은 공유 폴더로 옮기지 마세요. 그 안에는 개인 Hugging Face 토큰이 저장됩니다.
- 라이선스 동의가 필요한 모델(gated)은 각자 자기 Hugging Face 계정으로 동의한 뒤 씁니다.
- 공유 폴더에서 남이 받은 파일은 지우거나 고치지 않습니다. 안 쓰는 모델 정리는 관리자에게 요청합니다.
- 공유 폴더는 노드마다 따로입니다. 다른 노드에 있는 모델이 이 노드에는 없을 수 있습니다.
- 이미 홈에 받아 둔 공개 모델이 있으면 공유 폴더에 있는지 확인하고 홈 쪽을 지워 주세요
  (`du -sh ~/.cache/huggingface`).

## 3. 저장공간을 아낀다

노드 디스크는 모두가 함께 씁니다. 사용률이 80% 를 넘으면 관리자에게 알림이 가고, 90% 를 넘으면 정리를 요청합니다.

- 홈에는 코드·설정·작은 결과물만 둡니다. 큰 데이터셋·체크포인트를 홈에 쌓지 않습니다.
- 체크포인트는 필요한 만큼만 남깁니다(예: transformers `save_total_limit`). 끝난 실험의 중간 체크포인트는 지웁니다.
- 가끔 내 사용량을 확인합니다.
  ```bash
  du -sh ~/* ~/.cache/* 2>/dev/null | sort -h | tail
  ```
- 쌓인 캐시 정리: `pip cache purge`, `conda clean -a`.

## 4. 오래 걸리는 작업

- `tmux`·`nohup` 으로 띄우되, 예약 시간 안에 끝나도록 잡습니다. 길어지면 끝나기 전에 연장합니다.
- 예약이 끝나면 남은 프로세스를 정리합니다. `nvidia-smi` 로 내 프로세스가 남아 있는지 확인하세요.
- 쓰지 않는 GPU 를 붙잡아 두지 않습니다(커널·노트북·추론 서버를 띄워 놓고 방치하지 않기).

## 5. 코딩 에이전트

- 에이전트도 사람과 같은 규칙을 따릅니다. 노드에서 쓰는 Claude Code 에는 규칙이 이미 들어가 있고,
  예약 조회·예약은 플러그인([README](README.md))으로 할 수 있습니다.
- 에이전트가 모델을 받거나 학습을 돌리기 전에 공유 폴더와 예약을 확인하는지 지켜봐 주세요.
- 에이전트에게 `sudo`, 남의 프로세스 종료, 공유 폴더 정리를 시키지 않습니다.

## 6. 하지 말 것

- 예약 없이 GPU 쓰기, 남이 예약한 GPU 쓰기
- 남의 프로세스 종료, 남의 파일(공유 폴더 포함) 삭제·수정
- 계정·비밀번호·API 토큰 공유
- 관리자와 상의 없이 시스템 설정 바꾸기(드라이버·시스템 패키지·서비스)

## 7. 문의

예약 포털 [게시판](https://faiserver.hufs.ac.kr/board) 에 남겨 주세요. 문의 글은 작성자 본인과 관리자만 볼 수 있습니다.
