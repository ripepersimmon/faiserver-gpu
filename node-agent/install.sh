#!/usr/bin/env bash
# scripts/node-agent/install.sh — GPU 노드에 예약 경고 + 코딩 에이전트(Claude Code·Codex) 연동 설치
#
# 각 GPU 노드(fai001·fai002·fai003·gpu-b6000·fai-L40S)에서 root 로 1회. 재실행 안전(멱등).
#   sudo bash install.sh                       # 토큰은 숨김 입력으로 묻는다(처음 한 번)
#   sudo NODE_NAME=fai-L40S bash install.sh    # 호스트명이 예약 시스템 서버 이름과 다를 때
#   sudo bash install.sh --uninstall           # 되돌리기 (/etc/labres/agent.conf 는 남김)
# 환경변수(선택): AGENT_TOKEN(웹앱 .env NODE_AGENT_TOKEN 값), NODE_NAME, LABRES_URL
#
# 무엇을 설치하는가:
#   /usr/local/bin/labres-gpu-warn     경고 에이전트(root, 1분 주기) — 예약 ↔ 실제 GPU 사용자 대조
#   /usr/local/bin/labres-mcp          사용자용 MCP 서버(예약 조회·생성·연장·반납)
#   /usr/local/bin/labres-agent-hook   Claude Code 훅(경고를 에이전트 컨텍스트에 주입)
#   /usr/local/bin/labres-check        사용자용 점검 명령
#   /etc/labres/agent.conf (600)       에이전트 설정·토큰 — 이미 있으면 유지, 인자로 준 값만 교체
#   /etc/labres/node (644)             이 노드의 예약 시스템 서버 이름(labres-mcp 가 읽음)
#   /etc/systemd/system/labres-gpu-warn.{service,timer}
#   /etc/profile.d/labres-gpu.sh       로그인 배너 + 로그인 셸(Codex bash -lc) 경고
#   /etc/claude-code/managed-settings.json  훅 3개 병합(기존 설정 보존, 첫 수정 전 .orig-날짜 백업)
#   /etc/claude-code/CLAUDE.md         '<!-- labres:begin/end -->' 사이 예약 규칙 블록만 관리
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "root 로 실행하세요: sudo bash $0" >&2; exit 1; }
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN=/usr/local/bin
ETC=/etc/labres
CONF=$ETC/agent.conf
CC=/etc/claude-code
STAMP=$(date +%Y%m%d)

PY=$(command -v python3) || { echo "python3 가 필요합니다." >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 7))' || { echo "python3 3.7 이상이 필요합니다." >&2; exit 1; }

# 파이썬 도구는 여기서 검증한 인터프리터로 고정하고 -I 로 실행한다. 사용자 PATH 의 python3(pyenv·conda·
# 깨진 venv)나 PYTHONPATH 가 끼어들면 훅이 실패하거나 MCP 가 문법 오류로 죽기 때문이다.
install_py() {   # $1=원본 $2=설치 경로
  { printf '#!%s -I\n' "$PY"; tail -n +2 "$1"; } > "$2.tmp"
  chmod 755 "$2.tmp"
  mv -f "$2.tmp" "$2"
}

# managed-settings.json 훅 병합/제거 · CLAUDE.md 블록 병합/제거 (기존 내용은 건드리지 않는다)
claude_code_merge() {   # $1 = install | uninstall
  install -d -m 755 "$CC"
  for f in "$CC/managed-settings.json" "$CC/CLAUDE.md"; do
    [ -f "$f" ] && [ ! -e "$f.orig-$STAMP" ] && cp -p "$f" "$f.orig-$STAMP"
  done
  "$PY" - "$1" "$CC" "$HERE/CLAUDE.md.block" <<'PY'
import json, os, re, sys
mode, cc, block_path = sys.argv[1], sys.argv[2], sys.argv[3]
HOOK = "/usr/local/bin/labres-agent-hook"

path = os.path.join(cc, "managed-settings.json")
data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)          # 깨진 JSON 이면 여기서 멈춘다(덮어써서 기존 정책을 날리지 않게)
hooks = data.get("hooks") or {}
for ev in list(hooks):               # 우리 훅만 걷어낸다(재설치 시 중복 방지)
    groups = []
    for g in hooks[ev]:
        hs = [h for h in g.get("hooks", []) if HOOK not in h.get("command", "")]
        if hs:
            groups.append(dict(g, hooks=hs))
    if groups:
        hooks[ev] = groups
    else:
        del hooks[ev]
if mode == "install":
    def ent(ev, matcher=None):
        g = {"hooks": [{"type": "command", "command": "%s %s" % (HOOK, ev), "timeout": 10}]}
        if matcher:
            g["matcher"] = matcher
        return g
    hooks.setdefault("SessionStart", []).append(ent("SessionStart"))
    hooks.setdefault("UserPromptSubmit", []).append(ent("UserPromptSubmit"))
    hooks.setdefault("PostToolUse", []).append(ent("PostToolUse", "Bash"))
if hooks:
    data["hooks"] = hooks
else:
    data.pop("hooks", None)
if not data:                         # 우리 훅만 있던 파일이면 지운다(빈 정책 파일을 남기지 않게)
    if os.path.exists(path):
        os.unlink(path)
else:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)

md = os.path.join(cc, "CLAUDE.md")
text = open(md, encoding="utf-8").read() if os.path.exists(md) else ""
text = re.sub(r"\n*<!-- labres:begin.*?<!-- labres:end -->\n?", "\n", text, flags=re.S).strip("\n")
if mode == "install":
    block = open(block_path, encoding="utf-8").read().strip("\n")
    text = (text + "\n\n" + block if text else block)
if text.strip():
    with open(md, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    os.chmod(md, 0o644)
elif os.path.exists(md):
    os.unlink(md)
PY
}

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now labres-gpu-warn.timer 2>/dev/null || true
  rm -f /etc/systemd/system/labres-gpu-warn.service /etc/systemd/system/labres-gpu-warn.timer
  systemctl daemon-reload
  rm -f "$BIN/labres-gpu-warn" "$BIN/labres-mcp" "$BIN/labres-agent-hook" "$BIN/labres-check" \
        /etc/profile.d/labres-gpu.sh "$ETC/node"
  rm -rf /run/labres-gpu-warn /var/lib/labres-gpu-warn
  claude_code_merge uninstall
  echo "제거했습니다. 토큰이 든 $CONF 는 남겨 두었습니다(완전 삭제: rm -r $ETC)."
  exit 0
fi

# 1) 실행 파일
install_py "$HERE/labres_gpu_warn.py"   "$BIN/labres-gpu-warn"
install_py "$HERE/labres_mcp.py"        "$BIN/labres-mcp"
install_py "$HERE/labres_agent_hook.py" "$BIN/labres-agent-hook"
install -m 755 "$HERE/labres-check.sh"      "$BIN/labres-check"

# 2) 설정 — 기존 파일은 유지하고, 인자로 받은 값만 바꾼다
install -d -m 755 "$ETC"
[ -f "$CONF" ] || install -m 600 "$HERE/agent.conf.example" "$CONF"
chmod 600 "$CONF"
if [ -z "${AGENT_TOKEN:-}" ] && ! grep -qE '^AGENT_TOKEN=.+' "$CONF"; then
  if [ -t 0 ]; then
    read -rsp "NODE_AGENT_TOKEN (fai003 .env 의 값): " AGENT_TOKEN; echo
  fi
  [ -n "${AGENT_TOKEN:-}" ] || { echo "AGENT_TOKEN 이 필요합니다(sudo AGENT_TOKEN=... bash $0)." >&2; exit 1; }
fi
export AGENT_TOKEN="${AGENT_TOKEN:-}"   # read 로 받은 값도 아래 python 이 보게
"$PY" - "$CONF" <<'PY'
import os, sys
path = sys.argv[1]
updates = {k: os.environ[k] for k in ("AGENT_TOKEN", "NODE_NAME", "LABRES_URL") if os.environ.get(k)}
lines = open(path, encoding="utf-8").read().splitlines()
seen = set()
for i, line in enumerate(lines):
    k = line.split("=", 1)[0].strip()
    if k in updates and not line.lstrip().startswith("#"):
        lines[i] = "%s=%s" % (k, updates[k]); seen.add(k)
lines += ["%s=%s" % (k, v) for k, v in updates.items() if k not in seen]
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
PY
NODE=$(sed -n 's/^NODE_NAME=//p' "$CONF" | tail -1)
NODE=${NODE:-$(hostname -s)}
echo "$NODE" > "$ETC/node"; chmod 644 "$ETC/node"

# 3) 1분 주기 타이머
install -m 644 "$HERE/labres-gpu-warn.service" /etc/systemd/system/labres-gpu-warn.service
install -m 644 "$HERE/labres-gpu-warn.timer"   /etc/systemd/system/labres-gpu-warn.timer
systemctl daemon-reload
systemctl enable --now labres-gpu-warn.timer

# 4) 로그인 배너 + 로그인 셸 경고, 5) Claude Code 훅·규칙
install -m 644 "$HERE/labres-gpu.profile.sh" /etc/profile.d/labres-gpu.sh
claude_code_merge install

echo "설치 완료 — 서버 이름: $NODE"
echo "--- 점검(판정만, 터미널·파일 쓰기 없음) ---"
if ! "$BIN/labres-gpu-warn" --dry-run; then
  echo "⚠ 점검 실패 — 설치는 됐지만 경고가 동작하지 않습니다. 토큰·NODE_NAME(예약 시스템 서버 이름)·네트워크를 확인하고" >&2
  echo "  고친 뒤 이 스크립트를 다시 실행하세요. (sudo labres-gpu-warn --dry-run / journalctl -u labres-gpu-warn)" >&2
  exit 1
fi
