# /etc/profile.d/labres-models.sh — 공유 모델 캐시 기본값 (node-agent/install.sh 가 설치, GUIDELINES.md §2)
#
# Hugging Face 모델(transformers·vLLM·huggingface-cli 등)이 홈이 아니라 이 노드의 공유 캐시로 받아지게 한다.
# 같은 모델을 사람마다 따로 받아 디스크가 차는 것을 막는 게 목적이다.
#   - HF_HOME 이 아니라 HF_HUB_CACHE 만 바꾼다. HF_HOME 에는 개인 토큰(token)이 들어가서 공유하면 안 된다.
#   - 본인이 이미 캐시 위치를 정했으면(HF_HUB_CACHE·HF_HOME·옛 이름) 건드리지 않는다.
#   - 폴더가 없거나, 쓸 수 없거나, install.sh 가 만든 모양(root 소유 + sticky)이 아니면 아무것도 하지 않는다
#     (설치 전 노드·누가 바꿔 놓은 폴더 → 종전처럼 홈 캐시).
#   - zsh·VS Code Remote·Jupyter·cron 처럼 /etc/profile.d 를 읽지 않는 곳에는 적용되지 않는다(GUIDELINES.md §2).
_labres_hub=/data/models/hf/hub
if [ -z "${HF_HUB_CACHE:-}" ] && [ -z "${HF_HOME:-}" ] && [ -z "${HUGGINGFACE_HUB_CACHE:-}" ] \
   && [ -z "${TRANSFORMERS_CACHE:-}" ] && [ -d "$_labres_hub" ] && [ -w "$_labres_hub" ] && [ -k "$_labres_hub" ] \
   && [ "$(stat -c %u "$_labres_hub" 2>/dev/null)" = "0" ]; then
  HF_HUB_CACHE=$_labres_hub
  export HF_HUB_CACHE
fi
unset _labres_hub
true
