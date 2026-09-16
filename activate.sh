# Pick up a deploy_to_gcp.sh run in a new terminal, or a later session.
#
# This must be SOURCED, not executed -- exporting variables and activating a
# venv both only mean anything in the current shell, and a script run as
# `bash activate.sh` or `./activate.sh` does that work in a subshell that
# exits immediately, leaving your actual terminal untouched.
#
#   source activate.sh
#
# Written to run under both bash and zsh, because the two need genuinely
# different mechanisms here, not just different syntax:
#
# * detecting "was I sourced": bash exposes this by comparing $BASH_SOURCE to
#   $0; zsh has no such array, and $0 is left unchanged by `source`, so that
#   comparison is always true under zsh and would misreport every sourced run
#   as executed. zsh's own signal is $ZSH_EVAL_CONTEXT ending in ":file".
# * finding this file's own directory: bash's answer is $BASH_SOURCE[0]; the
#   zsh equivalent is the prompt-expansion form ${(%):-%x}, which -- unlike
#   $0 -- does reflect the sourced file, not the calling shell.

_activate_sourced=0
if [ -n "${ZSH_VERSION:-}" ]; then
  case "$ZSH_EVAL_CONTEXT" in *:file) _activate_sourced=1 ;; esac
  _activate_self="${(%):-%x}"
elif [ -n "${BASH_VERSION:-}" ]; then
  [ "${BASH_SOURCE[0]}" != "${0}" ] && _activate_sourced=1
  _activate_self="${BASH_SOURCE[0]}"
fi

if [ "$_activate_sourced" != 1 ]; then
  echo "Run this with 'source activate.sh', not './activate.sh' or 'bash activate.sh' --" >&2
  echo "otherwise nothing it sets survives past this command." >&2
  unset _activate_sourced _activate_self
  return 1 2>/dev/null || exit 1
fi

_ACTIVATE_DIR="$(cd "$(dirname "$_activate_self")" && pwd)"
unset _activate_sourced _activate_self

if [ -f "$_ACTIVATE_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$_ACTIVATE_DIR/.env"
  set +a
  echo "loaded .env (GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT:-unset})"
else
  echo "no .env yet -- run 'bash deploy_to_gcp.sh PROJECT_ID' first to generate one" >&2
fi

if [ -f "$_ACTIVATE_DIR/cloud/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$_ACTIVATE_DIR/cloud/.venv/bin/activate"
  echo "activated cloud/.venv"
else
  echo "no cloud/.venv -- create it: python3 -m venv cloud/.venv && cloud/.venv/bin/pip install -r cloud/requirements.txt" >&2
fi

unset _ACTIVATE_DIR
