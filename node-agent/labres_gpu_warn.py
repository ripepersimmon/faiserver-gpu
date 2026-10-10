#!/usr/bin/env python3
"""labres-gpu-warn — GPU 노드 예약 경고 에이전트 (각 GPU 노드에서 root, systemd 타이머 1분 주기).

예약 웹앱(LABRES_URL/agent/node/<node>)에서 이 노드의 현재·임박 예약과 관리자 차단을 받아,
nvidia-smi 로 본 실제 GPU 프로세스의 OS 사용자와 대조한다. 어긋나면:
  1) 그 사용자의 모든 터미널(/dev/pts/*, tmux·screen 창 포함)에 경고 배너를 쓴다 — 사람용
  2) /run/labres-gpu-warn/user/<user>.warn 을 쓴다 — 코딩 에이전트용. 로그인 셸(profile.d,
     Codex 의 bash -lc)과 Claude Code 훅(labres-agent-hook)이 이 파일을 읽어 컨텍스트에 넣는다
  3) /run/labres-gpu-warn/status.txt — 이 노드 GPU 예약 현황(사용자 이름 없음), 로그인 배너용
     /run/labres-gpu-warn/user/<user>.info — 그 사용자의 이 노드 예약 요약

경고 종류:
  blocked    관리자가 막아 둔(수업 중·점검) GPU 를 사용
  mismatch   다른 사람이 지금 예약한 GPU 를 사용
  unreserved 예약 없이 사용. 면제 사용자(exempt_users)는 건너뛰되, 곧 남의 예약이 시작되면 경고
  ending     내 예약이 곧 끝나는데 GPU 프로세스가 돌고 있음(안내, 예약 1건당 1회)

안전 원칙: 서버 응답이나 nvidia-smi 를 못 읽으면 아무것도 쓰지 않는다(오탐 금지 — 이전 파일은
3분 뒤 자연히 무시된다). 프로세스는 절대 건드리지 않는다. 컨테이너(hub 실습 세션) 프로세스와
시스템 계정(uid < MIN_UID)은 사람 터미널이 없으므로 판정에서 뺀다.

설정: /etc/labres/agent.conf (KEY=VALUE, root 600) — install.sh 가 만든다.
  LABRES_URL      기본 https://faiserver.hufs.ac.kr
  AGENT_TOKEN     웹앱 .env NODE_AGENT_TOKEN 과 같은 값 (필수)
  NODE_NAME       예약 시스템의 서버 이름 (기본: 호스트명)
  REPEAT_MINUTES  같은 경고를 터미널에 다시 띄우는 간격 (기본 30)
  ENDING_MINUTES  예약 종료 몇 분 전에 안내할지 (기본 10)
  UPCOMING_MINUTES 남의 예약이 몇 분 안에 시작되면 미리 경고할지 (기본 30)
  MIN_UID         이보다 작은 uid 는 판정 제외 (기본 1000)
  MIN_MEM_MIB     한 사용자가 GPU 에 올린 메모리가 이 미만이고 GPU 사용률도 BUSY_UTIL 미만이면
                  '안 쓰는 중'으로 본다 — 빈 MPS 데몬·잔여 컨텍스트 오탐 방지 (기본 256)
  BUSY_UTIL       GPU 사용률(%)이 이 이상이면 그 GPU 의 프로세스는 메모리와 무관하게 사용 중 (기본 5)
  TTY_WARN        0 이면 터미널 쓰기 끄기(파일만) (기본 1)

사용: labres-gpu-warn [--dry-run] [--config PATH]
  --dry-run  판정 결과만 출력하고 터미널·파일에는 쓰지 않는다 (설치 직후 점검용)

Python 3.6+ 표준 라이브러리만 쓴다(노드에 pip 설치 없이 동작).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
DEFAULTS = {
    "LABRES_URL": "https://faiserver.hufs.ac.kr",
    "AGENT_TOKEN": "",
    "NODE_NAME": "",
    "REPEAT_MINUTES": "30",
    "ENDING_MINUTES": "10",
    "UPCOMING_MINUTES": "30",
    "MIN_UID": "1000",
    "MIN_MEM_MIB": "256",
    "BUSY_UTIL": "5",
    "TTY_WARN": "1",
    "STATE_DIR": "/var/lib/labres-gpu-warn",
    "RUN_DIR": "/run/labres-gpu-warn",
}
# 컨테이너 cgroup 경로(시스템 docker/containerd/k8s/podman). 'docker' 같은 낱말 포함만으로 거르면
# `systemd-run --scope --unit=docker-x` 로 쉽게 빠져나가므로 컨테이너 id 모양까지 본다.
# 사용자 슬라이스(user.slice) 아래는 rootless 컨테이너라도 그 사람의 작업이므로 판정 대상으로 남긴다.
_CONTAINER_CG = re.compile(r"docker[-/][0-9a-f]{12,}|/kubepods|cri-containerd-[0-9a-f]{12,}|libpod-[0-9a-f]{12,}")
KIND_LABEL = {"blocked": "위반", "mismatch": "위반", "unreserved": "경고", "ending": "안내"}


# --------------------------------------------------------------------------- #
# 설정 / 시각
# --------------------------------------------------------------------------- #
def load_config(path: str) -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    if not cfg["NODE_NAME"]:
        cfg["NODE_NAME"] = socket.gethostname().split(".")[0]
    return cfg


_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$")


def parse_ts(value: str) -> datetime:
    """DB 시각 문자열(대개 'YYYY-MM-DDTHH:MM:SS.mmmZ') → aware UTC datetime. 3.6 호환 파서."""
    m = _TS_RE.match(value.strip())
    if not m:
        raise ValueError("bad timestamp: %r" % value)
    dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
    if m.group(2):
        dt = dt.replace(microsecond=int((m.group(2)[1:] + "000000")[:6]))
    tz = m.group(3)
    if not tz or tz == "Z":
        return dt.replace(tzinfo=timezone.utc)
    sign = 1 if tz[0] == "+" else -1
    digits = tz[1:].replace(":", "")
    off = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return dt.replace(tzinfo=timezone(sign * off)).astimezone(timezone.utc)


def kst(dt: datetime, now: datetime) -> str:
    """KST 'HH:MM' (오늘이 아니면 'M/D HH:MM')."""
    k, n = dt.astimezone(KST), now.astimezone(KST)
    if k.date() == n.date():
        return k.strftime("%H:%M")
    return "%d/%d %s" % (k.month, k.day, k.strftime("%H:%M"))


# --------------------------------------------------------------------------- #
# 수집: 서버 예약 정보 / GPU 프로세스
# --------------------------------------------------------------------------- #
def fetch_node_info(cfg: dict) -> dict:
    url = "%s/agent/node/%s?hours=12" % (cfg["LABRES_URL"].rstrip("/"), cfg["NODE_NAME"])
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + cfg["AGENT_TOKEN"],
        "User-Agent": "labres-gpu-warn/1",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _smi(args: list) -> str | None:
    try:
        out = subprocess.run(["nvidia-smi"] + args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


def _ngpu() -> int:
    try:
        return len(os.listdir("/proc/driver/nvidia/gpus"))
    except OSError:
        return 0


def _query(kind: str, fields: str) -> str | None:
    """nvidia-smi 조회. 카드 1장이 버스에서 떨어지면 일괄 조회가 통째로 실패하므로
    (labres-nvstat.sh 와 같은 사정) 인덱스별로 다시 물어 살아 있는 카드만이라도 읽는다."""
    base = ["--query-%s=%s" % (kind, fields), "--format=csv,noheader,nounits"]
    out = _smi(base)
    if out is not None:
        return out
    parts, any_ok = [], False
    for i in range(_ngpu()):
        o = _smi(["-i", str(i)] + base)
        if o is not None:
            any_ok = True
            parts.append(o)
    return "".join(parts) if any_ok else None


def in_container(cgroup_text: str) -> bool:
    return "user.slice" not in cgroup_text and bool(_CONTAINER_CG.search(cgroup_text))


def _cmd(pid: int, comm: str) -> str:
    """사람이 알아볼 명령 요약: 'python train.py …'. comm 은 15자에서 잘려서 cmdline 을 쓰되, 인자에
    API 키 같은 비밀이 섞일 수 있어 앞 두 개(프로그램·스크립트)의 파일 이름만 보인다."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            args = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
    except OSError:
        args = []
    if not args:
        return comm or "?"
    text = " ".join(os.path.basename(a) for a in args[:2]) + (" …" if len(args) > 2 else "")
    return text if len(text) <= 60 else text[:57] + "..."


def _proc_owner(pid: int):
    """(uid, user, cmd, in_container). 이미 끝난 프로세스면 None."""
    try:
        uid = os.stat("/proc/%d" % pid).st_uid
        with open("/proc/%d/comm" % pid, encoding="utf-8", errors="replace") as f:
            comm = f.read().strip()
        with open("/proc/%d/cgroup" % pid, encoding="utf-8", errors="replace") as f:
            cg = f.read()
    except OSError:
        return None
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    return uid, user, _cmd(pid, comm), in_container(cg)


def collect_gpu_procs() -> dict | None:
    """{"procs": [{gpu, pid, mem, uid, user, comm, container}], "util": {gpu: %}, "alive": [gpu...]}.
    nvidia-smi 조회 자체가 안 되면 None. 'alive' 에 없는 인덱스 = 응답 없는(고장) GPU."""
    gq = _query("gpu", "index,uuid,utilization.gpu")
    if gq is None:
        return None
    uuid2idx, util = {}, {}
    for line in gq.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 2 and p[0].isdigit():
            uuid2idx[p[1]] = int(p[0])
            util[int(p[0])] = int(p[2]) if len(p) > 2 and p[2].isdigit() else 0
    aq = _query("compute-apps", "gpu_uuid,pid,used_memory")
    if aq is None:
        return None
    procs = []
    for line in aq.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 3 or p[0] not in uuid2idx or not p[1].isdigit():
            continue
        owner = _proc_owner(int(p[1]))
        if owner is None:
            continue
        uid, user, comm, container = owner
        procs.append({
            "gpu": uuid2idx[p[0]], "pid": int(p[1]),
            "mem": int(p[2]) if p[2].isdigit() else 0,
            "uid": uid, "user": user, "comm": comm, "container": container,
        })
    return {"procs": procs, "util": util, "alive": sorted(util)}


# --------------------------------------------------------------------------- #
# 판정 (순수 함수 — tests/test_node_agent.py)
# --------------------------------------------------------------------------- #
def _same_person(res_user: str, os_user: str, aliases: dict) -> bool:
    return res_user == os_user or os_user in aliases.get(res_user, ())


def _procs_desc(ps: list) -> str:
    items = ["PID %d '%s' (%.1fGiB)" % (p["pid"], p["comm"] or "?", p["mem"] / 1024.0) for p in ps[:3]]
    if len(ps) > 3:
        items.append("외 %d개" % (len(ps) - 3))
    return ", ".join(items)


def classify(info: dict, procs: list, now: datetime, cfg: dict,
             util: dict | None = None, alive: list | None = None) -> dict:
    """서버 예약 정보 + GPU 프로세스 → 판정 결과.

    '사용 중' 기준은 웹앱 서버 현황(cluster.py: 사용률 5%↑ 또는 메모리 500MB↑)과 같은 취지로,
    사용자별로 그 GPU 에 올린 메모리 합이 MIN_MEM_MIB 이상이거나 GPU 사용률이 BUSY_UTIL 이상일 때만 본다.
    alive 를 주면 거기 없는 GPU 는 응답 없음(고장)으로 표시하고 '빈 GPU' 추천에서 뺀다.

    반환 {"warnings": {user: [w...]}, "uids": {user: uid}, "free_now": [gpu...],
          "mine": {user: [reservation...]}, "status": [line...]}
    w = {"key", "kind", "gpu", "text"}  — key 는 터미널 재알림 판정용(같은 key 면 REPEAT 간격).
    """
    aliases = {k: set(v) for k, v in (info.get("aliases") or {}).items()}
    exempt = set(info.get("exempt_users") or [])
    lab_users = set(info.get("lab_proc_users") or [])
    min_uid = int(cfg.get("MIN_UID", 1000))
    ending = timedelta(minutes=int(cfg.get("ENDING_MINUTES", 10)))
    upcoming = timedelta(minutes=int(cfg.get("UPCOMING_MINUTES", 30)))
    ngpu = int(info.get("gpu_count") or 0)
    util = util or {}
    min_mem = int(cfg.get("MIN_MEM_MIB", 256))
    busy_util = int(cfg.get("BUSY_UTIL", 5))
    dead = set() if alive is None else {g for g in range(ngpu) if g not in set(alive)}

    res = []
    for r in info.get("reservations") or []:
        try:
            res.append(dict(r, s=parse_ts(r["start_at"]), e=parse_ts(r["end_at"])))
        except (KeyError, ValueError):
            continue
    blocks = []
    for b in info.get("blocks") or []:
        try:
            blocks.append(dict(b, s=parse_ts(b["start_at"]), e=parse_ts(b["end_at"])))
        except (KeyError, ValueError):
            continue

    def current(g):
        return [r for r in res if r["gpu_index"] == g and r["s"] <= now < r["e"]]

    def block_now(g):
        hit = [b for b in blocks if b["gpu_index"] == g and b["s"] <= now < b["e"]]
        return hit[0] if hit else None

    def own_end(g, user):
        """이 GPU 에서 user 예약이 끊김 없이 이어지는 마지막 종료 시각(연속 예약은 합친다)."""
        mine = sorted((r for r in res if r["gpu_index"] == g and _same_person(r["username"], user, aliases)),
                      key=lambda r: r["s"])
        end = None
        for r in mine:
            if r["s"] <= now < r["e"] and end is None:
                end = r["e"]
            elif end is not None and r["s"] <= end:
                end = max(end, r["e"])
        return end

    def next_conflict(g, user):
        """UPCOMING 안에 이 GPU 에서 시작할 남의 예약·관리자 차단 → (시작, 설명). 없으면 None."""
        cands = [(r["s"], "다른 사용자 예약") for r in res
                 if r["gpu_index"] == g and now < r["s"] <= now + upcoming
                 and not _same_person(r["username"], user, aliases)]
        cands += [(b["s"], "관리자 차단(%s)" % (b.get("reason") or "사용 금지")) for b in blocks
                  if b["gpu_index"] == g and now < b["s"] <= now + upcoming]
        return min(cands, key=lambda c: c[0]) if cands else None

    def soon_taken(g):
        return any(r["gpu_index"] == g and now < r["s"] <= now + upcoming for r in res) or \
            any(b["gpu_index"] == g and now < b["s"] <= now + upcoming for b in blocks)

    def active(ps, g):
        return sum(p["mem"] for p in ps) >= min_mem or util.get(g, 0) >= busy_util

    # (user, gpu) 별로 프로세스 묶기. 컨테이너·시스템 계정·실습 컨테이너 유저는 판정에서 제외(사용 중 표시엔 포함).
    by_gpu, pairs, uids = {}, {}, {}
    for p in procs:
        by_gpu.setdefault(p["gpu"], []).append(p)
        if p["container"] or p["uid"] < min_uid or p["user"] in lab_users:
            continue
        pairs.setdefault((p["user"], p["gpu"]), []).append(p)
    busy = {g for g, ps in by_gpu.items() if active(ps, g)}
    pairs = {k: ps for k, ps in pairs.items() if active(ps, k[1])}   # 빈 MPS 데몬·잔여 컨텍스트 제외
    for (user, _g), ps in pairs.items():
        uids[user] = ps[0]["uid"]

    warnings = {}
    for (user, g), ps in sorted(pairs.items()):
        cur = current(g)
        desc = _procs_desc(ps)
        if any(_same_person(r["username"], user, aliases) for r in cur):
            end = own_end(g, user)
            if end is not None and end - now <= ending:
                warnings.setdefault(user, []).append({
                    "key": "%s|%d|ending|%s" % (user, g, end.isoformat()), "kind": "ending", "gpu": g,
                    "text": "GPU %d: 내 예약이 %s에 끝납니다. 계속 쓰려면 연장하세요(포털 또는 labres MCP "
                            "extend_reservation). 사용 중: %s" % (g, kst(end, now), desc),
                })
            continue
        blk = block_now(g)
        key_extra = ""
        if blk:
            kind = "blocked"
            text = "GPU %d: 관리자가 막아 둔 시간입니다 — %s (~%s). 지금 바로 이 GPU 작업을 멈추세요. " \
                   "사용 중: %s" % (g, blk.get("reason") or "사용 금지", kst(blk["e"], now), desc)
        elif cur:
            kind = "mismatch"
            text = "GPU %d: 다른 사용자가 예약한 GPU입니다 (~%s). 지금 바로 이 GPU 작업을 멈추고 " \
                   "예약한 GPU로 옮기세요. 사용 중: %s" % (g, kst(max(r["e"] for r in cur), now), desc)
        else:
            nxt = next_conflict(g, user)
            if user in exempt and nxt is None:
                continue
            kind = "unreserved"
            text = "GPU %d: 예약 없이 사용 중입니다. 사용 중: %s." % (g, desc)
            if nxt is not None:
                text += " %s부터 %s이 있으니 그 전에 끝내세요." % (kst(nxt[0], now), nxt[1])
                key_extra = "|" + nxt[0].isoformat()   # 임박한 충돌이 새로 생기면 30분 재알림을 기다리지 않고 바로
        warnings.setdefault(user, []).append(
            {"key": "%s|%d|%s%s" % (user, g, kind, key_extra), "kind": kind, "gpu": g, "text": text})

    free_now = [g for g in range(ngpu)   # 곧 남의 예약·차단이 시작될 GPU 는 옮겨 가 봐야 또 경고라 뺀다
                if g not in busy and g not in dead and not current(g) and block_now(g) is None
                and not soon_taken(g)]

    # 사용자별 '이 노드의 내 예약'(지금 진행 중 + 12시간 안에 시작). 예약명 → 로컬 OS 계정으로 펼친다.
    mine = {}
    for r in sorted(res, key=lambda r: (r["s"], r["gpu_index"])):
        for name in {r["username"]} | aliases.get(r["username"], set()):
            mine.setdefault(name, []).append(r)

    status = []
    for g in range(ngpu):
        cur, blk = current(g), block_now(g)
        nxt = [r for r in res if r["gpu_index"] == g and r["s"] > now]
        if g in dead:
            line = "응답 없음(고장?) — 쓰지 마세요"
        elif blk:
            line = "차단: %s ~%s" % (blk.get("reason") or "사용 금지", kst(blk["e"], now))
        elif cur:
            line = "예약됨 ~%s" % kst(max(r["e"] for r in cur), now)
        else:
            line = "예약 없음" + (" · 다음 예약 %s" % kst(min(r["s"] for r in nxt), now) if nxt else "")
        if g in busy:
            line += " · 사용 중"
        status.append("  GPU%d  %s" % (g, line))
    return {"warnings": warnings, "uids": uids, "free_now": free_now, "mine": mine, "status": status}


def _mine_lines(rs: list, node: str, now: datetime) -> list:
    """사용자 본인 예약 요약 줄. 진행 중이면 CUDA_VISIBLE_DEVICES 힌트."""
    lines, cur_gpus = [], []
    for r in rs:
        g = r["gpu_index"]
        live = r["s"] <= now < r["e"]
        what = "CPU 전용" if g < 0 else "GPU %d" % g
        lines.append("  - %s · %s~%s%s" % (what, kst(r["s"], now), kst(r["e"], now), " (진행 중)" if live else ""))
        if live and g >= 0:
            cur_gpus.append(g)
    head = "내 예약(%s):" % node
    if cur_gpus:
        head += " 지금 쓸 수 있는 GPU → export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=%s" % \
            ",".join(map(str, sorted(set(cur_gpus))))
    return [head] + lines


def render_warn(user: str, ws: list, result: dict, node: str, portal: str, now: datetime) -> str:
    stamp = now.astimezone(KST).strftime("%m/%d %H:%M")
    out = ["[GPU 예약 경고] %s · %s KST" % (node, stamp)]
    for w in ws:
        out.append("⚠ [%s] %s" % (KIND_LABEL[w["kind"]], w["text"]))
    mine = [r for r in result["mine"].get(user, []) if r["s"] <= now < r["e"] and r["gpu_index"] >= 0]
    if mine:
        out.append("  내 예약 GPU: %s → export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=%s" % (
            ", ".join(str(g) for g in sorted({r["gpu_index"] for r in mine})),
            ",".join(str(g) for g in sorted({r["gpu_index"] for r in mine}))))
    if any(w["kind"] != "ending" for w in ws):
        free = result["free_now"]
        out.append("  지금 예약도 사용도 없는 GPU: %s" % (", ".join(map(str, free)) if free else "없음"))
        out.append("  해결: 예약(%s 또는 labres MCP reserve_gpu) · 예약한 GPU만 쓰기 · 작업 종료" % (portal or "포털"))
        out.append("  코딩 에이전트에게: 이 경고를 사용자에게 그대로 알리고, 해결 전에는 새 GPU 작업을 시작하지 "
                   "마세요. 실행 중인 작업을 임의로 종료하지 말고 사용자에게 먼저 물으세요.")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# 전달: 터미널 / 파일
# --------------------------------------------------------------------------- #
def user_ttys(uid: int) -> list:
    """uid 소유 터미널 장치(/dev/pts/N, 콘솔 /dev/ttyN). tmux·screen 창도 각자 pts 라 포함된다."""
    found = []
    for d, pat in (("/dev/pts", re.compile(r"^\d+$")), ("/dev", re.compile(r"^tty\d+$"))):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if not pat.match(n):
                continue
            path = os.path.join(d, n)
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISCHR(st.st_mode) and st.st_uid == uid:
                found.append(path)
    return sorted(found)


def write_tty(path: str, text: str, uid: int) -> bool:
    data = ("\r\n" + text.replace("\r\n", "\n").replace("\n", "\r\n")).encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        st = os.fstat(fd)   # 목록을 만든 뒤 그 pts 가 닫히고 남에게 다시 배정됐을 수 있다 → 연 fd 로 재확인
        if not stat.S_ISCHR(st.st_mode) or st.st_uid != uid:
            return False
        os.write(fd, data)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def tty_banner(text: str) -> str:
    bar = "━" * 64
    return "%s\n%s%s\n" % (bar, text, bar)


def write_file(path: str, text: str, uid: int | None = None, gid: int | None = None, mode: int = 0o600) -> None:
    """원자적 쓰기(+ uid 를 주면 그 사용자 소유로). 내용이 같아도 매번 다시 써서 mtime 을 '살아 있음' 신호로 쓴다."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    try:
        os.write(fd, text.encode("utf-8"))
        if uid is not None:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _local_user(name: str):
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    write_file(path, json.dumps(state, ensure_ascii=False, indent=1))


def run(cfg: dict, dry_run: bool = False) -> int:
    node = cfg["NODE_NAME"]
    if not cfg["AGENT_TOKEN"]:
        print("AGENT_TOKEN 미설정 — /etc/labres/agent.conf 확인", file=sys.stderr)
        return 2
    try:
        info = fetch_node_info(cfg)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print("예약 정보 조회 실패(경고 생략): %s" % e, file=sys.stderr)
        return 1
    # 엉뚱한 서비스(LABRES_URL 오설정·프록시)의 200 응답을 '예약 없음'으로 읽어 전원에게 경고하지 않게
    if not isinstance(info, dict) or str(info.get("node", "")).lower() != node.lower() \
            or not all(k in info for k in ("now", "gpu_count", "reservations", "blocks")):
        print("예약 서버 응답 형식이 맞지 않음(경고 생략) — LABRES_URL·NODE_NAME 확인", file=sys.stderr)
        return 1
    node = info["node"]   # 서버의 정식 이름(대소문자)
    snap = collect_gpu_procs()
    if snap is None:
        print("nvidia-smi 조회 실패(경고 생략)", file=sys.stderr)
        return 1
    procs = snap["procs"]
    now = parse_ts(info["now"]) if info.get("now") else datetime.now(timezone.utc)
    result = classify(info, procs, now, cfg, util=snap["util"], alive=snap["alive"])
    portal = info.get("portal_url") or cfg["LABRES_URL"]

    stamp = now.astimezone(KST).strftime("%m/%d %H:%M")
    status_txt = "[GPU 예약 현황] %s · %s KST (예약: %s)\n%s\n" % (node, stamp, portal, "\n".join(result["status"]))
    warn_txt = {u: render_warn(u, ws, result, node, portal, now) for u, ws in result["warnings"].items()}
    info_txt = {u: "\n".join(_mine_lines(rs, node, now)) + "\n" for u, rs in result["mine"].items()}

    if dry_run:
        print(status_txt)
        for u, t in sorted(warn_txt.items()):
            print("--- %s.warn (터미널 %d개)" % (u, len(user_ttys(result["uids"].get(u, -1)))))
            print(t)
        for u, t in sorted(info_txt.items()):
            if _local_user(u):
                print("--- %s.info\n%s" % (u, t))
        print("GPU 프로세스 %d개 · 경고 대상 %d명" % (len(procs), len(warn_txt)))
        return 0

    run_dir, user_dir = cfg["RUN_DIR"], os.path.join(cfg["RUN_DIR"], "user")
    os.makedirs(user_dir, exist_ok=True)
    os.chmod(run_dir, 0o755)
    os.chmod(user_dir, 0o711)   # 남의 파일 이름(=누가 경고 중인지) 목록은 못 보게, 자기 파일은 이름으로 열 수 있게
    write_file(os.path.join(run_dir, "status.txt"), status_txt, mode=0o644)

    keep = set()
    for kind_ext, texts in ((".warn", warn_txt), (".info", info_txt)):
        for u, t in texts.items():
            pw = _local_user(u)
            if pw is None:
                continue
            write_file(os.path.join(user_dir, u + kind_ext), t, pw.pw_uid, pw.pw_gid)
            keep.add(u + kind_ext)
    for n in os.listdir(user_dir):
        if n not in keep:
            try:
                os.unlink(os.path.join(user_dir, n))
            except OSError:
                pass

    # 터미널: 새 경고·바뀐 경고는 즉시, 같은 경고는 REPEAT 간격마다. ending 은 예약(종료시각)당 1회.
    state_path = os.path.join(cfg["STATE_DIR"], "state.json")
    state = load_state(state_path)
    repeat = int(cfg["REPEAT_MINUTES"]) * 60
    now_ts = now.timestamp()
    new_state = {}
    for u, ws in result["warnings"].items():
        due = False
        for w in ws:
            last = state.get(w["key"])
            if last is None or (w["kind"] != "ending" and now_ts - last >= repeat):
                due = True
                new_state[w["key"]] = now_ts
            else:
                new_state[w["key"]] = last
        if not due:
            continue
        sent = 0
        if cfg["TTY_WARN"] != "0":
            banner = tty_banner(warn_txt[u])
            uid = result["uids"][u]
            sent = sum(write_tty(t, banner, uid) for t in user_ttys(uid))
        kinds = ",".join("%s:GPU%d" % (w["kind"], w["gpu"]) for w in ws)
        print("warn user=%s %s ttys=%d" % (u, kinds, sent), flush=True)
    save_state(state_path, new_state)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="GPU 예약 경고 에이전트")
    ap.add_argument("--config", default="/etc/labres/agent.conf")
    ap.add_argument("--dry-run", action="store_true", help="판정만 출력, 터미널·파일 쓰기 없음")
    a = ap.parse_args(argv)
    return run(load_config(a.config), dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
