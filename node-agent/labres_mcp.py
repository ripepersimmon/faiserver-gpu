#!/usr/bin/env python3
"""labres-mcp — GPU 예약 시스템 MCP 서버 (stdio). Claude Code·Codex 에서 예약 조회·생성·연장·반납.

사용자 본인 권한으로 동작한다: 포털(https://faiserver.hufs.ac.kr/tokens)에서 발급한 개인 API 토큰을
~/.config/labres/token 에 저장해 두면, 그 토큰으로 예약 웹앱 REST API 를 부른다. 토큰으로는 예약 관련
API 만 쓸 수 있다(웹앱 TOKEN_ROUTES). GPU 노드에서 돌면 그 노드를 기본 서버로 삼고, 이 사용자의
실제 GPU 프로세스(nvidia-smi)를 예약과 대조하는 점검(check_my_gpu_usage)도 한다.

등록:
  Claude Code:  플러그인(권장)  /plugin marketplace add ripepersimmon/faiserver-gpu → /plugin install faiserver-gpu@faiserver
                또는 직접      claude mcp add labres --scope user -- labres-mcp
  Codex:        ~/.codex/config.toml →  [mcp_servers.labres]  command = "labres-mcp"
명령행:
  labres-mcp                MCP 서버 실행(stdio, 에이전트가 띄운다)
  labres-mcp --save-token   토큰을 입력받아 ~/.config/labres/token 에 저장(600)
  labres-mcp --check        토큰·연결 점검 + 내 예약·GPU 사용 점검 출력
환경변수(선택): LABRES_URL(기본 https://faiserver.hufs.ac.kr), LABRES_TOKEN(파일 대신), LABRES_NODE(서버 이름)

MCP 프로토콜(JSON-RPC 2.0, 줄 단위)을 표준 라이브러리만으로 구현한다 — 노드에 pip 설치 없이 동작.
"""

from __future__ import annotations

import getpass
import json
import os
import pwd
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

VERSION = "1.0.2"
KST = timezone(timedelta(hours=9))
DEFAULT_URL = "https://faiserver.hufs.ac.kr"
TOKEN_FILE = os.path.expanduser("~/.config/labres/token")
NODE_FILE = "/etc/labres/node"           # install.sh 가 노드 이름을 적어 둔다(644)
WARN_DIR = "/run/labres-gpu-warn/user"   # labres-gpu-warn 이 쓰는 사용자별 경고(<user>/warn)
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "HUFS Finance & AI GPU 서버 예약 시스템. 이 서버의 GPU 는 예약제다.\n"
    "- GPU 를 쓰는 작업(학습·추론·CUDA 코드 실행)을 시작하기 전에 check_my_gpu_usage 로 지금 예약이 있는지 확인하고, "
    "예약한 GPU 번호로 CUDA_VISIBLE_DEVICES 를 맞춰 실행한다.\n"
    "- 예약이 없으면 사용자에게 알리고, 사용자가 원할 때만 find_available_gpus → reserve_gpu 로 예약한다. "
    "예약·연장·반납은 사용자에게 시간·GPU 를 확인받은 뒤 실행한다.\n"
    "- '[GPU 예약 경고]' 가 보이면 사용자에게 그대로 전달하고, 해결 전에는 새 GPU 작업을 시작하지 않는다.\n"
    "- 시각은 한국 시간(KST) 기준이다."
)


class ToolError(Exception):
    """사용자에게 그대로 보여줄 도구 오류."""


# --------------------------------------------------------------------------- #
# 시각
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_api(dt: datetime) -> str:
    """웹앱 저장 포맷 'YYYY-MM-DDTHH:MM:SS.000Z' (DB 의 문자열 비교와 맞춘다)."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d(?::\d\d)?)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$")


def parse_time(value: str | None, now: datetime | None = None) -> datetime:
    """'now' · ISO8601(오프셋 없으면 KST) · 'YYYY-MM-DD HH:MM' · 'HH:MM'(오늘 KST) → aware UTC."""
    now = now or now_utc()
    v = (value or "now").strip()
    if v.lower() in ("now", "지금"):
        return now.replace(second=0, microsecond=0)
    m = re.match(r"^(\d{1,2}):(\d\d)$", v)
    if m:
        d = now.astimezone(KST).date()
        return datetime(d.year, d.month, d.day, int(m.group(1)), int(m.group(2)), tzinfo=KST).astimezone(timezone.utc)
    m = _TS_RE.match(v)
    if not m:
        raise ToolError("시각 형식을 알 수 없습니다: %r (예: now, 14:30, 2026-10-11 09:00, ISO8601)" % v)
    hms = m.group(2) if m.group(2).count(":") == 2 else m.group(2) + ":00"
    dt = datetime.strptime("%s %s" % (m.group(1), hms), "%Y-%m-%d %H:%M:%S")
    if m.group(3):
        dt = dt.replace(microsecond=int((m.group(3)[1:] + "000000")[:6]))
    tz = m.group(4)
    if not tz:
        return dt.replace(tzinfo=KST).astimezone(timezone.utc)
    if tz == "Z":
        return dt.replace(tzinfo=timezone.utc)
    sign = 1 if tz[0] == "+" else -1
    digits = tz[1:].replace(":", "")
    return dt.replace(tzinfo=timezone(sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))).astimezone(timezone.utc)


def parse_db(value: str) -> datetime:
    return parse_time(value if re.search(r"(Z|[+-]\d\d:?\d\d)$", value) else value + "Z")


def kst(value) -> str:
    """'MM/DD(요일) HH:MM' KST. 요일은 로캘과 무관하게 한글."""
    dt = (parse_db(value) if isinstance(value, str) else value).astimezone(KST)
    return "%s(%s) %s" % (dt.strftime("%m/%d"), "월화수목금토일"[dt.weekday()], dt.strftime("%H:%M"))


def window(start: str | None, end: str | None, hours) -> tuple:
    s = parse_time(start)
    if end:
        e = parse_time(end)
        if e <= s and re.match(r"^\d{1,2}:\d\d$", end.strip()):
            e += timedelta(days=1)   # 23:30 에 end="01:00" = 다음 날 새벽
    elif hours is not None:
        h = float(hours)
        if not 0 < h <= 24 * 31:
            raise ToolError("hours 는 0 보다 크고 744(31일) 이하여야 합니다.")
        e = s + timedelta(hours=h)
    else:
        raise ToolError("끝 시각(end) 또는 사용 시간(hours)을 지정하세요.")
    if e <= s:
        raise ToolError("끝 시각이 시작 시각보다 뒤여야 합니다.")
    return s, e


# --------------------------------------------------------------------------- #
# 예약 웹앱 API
# --------------------------------------------------------------------------- #
def read_token() -> str:
    """토큰: LABRES_TOKEN 환경변수 > ~/.config/labres/token. 파일이 본인 소유·0600 이 아니면 쓰지 않는다
    (손으로 저장하다 umask 를 빠뜨려 남이 읽을 수 있는 파일이 되는 사고 방지)."""
    tok = os.environ.get("LABRES_TOKEN", "").strip()
    if tok:
        return tok
    try:
        st = os.stat(TOKEN_FILE)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise ToolError("토큰 파일 %s 의 권한이 안전하지 않습니다(본인 소유·600 이어야 함): "
                            "chmod 600 %s" % (TOKEN_FILE, TOKEN_FILE))
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """리다이렉트를 따라가면 urllib 이 Authorization 을 새 호스트(http 포함)에도 보낸다 → 오류로 취급."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Api:
    def __init__(self, base: str | None = None, token: str | None = None):
        self.base = (base or os.environ.get("LABRES_URL") or DEFAULT_URL).rstrip("/")
        self._token = token   # None = 매 호출마다 env/파일에서 읽는다

    @property
    def token(self) -> str:
        # 서버(MCP)가 떠 있는 동안 저장·교체한 토큰도 재시작 없이 바로 쓰도록 매번 읽는다(파일 하나라 싸다).
        return self._token if self._token is not None else read_token()

    def call(self, method: str, path: str, body=None, query: dict | None = None):
        if not self.base.startswith("https://") and os.environ.get("LABRES_ALLOW_HTTP") != "1":
            raise ToolError("LABRES_URL 은 https:// 여야 합니다(토큰이 평문으로 나갑니다). 시험용이면 LABRES_ALLOW_HTTP=1")
        token = self.token
        if not token:
            raise ToolError(
                "API 토큰이 없습니다. %s/tokens 에서 발급한 뒤 터미널에서 저장하세요 — GPU 서버: "
                "`labres-mcp --save-token`, 그 밖의 컴퓨터: `mkdir -p ~/.config/labres && "
                "(umask 077; cat > ~/.config/labres/token)` 후 붙여넣고 Ctrl-D. (토큰을 대화창에 붙여넣지 마세요)"
                % self.base)
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "labres-mcp/" + VERSION,
        })
        try:
            with _OPENER.open(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8")).get("detail")
            except (ValueError, AttributeError):
                detail = None
            if e.code == 401:
                raise ToolError("토큰이 유효하지 않거나 만료됐습니다. %s/tokens 에서 새로 발급해 "
                                "~/.config/labres/token 에 다시 저장하세요(GPU 서버: `labres-mcp --save-token`)."
                                % self.base)
            raise ToolError("예약 서버 오류 (%d): %s" % (e.code, detail or e.reason))
        except (urllib.error.URLError, OSError) as e:
            raise ToolError("예약 서버(%s)에 연결하지 못했습니다: %s" % (self.base, getattr(e, "reason", e)))


# --------------------------------------------------------------------------- #
# 이 머신(GPU 노드) 정보
# --------------------------------------------------------------------------- #
def local_node(known: list | None = None) -> str | None:
    """이 머신이 예약 시스템의 어느 서버인지. LABRES_NODE > /etc/labres/node > 호스트명(대소문자 무시)."""
    name = os.environ.get("LABRES_NODE", "").strip()
    if not name:
        try:
            with open(NODE_FILE, encoding="utf-8") as f:
                name = f.read().strip()
        except OSError:
            name = socket.gethostname().split(".")[0]
    if known:
        for k in known:
            if k.lower() == name.lower():
                return k
        return None
    return name or None


def my_gpu_procs() -> list | None:
    """이 머신에서 '내' uid 의 GPU 프로세스 [{gpu, pid, mem, comm}]. nvidia-smi 가 없거나 실패하면 None."""
    def run(args):
        try:
            r = subprocess.run(["nvidia-smi"] + args + ["--format=csv,noheader,nounits"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None

    def smi(args):
        """카드 1장이 고장 나면 일괄 조회가 통째로 실패한다 → 인덱스별로 다시 물어 살아 있는 카드만 읽는다."""
        out = run(args)
        if out is not None:
            return out
        try:
            n = len(os.listdir("/proc/driver/nvidia/gpus"))
        except OSError:
            return None
        parts = [o for o in (run(["-i", str(i)] + args) for i in range(n)) if o is not None]
        return "".join(parts) if parts else None

    gq = smi(["--query-gpu=index,uuid"])
    aq = smi(["--query-compute-apps=gpu_uuid,pid,used_memory"])
    if gq is None or aq is None:
        return None
    uuid2idx = {}
    for line in gq.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 2 and p[0].isdigit():
            uuid2idx[p[1]] = int(p[0])
    me, out = os.getuid(), []
    for line in aq.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 3 or p[0] not in uuid2idx or not p[1].isdigit():
            continue
        pid = int(p[1])
        try:
            if os.stat("/proc/%d" % pid).st_uid != me:
                continue
            with open("/proc/%d/comm" % pid, encoding="utf-8", errors="replace") as f:
                comm = f.read().strip()
        except OSError:
            continue
        out.append({"gpu": uuid2idx[p[0]], "pid": pid, "mem": int(p[2]) if p[2].isdigit() else 0, "comm": comm})
    return out


def node_warning() -> str | None:
    """labres-gpu-warn 이 이 사용자에게 남긴 경고(3분 이내 것만)."""
    path = os.path.join(WARN_DIR, pwd.getpwuid(os.getuid()).pw_name, "warn")
    try:
        if now_utc().timestamp() - os.stat(path).st_mtime > 180:
            return None
        with open(path, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# 도구 구현
# --------------------------------------------------------------------------- #
def _group(rows: list) -> list:
    """GPU 당 1행인 예약을 (서버, 시작, 끝) 세션으로 묶는다. 연장·반납은 세션 단위로 적용된다."""
    groups = {}
    for r in rows:
        k = (r["node"], r["start_at"], r["end_at"])
        g = groups.setdefault(k, dict(r, gpus=[], ids=[]))
        g["gpus"].append(r["gpu_index"])
        g["ids"].append(r["id"])
    return sorted(groups.values(), key=lambda g: g["start_at"])


def _fmt_session(g: dict, now: datetime) -> str:
    gpus = "CPU 전용" if any(x < 0 for x in g["gpus"]) else "GPU " + ",".join(map(str, sorted(g["gpus"])))
    live = parse_db(g["start_at"]) <= now < parse_db(g["end_at"])
    tags = ["진행 중" if live else "예정"]
    if g.get("session_id") is not None:
        tags.append("수업 배정 — 연장·반납 불가")
    if g.get("purpose"):
        tags.append(g["purpose"])
    return "- [id %s] %s %s · %s ~ %s (%s)" % (
        g["ids"][0], g["node"], gpus, kst(g["start_at"]), kst(g["end_at"]), " · ".join(tags))


def tool_get_my_reservations(api: Api, args: dict) -> str:
    me = api.call("GET", "/me")
    rows = api.call("GET", "/my/reservations").get("reservations") or []
    now = now_utc()
    head = "사용자: %s (%s) · 현재 %s KST" % (me.get("name"), me.get("role"), kst(now))
    if not rows:
        return head + "\n살아 있는 예약이 없습니다."
    return head + "\n" + "\n".join(_fmt_session(g, now) for g in _group(rows)) + \
        "\n(연장·반납은 id 로 — 멀티 GPU 예약은 세션 전체에 적용)"


def _free_gpus(nodes: list, avail: dict, s: datetime, e: datetime, node: str | None) -> tuple:
    """[s,e) 에 예약·차단이 겹치지 않는 GPU {node: [idx...]} + 지금 실측 사용 중 GPU {node: set}."""
    enforced = avail.get("blocks_enforced", True)
    taken = {}
    for r in avail.get("reservations") or []:
        if r.get("status") in ("booked", "active") and r["gpu_index"] >= 0 \
                and parse_db(r["start_at"]) < e and s < parse_db(r["end_at"]):
            taken.setdefault(r["node"], set()).add(r["gpu_index"])
    if enforced:
        for b in avail.get("blocks") or []:
            if parse_db(b["start_at"]) < e and s < parse_db(b["end_at"]):
                taken.setdefault(b["node"], set()).add(b["gpu_index"])
    free, live = {}, {}
    for n in nodes:
        if node and n["node"] != node:
            continue
        gpus = n.get("gpus") or []
        live[n["node"]] = {g["index"] for g in gpus if g.get("in_use")}
        dead = {g["index"] for g in gpus if g.get("error")}   # 응답 없는(고장) 카드는 후보에서 뺀다
        free[n["node"]] = [i for i in range(n["gpu_total"])
                           if i not in taken.get(n["node"], set()) and i not in dead]
    return free, live


def tool_find_available_gpus(api: Api, args: dict) -> str:
    s, e = window(args.get("start"), args.get("end"), args.get("hours", 1))
    nodes = api.call("GET", "/nodes").get("nodes") or []
    node = args.get("node")
    if node and node not in [n["node"] for n in nodes]:
        raise ToolError("알 수 없는 서버: %s (가능: %s)" % (node, ", ".join(n["node"] for n in nodes)))
    avail = api.call("GET", "/availability", query={"from": to_api(s), "to": to_api(e)})
    free, live = _free_gpus(nodes, avail, s, e, node)
    starts_now = s <= now_utc() + timedelta(minutes=10)
    out = ["%s ~ %s KST 에 예약 가능한 GPU:" % (kst(s), kst(e))]
    for n, gpus in free.items():
        if not gpus:
            out.append("- %s: 없음" % n)
            continue
        marks = ["%d%s" % (g, "(지금 누군가 예약 없이 사용 중)" if starts_now and g in live.get(n, ()) else "")
                 for g in gpus]
        out.append("- %s: GPU %s" % (n, ", ".join(marks)))
    here = local_node([n["node"] for n in nodes])
    if here:
        out.append("(이 머신은 %s 입니다)" % here)
    return "\n".join(out)


def tool_check_my_gpu_usage(api: Api, args: dict) -> str:
    nodes = api.call("GET", "/nodes").get("nodes") or []
    here = local_node([n["node"] for n in nodes])
    now = now_utc()
    rows = api.call("GET", "/my/reservations").get("reservations") or []
    out = []
    if here is None:
        out.append("이 머신은 예약 시스템의 GPU 서버가 아닙니다(%s). 예약 현황만 보여 줍니다." % socket.gethostname())
    live_here = [r for r in rows if r["node"] == here and parse_db(r["start_at"]) <= now < parse_db(r["end_at"])]
    my_gpus = sorted({r["gpu_index"] for r in live_here if r["gpu_index"] >= 0})
    if here:
        out.append("서버 %s · 현재 %s KST" % (here, kst(now)))
        if my_gpus:
            out.append("✓ 지금 내 예약 GPU: %s → GPU 작업은 export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=%s "
                       "로 실행하세요. (종료 %s)" % (
                ", ".join(map(str, my_gpus)), ",".join(map(str, my_gpus)),
                kst(min(r["end_at"] for r in live_here if r["gpu_index"] >= 0))))
        else:
            out.append("✗ 지금 이 서버에 내 GPU 예약이 없습니다. GPU 를 쓰려면 먼저 예약하세요(find_available_gpus → reserve_gpu).")
        procs = my_gpu_procs()
        if procs is None:
            out.append("(nvidia-smi 로 내 GPU 프로세스를 확인하지 못했습니다)")
        elif not procs:
            out.append("내 GPU 프로세스: 없음")
        else:
            for p in procs:
                ok = p["gpu"] in my_gpus
                out.append("%s GPU %d · PID %d %s (%.1fGiB)%s" % (
                    "✓" if ok else "⚠", p["gpu"], p["pid"], p["comm"], p["mem"] / 1024.0,
                    "" if ok else " — 예약하지 않은 GPU 입니다"))
        warn = node_warning()
        if warn:
            out.append("\n" + warn)
    others = [g for g in _group(rows) if not (g["node"] == here and parse_db(g["start_at"]) <= now < parse_db(g["end_at"]))]
    if others:
        out.append("다른 예약:\n" + "\n".join(_fmt_session(g, now) for g in others))
    return "\n".join(out)


def tool_reserve_gpu(api: Api, args: dict) -> str:
    s, e = window(args.get("start"), args.get("end"), args.get("hours"))
    nodes = api.call("GET", "/nodes").get("nodes") or []
    names = [n["node"] for n in nodes]
    node = args.get("node") or local_node(names)
    if not node:
        raise ToolError("서버(node)를 지정하세요. 가능: %s" % ", ".join(names))
    if node not in names:
        raise ToolError("예약할 수 없는 서버: %s (가능: %s)" % (node, ", ".join(names)))

    # 같은 시간대에 내 예약이 이미 있으면 새로 만들지 않는다(중복 예약 방지 — 명시적으로 원할 때만 추가).
    mine = api.call("GET", "/my/reservations").get("reservations") or []
    overlap = [r for r in mine if parse_db(r["start_at"]) < e and s < parse_db(r["end_at"])]
    if overlap and not args.get("allow_additional"):
        return "이미 이 시간대에 내 예약이 있어 새로 만들지 않았습니다:\n" + \
            "\n".join(_fmt_session(g, now_utc()) for g in _group(overlap)) + \
            "\n(정말 추가로 예약하려면 allow_additional=true)"

    gpus = [int(g) for g in args.get("gpu_indices") or []]
    if not gpus:
        count = int(args.get("count") or 1)
        avail = api.call("GET", "/availability", query={"from": to_api(s), "to": to_api(e)})
        free, live = _free_gpus(nodes, avail, s, e, node)
        cand = free.get(node, [])
        # 지금 시작이면 실측상 비어 있는 GPU 를 먼저 고른다(예약은 없지만 누가 쓰고 있는 GPU 는 뒤로).
        cand.sort(key=lambda g: g in live.get(node, set()))
        if len(cand) < count:
            raise ToolError("%s 에 %s~%s 동안 비어 있는 GPU 가 %d개뿐입니다(요청 %d개). find_available_gpus 로 다른 "
                            "서버·시간을 찾아보세요." % (node, kst(s), kst(e), len(cand), count))
        gpus = sorted(cand[:count])
    body = {"node": node, "gpu_indices": gpus, "start_at": to_api(s), "end_at": to_api(e)}
    if args.get("purpose"):
        body["purpose"] = str(args["purpose"])[:200]
    res = api.call("POST", "/reserve", body=body)
    return "예약했습니다: %s GPU %s · %s ~ %s KST (id %s)\n→ GPU 작업은 export CUDA_DEVICE_ORDER=PCI_BUS_ID " \
           "CUDA_VISIBLE_DEVICES=%s 로 실행하세요." % (
        node, ",".join(map(str, gpus)), kst(s), kst(e), ", ".join(map(str, res.get("ids") or [])),
        ",".join(map(str, gpus)))


def tool_extend_reservation(api: Api, args: dict) -> str:
    rid, minutes = int(args["reservation_id"]), int(args["minutes"])
    res = api.call("POST", "/reservations/%d/extend" % rid, body={"minutes": minutes})
    return "연장했습니다: 예약 %d 새 종료 %s KST (같은 세션의 GPU 전체에 적용)" % (rid, kst(res["end_at"]))


def tool_release_reservation(api: Api, args: dict) -> str:
    rid = int(args["reservation_id"])
    api.call("POST", "/reservations/%d/stop" % rid)
    return "반납했습니다: 예약 %d (같은 세션의 GPU 전체). 실행 중인 JupyterLab 세션은 1분 안에 정리됩니다." % rid


_TIME_DESC = "now, HH:MM(오늘 KST), 'YYYY-MM-DD HH:MM'(KST) 또는 ISO8601"
TOOLS = [
    {
        "name": "get_my_reservations",
        "description": "내 GPU 예약(진행 중·예정) 목록. 각 예약의 id·서버·GPU·시간(KST)·수업 배정 여부.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "내 예약 조회", "readOnlyHint": True},
        "fn": tool_get_my_reservations,
    },
    {
        "name": "check_my_gpu_usage",
        "description": "GPU 작업을 시작하기 전에 호출. 이 서버에서 지금 내 예약 GPU(→ CUDA_VISIBLE_DEVICES), 내가 실제로 "
                       "쓰고 있는 GPU 프로세스가 예약과 맞는지, 서버가 띄운 예약 경고를 알려 준다.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "내 GPU 사용 점검", "readOnlyHint": True},
        "fn": tool_check_my_gpu_usage,
    },
    {
        "name": "find_available_gpus",
        "description": "지정한 시간대에 예약·차단이 없어 예약 가능한 GPU 를 서버별로 찾는다. 기본: 지금부터 1시간.",
        "inputSchema": {"type": "object", "properties": {
            "start": {"type": "string", "description": "시작. " + _TIME_DESC + ". 기본 now"},
            "end": {"type": "string", "description": "끝. " + _TIME_DESC},
            "hours": {"type": "number", "description": "end 대신 사용 시간(시간 단위)"},
            "node": {"type": "string", "description": "서버 이름(예: fai001). 생략하면 전체"},
        }},
        "annotations": {"title": "빈 GPU 찾기", "readOnlyHint": True},
        "fn": tool_find_available_gpus,
    },
    {
        "name": "reserve_gpu",
        "description": "GPU 예약을 만든다. 같은 시간대에 내 예약이 이미 있으면 만들지 않고 기존 예약을 알려 준다. "
                       "gpu_indices 를 생략하면 빈 GPU 를 count 개 자동으로 고른다. 사용자에게 시간·GPU 를 확인받은 뒤 호출할 것.",
        "inputSchema": {"type": "object", "properties": {
            "node": {"type": "string", "description": "서버 이름. 생략하면 지금 이 GPU 서버"},
            "start": {"type": "string", "description": "시작. " + _TIME_DESC + ". 기본 now"},
            "end": {"type": "string", "description": "끝. " + _TIME_DESC},
            "hours": {"type": "number", "description": "end 대신 사용 시간(시간 단위)"},
            "gpu_indices": {"type": "array", "items": {"type": "integer", "minimum": 0},
                            "description": "예약할 GPU 번호들. 생략하면 자동 선택"},
            "count": {"type": "integer", "minimum": 1, "description": "자동 선택 시 GPU 개수(기본 1)"},
            "purpose": {"type": "string", "description": "사용 목적(선택)"},
            "allow_additional": {"type": "boolean", "description": "같은 시간대 내 예약이 있어도 추가로 만들기(기본 false)"},
        }},
        "annotations": {"title": "GPU 예약", "readOnlyHint": False, "destructiveHint": False},
        "fn": tool_reserve_gpu,
    },
    {
        "name": "extend_reservation",
        "description": "내 예약의 종료 시각을 늘린다(실행 중인 작업은 그대로). 다음 예약과 겹치면 거부된다.",
        "inputSchema": {"type": "object", "required": ["reservation_id", "minutes"], "properties": {
            "reservation_id": {"type": "integer", "description": "get_my_reservations 의 id"},
            "minutes": {"type": "integer", "minimum": 1, "maximum": 1440, "description": "늘릴 분"},
        }},
        "annotations": {"title": "예약 연장", "readOnlyHint": False, "destructiveHint": False},
        "fn": tool_extend_reservation,
    },
    {
        "name": "release_reservation",
        "description": "내 예약을 반납(종료·취소)한다. 같은 세션의 GPU 전체가 반납되고 되돌릴 수 없다. 사용자 확인 후 호출할 것.",
        "inputSchema": {"type": "object", "required": ["reservation_id"], "properties": {
            "reservation_id": {"type": "integer", "description": "get_my_reservations 의 id"},
        }},
        "annotations": {"title": "예약 반납", "readOnlyHint": False, "destructiveHint": True},
        "fn": tool_release_reservation,
    },
]
TOOL_BY_NAME = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------- #
# MCP (JSON-RPC 2.0 over stdio, 줄 단위)
# --------------------------------------------------------------------------- #
def handle(msg: dict, api: Api) -> dict | None:
    """요청 1개 처리 → 응답 dict. 알림(id 없음)이면 None."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None   # notifications/initialized 등
    params = msg.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({
            "protocolVersion": want if want in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "labres", "title": "GPU 예약", "version": VERSION},
            "instructions": INSTRUCTIONS,
        })
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{k: v for k, v in t.items() if k != "fn"} for t in TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        tool = TOOL_BY_NAME.get(name) if isinstance(name, str) else None
        if tool is None:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool: %s" % name}}
        args = params.get("arguments") or {}
        try:
            if not isinstance(args, dict):
                raise ToolError("arguments 는 객체여야 합니다.")
            text, is_err = tool["fn"](api, args), False
        except ToolError as e:
            text, is_err = str(e), True
        except (KeyError, TypeError, ValueError, OverflowError) as e:
            text, is_err = "잘못된 인자: %s" % e, True
        except Exception as e:  # noqa: BLE001 — 도구 하나의 예기치 못한 오류로 MCP 서버가 죽지 않게
            text, is_err = "도구 실행 오류(%s): %s" % (type(e).__name__, e), True
        return ok({"content": [{"type": "text", "text": text}], "isError": is_err})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}


def serve() -> int:
    api = Api()
    for raw in sys.stdin.buffer:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                if isinstance(msg, list):   # 구버전 배치
                    out = [r for r in (handle(m, api) for m in msg if isinstance(m, dict)) if r is not None] or None
                elif isinstance(msg, dict):
                    out = handle(msg, api)
                else:
                    out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
            except Exception as e:  # noqa: BLE001 — 메시지 하나 때문에 세션 전체가 끊기지 않게
                mid = msg.get("id") if isinstance(msg, dict) else None
                out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": "internal error: %s" % e}}
        if out is not None:
            # 로캘(EUC-KR 등)과 무관하게 UTF-8 로 — JSON-RPC 스트림은 UTF-8 이어야 한다
            sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8") + b"\n")
            sys.stdout.buffer.flush()
    return 0


# --------------------------------------------------------------------------- #
# 명령행 도우미
# --------------------------------------------------------------------------- #
def save_token() -> int:
    tok = (getpass.getpass("API 토큰 붙여넣기: ") if sys.stdin.isatty() else sys.stdin.readline()).strip()
    if not tok.startswith("lrt_"):
        print("토큰 형식이 아닙니다(lrt_ 로 시작). 포털 /tokens 에서 발급한 값을 붙여넣으세요.", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
    try:
        os.unlink(TOKEN_FILE)   # 기존 파일(심볼릭 링크일 수도)은 지우고 새로 만든다
    except OSError:
        pass
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    print("저장했습니다: %s" % TOKEN_FILE)
    return check()


def check() -> int:
    api = Api()
    try:
        me = api.call("GET", "/me")
        if not me.get("authenticated") and me.get("auth_enabled"):
            raise ToolError("토큰으로 로그인되지 않았습니다.")
        print("로그인: %s (%s) · 서버 %s" % (me.get("name"), me.get("role"), api.base))
        print(tool_check_my_gpu_usage(api, {}))
    except ToolError as e:
        print(str(e), file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--save-token"]:
        return save_token()
    if argv[:1] == ["--check"]:
        return check()
    if argv[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 0
    return serve()


if __name__ == "__main__":
    sys.exit(main())
