"""tests/test_node_agent.py

GPU 노드 번들(scripts/node-agent) — 실제 GPU·root·서버 없이 검증.

  - labres_gpu_warn.classify: 남의 예약(mismatch)·관리자 차단(blocked)·미예약(unreserved)·종료 임박(ending),
    면제 사용자, 곧 시작할 남의 예약, 컨테이너·시스템 계정 제외, 연속 예약 합치기, 별칭(예약명↔OS 계정)
  - labres_gpu_warn.run: 파일(.warn/.info/status.txt) + 실제 pty 로 터미널 배너 전달, 재알림 억제
  - labres_mcp: MCP JSON-RPC(initialize·tools/list·tools/call·알림), 시각 파싱, reserve_gpu 의
    '내 예약이 이미 있으면 만들지 않음' + 빈 GPU 자동 선택
  - labres_agent_hook: SessionStart/UserPromptSubmit/PostToolUse 출력, PostToolUse 중복 억제
"""

import importlib.util
import io
import json
import os
import pwd
import sys
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.join(os.path.dirname(__file__), "..", "node-agent")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


warn = _load("labres_gpu_warn")
mcp = _load("labres_mcp")
hook = _load("labres_agent_hook")

NOW = datetime(2030, 3, 2, 5, 0, tzinfo=timezone.utc)   # 14:00 KST
CFG = {"MIN_UID": "1000", "ENDING_MINUTES": "10", "UPCOMING_MINUTES": "30"}


def _z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _res(user, gpu, start_min, end_min):
    return {"username": user, "gpu_index": gpu, "status": "booked", "session_id": None,
            "start_at": _z(NOW + timedelta(minutes=start_min)), "end_at": _z(NOW + timedelta(minutes=end_min))}


def _proc(user, gpu, pid=100, uid=2000, container=False, mem=4096):
    return {"gpu": gpu, "pid": pid, "mem": mem, "uid": uid, "user": user, "comm": "python", "container": container}


def _info(reservations=(), blocks=(), exempt=(), aliases=None):
    return {"node": "fai001", "now": _z(NOW), "gpu_count": 4, "reservations": list(reservations),
            "blocks": list(blocks), "aliases": aliases or {}, "exempt_users": list(exempt),
            "lab_proc_users": ["user", "jovyan"], "portal_url": "https://portal.example"}


def _kinds(result):
    return {u: sorted((w["gpu"], w["kind"]) for w in ws) for u, ws in result["warnings"].items()}


# --------------------------------------------------------------------------- #
# 판정
# --------------------------------------------------------------------------- #
def test_classify_core_kinds():
    info = _info(
        reservations=[_res("alice", 1, -60, 120)],
        blocks=[{"gpu_index": 3, "reason": "딥러닝 수업 중", "session_id": 9,
                 "start_at": _z(NOW - timedelta(minutes=5)), "end_at": _z(NOW + timedelta(hours=2))}],
    )
    procs = [
        _proc("bob", 1),                        # alice 예약 GPU → mismatch
        _proc("carol", 2, pid=200),             # 예약 없음 → unreserved
        _proc("dave", 3, pid=300),              # 차단 GPU → blocked
        _proc("alice", 1, pid=400),             # 본인 예약 → 정상
        _proc("user", 0, pid=500, uid=1000),    # 실습 컨테이너 유저 → 제외
        _proc("root", 0, pid=600, uid=0),       # 시스템 계정 → 제외
        _proc("eve", 0, pid=700, container=True),  # 컨테이너 → 제외
    ]
    r = warn.classify(info, procs, NOW, CFG)
    assert _kinds(r) == {"bob": [(1, "mismatch")], "carol": [(2, "unreserved")], "dave": [(3, "blocked")]}
    assert "딥러닝 수업 중" in r["warnings"]["dave"][0]["text"]
    assert "PID 100 'python' (4.0GiB)" in r["warnings"]["bob"][0]["text"]
    assert r["free_now"] == []          # 0: 프로세스 있음, 1: 예약, 2: 사용 중, 3: 차단
    assert r["status"][3].startswith('  GPU3  차단: "딥러닝 수업 중"')
    assert "예약됨" in r["status"][1] and "사용 중" in r["status"][1]
    assert all("alice" not in line and "bob" not in line for line in r["status"])   # 현황엔 이름 없음


def test_exempt_user_only_for_plain_unreserved():
    procs = [_proc("labmember", 2)]
    assert warn.classify(_info(exempt=["labmember"]), procs, NOW, CFG)["warnings"] == {}
    # 곧(20분 뒤) 남의 예약이 시작되면 면제자도 경고 + 시작 시각 안내
    r = warn.classify(_info(reservations=[_res("alice", 2, 20, 80)], exempt=["labmember"]), procs, NOW, CFG)
    w = r["warnings"]["labmember"][0]
    assert w["kind"] == "unreserved" and "14:20부터 다른 사용자 예약" in w["text"]
    # 남의 예약 GPU 를 쓰면 면제자도 mismatch
    r = warn.classify(_info(reservations=[_res("alice", 2, -10, 50)], exempt=["labmember"]), procs, NOW, CFG)
    assert _kinds(r) == {"labmember": [(2, "mismatch")]}


def test_alias_and_ending():
    aliases = {"justin": ["sahong"], "sahong": ["justin"]}
    procs = [_proc("sahong", 0)]
    # 예약명 justin ↔ OS 계정 sahong 동일인, 끝까지 2시간 → 경고 없음
    r = warn.classify(_info(reservations=[_res("justin", 0, -30, 120)], aliases=aliases), procs, NOW, CFG)
    assert r["warnings"] == {}
    assert [x["gpu_index"] for x in r["mine"]["sahong"]] == [0]
    # 5분 뒤 종료 → ending 안내
    r = warn.classify(_info(reservations=[_res("justin", 0, -30, 5)], aliases=aliases), procs, NOW, CFG)
    assert _kinds(r) == {"sahong": [(0, "ending")]}
    assert "14:05에 끝납니다" in r["warnings"]["sahong"][0]["text"]
    # 바로 이어지는 본인 예약이 있으면 끝이 아니다(연속 예약 합치기)
    r = warn.classify(_info(reservations=[_res("justin", 0, -30, 5), _res("justin", 0, 5, 120)],
                            aliases=aliases), procs, NOW, CFG)
    assert r["warnings"] == {}


def test_free_now_and_render():
    info = _info(reservations=[_res("alice", 1, -60, 120), _res("rita", 0, 90, 150)])
    r = warn.classify(info, [_proc("carol", 2)], NOW, CFG)
    assert r["free_now"] == [0, 3]
    assert "다음 예약 15:30" in r["status"][0]
    text = warn.render_warn("carol", r["warnings"]["carol"], r, "fai001", "https://portal.example", NOW)
    assert text.startswith("[GPU 예약 경고] fai001")
    assert "지금 예약도 사용도 없는 GPU: 0, 3" in text
    assert "코딩 에이전트에게" in text


def test_idle_process_and_dead_gpu():
    # 메모리 28MiB·사용률 0% 인 빈 MPS 데몬 = 사용 아님(경고·사용 중 표시 없음)
    idle = [_proc("koo", g, pid=632680, mem=28) for g in (0, 1, 2)]
    r = warn.classify(_info(), idle, NOW, CFG, util={0: 0, 1: 0, 2: 0}, alive=[0, 1, 2])
    assert r["warnings"] == {}
    assert r["free_now"] == [0, 1, 2]                   # GPU3 은 응답 없음 → 추천 제외
    assert "응답 없음" in r["status"][3] and "사용 중" not in r["status"][0]
    # 메모리가 적어도 GPU 사용률이 높으면 사용 중
    r = warn.classify(_info(), idle, NOW, CFG, util={0: 80, 1: 0, 2: 0}, alive=[0, 1, 2])
    assert _kinds(r) == {"koo": [(0, "unreserved")]}


def test_upcoming_conflicts_blocks_and_free_now():
    procs = [_proc("labmember", 1)]
    blk = {"gpu_index": 1, "reason": "수업", "session_id": 3,
           "start_at": _z(NOW + timedelta(minutes=5)), "end_at": _z(NOW + timedelta(hours=2))}
    # 5분 뒤 관리자 차단이 시작되는 GPU 를 쓰는 면제자도 미리 경고
    r = warn.classify(_info(blocks=[blk], exempt=["labmember"]), procs, NOW, CFG)
    assert "14:05부터 관리자 차단(수업)이 있으니" in r["warnings"]["labmember"][0]["text"]
    # 임박 충돌이 새로 생기면 key 가 바뀌어(→ 터미널 즉시 재알림) 30분을 기다리지 않는다
    k0 = warn.classify(_info(), [_proc("carol", 1)], NOW, CFG)["warnings"]["carol"][0]["key"]
    k1 = warn.classify(_info(reservations=[_res("alice", 1, 20, 80)]), [_proc("carol", 1)], NOW, CFG)
    assert k1["warnings"]["carol"][0]["key"] != k0
    # 곧 남의 예약·차단이 시작될 GPU 는 '빈 GPU' 추천에서 뺀다
    r = warn.classify(_info(reservations=[_res("alice", 2, 2, 60)], blocks=[blk]), [], NOW, CFG)
    assert r["free_now"] == [0, 3]


def test_container_detection():
    assert warn.in_container("0::/system.slice/docker-" + "a" * 64 + ".scope")
    assert warn.in_container("12:memory:/docker/" + "b" * 64)
    assert warn.in_container("0::/kubepods/besteffort/pod1/abc")
    assert not warn.in_container("0::/user.slice/user-2000.slice/user@2000.service/app.slice/docker-x.scope")
    assert not warn.in_container("0::/user.slice/user-2000.slice/session-5.scope")
    # 사용자 슬라이스 아래 rootless 컨테이너는 그 사람 작업 → 판정 대상
    assert not warn.in_container("0::/user.slice/user-2000.slice/user@2000.service/libpod-" + "c" * 64 + ".scope")


def test_run_rejects_foreign_response(tmp_path, monkeypatch):
    """엉뚱한 서비스의 200 응답(다른 노드·필드 누락)을 '예약 없음'으로 읽어 경고하지 않는다."""
    monkeypatch.setattr(warn, "collect_gpu_procs", lambda: {"procs": [_proc("carol", 0)], "util": {}, "alive": [0]})
    cfg = dict(warn.DEFAULTS, AGENT_TOKEN="x", NODE_NAME="fai001", RUN_DIR=str(tmp_path / "run"))
    for bad in ({"status": "ok"}, dict(_info(), node="fai002"), []):
        monkeypatch.setattr(warn, "fetch_node_info", lambda cfg, b=bad: b)
        assert warn.run(cfg) == 1
    assert not (tmp_path / "run").exists()


def test_parse_ts_variants():
    base = datetime(2030, 3, 2, 5, 0, tzinfo=timezone.utc)
    assert warn.parse_ts("2030-03-02T05:00:00.000Z") == base
    assert warn.parse_ts("2030-03-02T05:00:00Z") == base
    assert warn.parse_ts("2030-03-02T14:00:00+09:00") == base
    assert warn.parse_ts("2030-03-02T05:00:00.123456+00:00") == base.replace(microsecond=123456)


# --------------------------------------------------------------------------- #
# 전달 (파일 + 실제 pty)
# --------------------------------------------------------------------------- #
def test_run_writes_files_and_tty_with_repeat_suppression(tmp_path, monkeypatch):
    me = pwd.getpwuid(os.getuid())
    master, slave = os.openpty()
    slave_path = os.ttyname(slave)
    # 실제 사용자 터미널에 쓰지 않도록 테스트 pty 만 대상으로
    monkeypatch.setattr(warn, "user_ttys", lambda uid: [slave_path] if uid == me.pw_uid else [])
    state = {"info": _info(reservations=[_res(me.pw_name, 0, 60, 120)])}
    monkeypatch.setattr(warn, "fetch_node_info", lambda cfg: state["info"])
    monkeypatch.setattr(warn, "collect_gpu_procs", lambda: {
        "procs": [_proc(me.pw_name, 2, uid=me.pw_uid)], "util": {0: 0, 1: 0, 2: 0, 3: 0}, "alive": [0, 1, 2, 3]})
    cfg = dict(warn.DEFAULTS, AGENT_TOKEN="x", NODE_NAME="fai001",
               RUN_DIR=str(tmp_path / "run"), STATE_DIR=str(tmp_path / "state"))

    assert warn.run(cfg) == 0
    out = os.read(master, 65536).decode("utf-8")
    assert "[GPU 예약 경고] fai001" in out and "GPU 2: 예약 없이 사용 중" in out and "\r\n" in out
    udir = tmp_path / "run" / "user"
    mine = udir / me.pw_name
    assert (mine / "warn").read_text().startswith("[GPU 예약 경고]")
    assert "15:00~16:00" in (mine / "info").read_text()
    upg = warn.private_group(me, warn.human_users(1000))
    assert oct((mine / "warn").stat().st_mode & 0o777) == ("0o640" if upg else "0o600")
    assert oct(mine.stat().st_mode & 0o777) == ("0o750" if upg else "0o711")
    assert oct(udir.stat().st_mode & 0o777) == "0o711"
    # 사람 계정 전부에 디렉터리가 미리 있고(존재 = 정보 아님), 내용 없는 사용자는 파일이 없다
    others = [pw.pw_name for pw in warn.human_users(1000) if pw.pw_name != me.pw_name]
    assert all((udir / u).is_dir() and not (udir / u / "warn").exists() for u in others)
    assert "GPU0  예약 없음 · 다음 예약 15:00" in (tmp_path / "run" / "status.txt").read_text()

    # 같은 경고는 REPEAT_MINUTES 안에 다시 쓰지 않는다
    os.set_blocking(master, False)
    assert warn.run(cfg) == 0
    with pytest.raises(BlockingIOError):
        os.read(master, 65536)
    # 남의 예약이 시작되면(종류 변경) 즉시 다시 쓴다
    state["info"] = _info(reservations=[_res("alice", 2, -1, 60)])
    assert warn.run(cfg) == 0
    assert "다른 사용자가 예약한 GPU" in os.read(master, 65536).decode("utf-8")
    # 경고가 사라지면 .warn 도 지운다
    monkeypatch.setattr(warn, "collect_gpu_procs", lambda: {"procs": [], "util": {}, "alive": [0, 1, 2, 3]})
    assert warn.run(cfg) == 0
    assert not (mine / "warn").exists()
    os.close(master)
    os.close(slave)


def test_run_skips_everything_when_server_unreachable(tmp_path, monkeypatch):
    def boom(cfg):
        raise OSError("down")
    monkeypatch.setattr(warn, "fetch_node_info", boom)
    cfg = dict(warn.DEFAULTS, AGENT_TOKEN="x", NODE_NAME="fai001", RUN_DIR=str(tmp_path / "run"))
    assert warn.run(cfg) == 1
    assert not (tmp_path / "run").exists()


# --------------------------------------------------------------------------- #
# MCP 서버
# --------------------------------------------------------------------------- #
class FakeApi(mcp.Api):
    def __init__(self, mine=(), reservations=(), blocks=()):
        super().__init__(base="https://portal.example", token="lrt_x")
        self.mine, self.reservations, self.blocks, self.posted = list(mine), list(reservations), list(blocks), []

    def call(self, method, path, body=None, query=None):
        if path == "/me":
            return {"auth_enabled": True, "authenticated": True, "name": "rita", "role": "researcher"}
        if path == "/my/reservations":
            return {"reservations": self.mine}
        if path == "/nodes":
            gpus = [{"index": i, "in_use": i == 1} for i in range(3)] + [{"index": 3, "error": "dead"}]
            return {"nodes": [{"node": "fai001", "gpu_total": 4, "gpus": gpus}]}
        if path == "/availability":
            return {"reservations": self.reservations, "blocks": self.blocks, "blocks_enforced": True}
        if path == "/reserve":
            self.posted.append(body)
            return {"ids": [11], "count": 1}
        raise AssertionError(path)


def _rpc(api, method, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if mid is not None:
        msg["id"] = mid
    return mcp.handle(msg, api)


def test_mcp_protocol_basics():
    api = FakeApi()
    init = _rpc(api, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "0"}})["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"]
    assert "CUDA_VISIBLE_DEVICES" in init["instructions"]
    assert _rpc(api, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-11-25"
    assert _rpc(api, "notifications/initialized", mid=None) is None
    tools = _rpc(api, "tools/list")["result"]["tools"]
    assert {t["name"] for t in tools} == {"get_my_reservations", "check_my_gpu_usage", "find_available_gpus",
                                          "reserve_gpu", "extend_reservation", "release_reservation"}
    assert all("fn" not in t and t["inputSchema"]["type"] == "object" for t in tools)
    json.dumps(tools)   # 직렬화 가능
    assert _rpc(api, "tools/call", {"name": "nope"})["error"]["code"] == -32602
    assert _rpc(api, "resources/list")["error"]["code"] == -32601


def test_mcp_reserve_skips_when_already_reserved_and_autopicks(monkeypatch):
    monkeypatch.setattr(mcp, "now_utc", lambda: NOW)
    monkeypatch.setattr(mcp, "local_node", lambda known=None: "fai001")
    mine = [dict(_res("rita", 2, -30, 60), id=5, node="fai001", purpose=None)]
    api = FakeApi(mine=mine)
    out = _rpc(api, "tools/call", {"name": "reserve_gpu", "arguments": {"hours": 2}})["result"]
    assert not out["isError"] and "새로 만들지 않았습니다" in out["content"][0]["text"] and api.posted == []

    # 내 예약 없음 → GPU0 은 남의 예약, GPU1 은 실측 사용 중 → 비어 있는 GPU2 를 먼저 고른다
    api = FakeApi(reservations=[dict(_res("alice", 0, -30, 60), node="fai001")])
    out = _rpc(api, "tools/call", {"name": "reserve_gpu", "arguments": {"hours": 2, "purpose": "실험"}})["result"]
    assert not out["isError"], out
    assert api.posted == [{"node": "fai001", "gpu_indices": [2], "purpose": "실험",
                           "start_at": "2030-03-02T05:00:00.000Z", "end_at": "2030-03-02T07:00:00.000Z"}]
    assert "CUDA_VISIBLE_DEVICES=2" in out["content"][0]["text"]

    # end/hours 둘 다 없으면 도구 오류(isError) — 예약을 멋대로 만들지 않는다
    out = _rpc(FakeApi(), "tools/call", {"name": "reserve_gpu", "arguments": {}})["result"]
    assert out["isError"] and "hours" in out["content"][0]["text"]


def test_mcp_find_excludes_dead_gpu(monkeypatch):
    monkeypatch.setattr(mcp, "now_utc", lambda: NOW)
    monkeypatch.setattr(mcp, "local_node", lambda known=None: None)
    out = _rpc(FakeApi(), "tools/call", {"name": "find_available_gpus", "arguments": {}})["result"]
    text = out["content"][0]["text"]
    assert "fai001: GPU 0, 1(지금 누군가 예약 없이 사용 중), 2" in text and "3" not in text.split("GPU")[-1]


def test_mcp_survives_bad_input(monkeypatch):
    api = FakeApi()
    for params in ({"name": "find_available_gpus", "arguments": {"hours": 1e20}},
                   {"name": "find_available_gpus", "arguments": ["not", "object"]}):
        out = _rpc(api, "tools/call", params)["result"]
        assert out["isError"]
    assert _rpc(api, "tools/call", {"name": ["unhashable"]})["error"]["code"] == -32602

    def boom(api, args):
        raise RuntimeError("unexpected")
    monkeypatch.setitem(mcp.TOOL_BY_NAME, "get_my_reservations", dict(mcp.TOOL_BY_NAME["get_my_reservations"], fn=boom))
    out = _rpc(api, "tools/call", {"name": "get_my_reservations"})["result"]
    assert out["isError"] and "RuntimeError" in out["content"][0]["text"]


def test_mcp_end_hhmm_rolls_to_next_day(monkeypatch):
    late = datetime(2030, 3, 2, 14, 30, tzinfo=timezone.utc)   # 23:30 KST
    monkeypatch.setattr(mcp, "now_utc", lambda: late)
    s, e = mcp.window("now", "01:00", None)
    assert e - s == timedelta(hours=1, minutes=30)


def test_mcp_no_token_message():
    out = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": "get_my_reservations", "arguments": {}}},
                     mcp.Api(base="https://portal.example", token=""))["result"]
    assert out["isError"] and "/tokens" in out["content"][0]["text"]


def test_mcp_parse_time():
    assert mcp.parse_time("14:30", NOW) == NOW + timedelta(minutes=30)          # 오늘 KST
    assert mcp.parse_time("2030-03-02 15:00", NOW) == NOW + timedelta(hours=1)  # 오프셋 없음 = KST
    assert mcp.parse_time("2030-03-02T06:00:00Z", NOW) == NOW + timedelta(hours=1)
    assert mcp.parse_time("now", NOW) == NOW
    with pytest.raises(mcp.ToolError):
        mcp.parse_time("내일 아침", NOW)
    assert mcp.to_api(NOW) == "2030-03-02T05:00:00.000Z"
    assert mcp.kst("2030-03-02T05:00:00.000Z") == "03/02(토) 14:00"


def test_mcp_stdio_loop(monkeypatch):
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
             "{broken"]
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(("\n".join(lines) + "\n").encode())))
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii"))   # 로캘이 UTF-8 이 아니어도
    assert mcp.serve() == 0
    replies = [json.loads(x) for x in raw.getvalue().decode("utf-8").splitlines()]
    assert replies[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert replies[1]["error"]["code"] == -32700 and len(replies) == 2


# --------------------------------------------------------------------------- #
# Claude Code 훅
# --------------------------------------------------------------------------- #
def _hook(monkeypatch, capsys, event, payload=None):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload or {})))
    assert hook.main(["labres-agent-hook", event]) == 0
    return capsys.readouterr().out


def test_agent_hook_events(tmp_path, monkeypatch, capsys):
    me = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setattr(hook, "USER_DIR", str(tmp_path))
    monkeypatch.setattr(hook, "STATE_FILE", str(tmp_path / "state.json"))
    assert _hook(monkeypatch, capsys, "UserPromptSubmit") == ""          # 경고 없음 → 조용
    (tmp_path / me).mkdir()
    (tmp_path / me / "info").write_text("내 예약(fai001): ...\n")
    assert "내 예약" in _hook(monkeypatch, capsys, "SessionStart")
    (tmp_path / me / "warn").write_text("[GPU 예약 경고] fai001\n⚠ GPU 2: 예약 없이 사용 중\n")
    assert "[GPU 예약 경고]" in _hook(monkeypatch, capsys, "UserPromptSubmit")
    out = json.loads(_hook(monkeypatch, capsys, "PostToolUse", {"session_id": "s1"}))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "GPU 2" in out["hookSpecificOutput"]["additionalContext"]
    assert _hook(monkeypatch, capsys, "PostToolUse", {"session_id": "s1"}) == ""   # 같은 경고 반복 억제
    assert _hook(monkeypatch, capsys, "PostToolUse", {"session_id": "s2"}) != ""   # 다른 세션은 따로
    (tmp_path / me / "warn").write_text("[GPU 예약 경고] fai001\n⚠ GPU 3: 다른 사용자가 예약한 GPU\n")
    assert "GPU 3" in _hook(monkeypatch, capsys, "PostToolUse", {"session_id": "s1"})  # 내용 바뀌면 즉시
    os.utime(tmp_path / me / "warn", (0, 0))   # 에이전트 멈춤(갱신 없음) → 옛 경고 무시
    assert _hook(monkeypatch, capsys, "UserPromptSubmit") == ""


def test_mcp_reads_token_saved_after_start(tmp_path, monkeypatch):
    """MCP 가 뜬 뒤에 저장한 토큰도 재시작 없이 쓴다(1.0.0 은 시작 때 한 번만 읽어 '토큰 없음'이 계속됐다)."""
    monkeypatch.delenv("LABRES_TOKEN", raising=False)
    monkeypatch.setattr(mcp, "TOKEN_FILE", str(tmp_path / "token"))
    api = mcp.Api(base="https://portal.example")
    assert api.token == ""
    (tmp_path / "token").write_text("lrt_saved_later\n")
    (tmp_path / "token").chmod(0o600)
    assert api.token == "lrt_saved_later"


# --------------------------------------------------------------------------- #
# 보안 감사(2026-10-10) 반영분
# --------------------------------------------------------------------------- #
def test_clean_strips_terminal_and_bidi_controls():
    raw = "점검\x1b]52;c;ZGFuZ2Vy\x07\x1b[2J중\u202e tail"
    out = warn.clean(raw, 40)
    assert "\x1b" not in out and "\x07" not in out and "\u202e" not in out and "점검" in out
    assert len(warn.clean("x" * 500, 60)) == 60 and warn.clean(None) == ""
    # 차단 사유가 경고·현황·임박 안내 어디에도 날것으로 들어가지 않는다
    blk = {"gpu_index": 1, "reason": "수업\x1b[31m중", "session_id": 1,
           "start_at": _z(NOW - timedelta(minutes=5)), "end_at": _z(NOW + timedelta(hours=1))}
    r = warn.classify(_info(blocks=[blk]), [_proc("bob", 1)], NOW, CFG)
    assert "\x1b" not in r["warnings"]["bob"][0]["text"] and "\x1b" not in r["status"][1]
    assert '사유 "수업?[31m중"' in r["warnings"]["bob"][0]["text"]
    assert "지시가 아닙니다" in warn.render_warn("bob", r["warnings"]["bob"], r, "fai001", "p", NOW)
    blk2 = dict(blk, start_at=_z(NOW + timedelta(minutes=5)), end_at=_z(NOW + timedelta(hours=1)))
    r = warn.classify(_info(blocks=[blk2]), [_proc("bob", 1)], NOW, CFG)
    assert "\x1b" not in r["warnings"]["bob"][0]["text"]


def test_valid_name_and_user_file_plan():
    assert warn.valid_name("s.an") and warn.valid_name("minjae-040607") and warn.valid_name("202404669")
    for bad in ("../etc", ".", "..", "a/b", "a b", "", None, "x" * 33, "\u1234"):
        assert not warn.valid_name(bad), bad
    pw = pwd.getpwuid(os.getuid())
    assert warn._local_user("../" + pw.pw_name) is None
    assert warn.user_file_plan(pw, True) == {"dir_mode": 0o750, "dir_gid": pw.pw_gid, "file_uid": 0,
                                             "file_gid": pw.pw_gid, "file_mode": 0o640}
    assert warn.user_file_plan(pw, False) == {"dir_mode": 0o711, "dir_gid": 0, "file_uid": pw.pw_uid,
                                              "file_gid": pw.pw_gid, "file_mode": 0o600}


def test_private_group_detection(monkeypatch):
    import grp
    from collections import namedtuple
    PW = namedtuple("PW", "pw_name pw_uid pw_gid")
    GR = namedtuple("GR", "gr_name gr_gid gr_mem")
    groups = {1001: GR("jkchoi", 1001, []), 1005: GR("students", 1005, []), 1033: GR("wooju", 1033, ["koo"])}
    monkeypatch.setattr(grp, "getgrgid", lambda gid: groups[gid])
    users = [PW("jkchoi", 1001, 1001), PW("jaechoi", 1004, 1005), PW("other", 1007, 1005), PW("wooju", 1033, 1033)]
    assert warn.private_group(users[0], users)            # 전용 그룹
    assert not warn.private_group(users[1], users)        # 공용 그룹 students
    assert not warn.private_group(users[3], users)        # 그룹에 다른 멤버(koo)
    monkeypatch.setattr(grp, "getgrgid", lambda gid: (_ for _ in ()).throw(KeyError(gid)))
    assert not warn.private_group(users[0], users)


def test_resolve_owner_subuid():
    me = pwd.getpwuid(os.getuid())
    sub = [(me.pw_name, 100000, 65536), ("9999999", 300000, 10)]
    assert warn.resolve_owner(me.pw_uid, sub) == (me.pw_uid, me.pw_name)
    assert warn.resolve_owner(100005, sub) == (me.pw_uid, me.pw_name)     # rootless 컨테이너 안 → 주인
    assert warn.resolve_owner(999999, sub) == (999999, "999999")           # 아무 데도 없음
    assert warn.resolve_owner(300001, sub) == (300001, "300001")           # 주인 계정이 없음


def test_lab_user_exempt_only_outside_login_sessions():
    procs = [dict(_proc("user", 0, uid=1000), user_slice=True),      # SSH 로 로그인한 실계정 user
             dict(_proc("user", 1, pid=2, uid=1000), user_slice=False)]  # 시스템 서비스(컨테이너 밖) 쪽
    r = warn.classify(_info(), procs, NOW, CFG)
    assert _kinds(r) == {"user": [(0, "unreserved")]}


def test_fetch_requires_https_and_never_follows_redirects(monkeypatch):
    with pytest.raises(ValueError):
        warn.fetch_node_info(dict(warn.DEFAULTS, LABRES_URL="http://127.0.0.1:1", AGENT_TOKEN="x", NODE_NAME="n"))
    # 리다이렉트 응답 → 예외(토큰 재전송 없음)
    import http.server, threading
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.headers.get("Authorization"))
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d" % srv.server_port
        with pytest.raises(Exception):
            warn.fetch_node_info(dict(warn.DEFAULTS, LABRES_URL=url, AGENT_TOKEN="x", NODE_NAME="n", ALLOW_HTTP="1"))
        monkeypatch.setenv("LABRES_ALLOW_HTTP", "1")
        with pytest.raises(mcp.ToolError):
            mcp.Api(base=url, token="lrt_x").call("GET", "/me")
    finally:
        srv.shutdown()
    assert hits == ["Bearer x", "Bearer lrt_x"]   # 원 서버에만 한 번씩, 리다이렉트 대상엔 안 감


def test_mcp_requires_https_and_safe_token_file(tmp_path, monkeypatch):
    monkeypatch.delenv("LABRES_ALLOW_HTTP", raising=False)
    monkeypatch.delenv("LABRES_TOKEN", raising=False)
    with pytest.raises(mcp.ToolError):
        mcp.Api(base="http://portal.example", token="lrt_x").call("GET", "/me")
    tf = tmp_path / "token"
    monkeypatch.setattr(mcp, "TOKEN_FILE", str(tf))
    tf.write_text("lrt_abc\n")
    tf.chmod(0o644)
    with pytest.raises(mcp.ToolError):
        mcp.Api(base="https://portal.example").token
    tf.chmod(0o600)
    assert mcp.Api(base="https://portal.example").token == "lrt_abc"
