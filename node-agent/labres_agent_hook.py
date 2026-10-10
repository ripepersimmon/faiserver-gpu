#!/usr/bin/env python3
"""labres-agent-hook — Claude Code 훅: GPU 예약 경고를 코딩 에이전트 컨텍스트에 넣는다.

/etc/claude-code/managed-settings.json(install.sh 가 병합)에서 이 노드의 모든 사용자 세션에 걸린다.
labres-gpu-warn(root 타이머)이 1분마다 쓰는 /run/labres-gpu-warn/user/<나>.warn·.info 를 읽을 뿐이라
빠르고, 어떤 오류에도 조용히 exit 0 한다(훅 실패로 사용자 작업을 막지 않는다).

  labres-agent-hook SessionStart      경고 + 이 노드의 내 예약 요약 (stdout → 컨텍스트)
  labres-agent-hook UserPromptSubmit  경고가 있을 때만 (stdout → 컨텍스트)
  labres-agent-hook PostToolUse       경고가 새로 생겼거나 바뀌었거나 10분 지났을 때만
                                      (JSON additionalContext — 매 명령마다 같은 경고로 컨텍스트를 채우지 않는다)
"""

import hashlib
import json
import os
import pwd
import sys
import time

USER_DIR = "/run/labres-gpu-warn/user"
FRESH_SECONDS = 180          # 에이전트가 3분 넘게 갱신 안 했으면(멈춤) 옛 경고를 믿지 않는다
REPEAT_SECONDS = 600         # PostToolUse 같은 경고 재주입 간격
STATE_FILE = os.path.expanduser("~/.cache/labres/hook-state.json")


def _read_fresh(path):
    try:
        if time.time() - os.stat(path).st_mtime > FRESH_SECONDS:
            return None
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _post_tool_due(session, text):
    """이 세션에 이 경고를 지금 주입할 차례인지 + 상태 갱신."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    now = time.time()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    last = state.get(session) or {}
    if last.get("h") == digest and now - last.get("t", 0) < REPEAT_SECONDS:
        return False
    state = {k: v for k, v in state.items() if now - v.get("t", 0) < 86400}
    state[session] = {"h": digest, "t": now}
    try:
        os.makedirs(os.path.dirname(STATE_FILE), mode=0o700, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass
    return True


def main(argv):
    event = argv[1] if len(argv) > 1 else ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    user = pwd.getpwuid(os.getuid()).pw_name
    warn = _read_fresh(os.path.join(USER_DIR, user + ".warn"))
    if event == "SessionStart":
        info = _read_fresh(os.path.join(USER_DIR, user + ".info"))
        parts = [p for p in (warn, info) if p]
        if parts:
            print("\n\n".join(parts))
    elif event == "UserPromptSubmit":
        if warn:
            print(warn)
    elif event == "PostToolUse":
        if warn and _post_tool_due(str(payload.get("session_id") or "-"), warn):
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse", "additionalContext": warn}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # noqa: BLE001 — 훅은 절대 사용자 작업을 막지 않는다
        sys.exit(0)
