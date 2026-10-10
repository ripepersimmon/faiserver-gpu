# /etc/profile.d/labres-gpu.sh — GPU 예약 현황·경고 (scripts/node-agent/install.sh 가 설치)
#
# labres-gpu-warn(root 타이머)이 1분마다 갱신하는 파일을 보여 줄 뿐이다. 3분 넘게 갱신이 없으면
# (에이전트 멈춤) 옛 내용은 보여 주지 않는다.
#   대화형 로그인(사람의 SSH):        이 노드 GPU 예약 현황 + 내 예약 + 내 경고 → stdout
#   비대화형 로그인 셸(bash -lc):      내 경고가 있을 때만 → stderr
#     Codex 같은 코딩 에이전트는 명령을 로그인 셸로 실행하므로, 경고가 있는 동안 모든 명령 결과에
#     경고가 붙어 에이전트가 읽게 된다. scp/rsync/ssh host cmd 는 로그인 셸이 아니라 영향 없다.
_labres_d=/run/labres-gpu-warn
_labres_u=${USER:-$(id -un 2>/dev/null)}
_labres_fresh() { [ -f "$1" ] && [ -n "$(find "$1" -mmin -3 2>/dev/null)" ]; }
case $- in
  *i*)
    _labres_fresh "$_labres_d/status.txt" && cat "$_labres_d/status.txt"
    _labres_fresh "$_labres_d/user/$_labres_u/info" && cat "$_labres_d/user/$_labres_u/info"
    _labres_fresh "$_labres_d/user/$_labres_u/warn" && cat "$_labres_d/user/$_labres_u/warn"
    ;;
  *)
    _labres_fresh "$_labres_d/user/$_labres_u/warn" && cat "$_labres_d/user/$_labres_u/warn" >&2
    ;;
esac
unset _labres_d _labres_u
unset -f _labres_fresh 2>/dev/null
true
