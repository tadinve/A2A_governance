# Pick up a deploy_to_gcp.sh run in a new terminal, or a later session.
#
# This must be SOURCED, not executed -- exporting variables and activating a
# venv both only mean anything in the current shell, and a script run as
# `bash activate.sh` or `./activate.sh` does that work in a subshell that
# exits immediately, leaving your actual terminal untouched.
#
#   source activate.sh

if [[ "${BASH_SOURCE[0]:-$0}" == "${0}" ]]; then
  echo "Run this with 'source activate.sh', not './activate.sh' or 'bash activate.sh' --" >&2
  echo "otherwise nothing it sets survives past this command." >&2
  return 1 2>/dev/null || exit 1
fi

_ACTIVATE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "$_ACTIVATE_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$_ACTIVATE_DIR/.env"
  set +a
  echo "loaded .env (GOOGLE_CLOUD_PROJECT=${GOOGLE_CLOUD_PROJECT:-unset})"
else
  echo "no .env yet -- run 'bash deploy_to_gcp.sh PROJECT_ID' first to generate one" >&2
fi

if [[ -f "$_ACTIVATE_DIR/cloud/.venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$_ACTIVATE_DIR/cloud/.venv/bin/activate"
  echo "activated cloud/.venv"
else
  echo "no cloud/.venv -- create it: python3 -m venv cloud/.venv && cloud/.venv/bin/pip install -r cloud/requirements.txt" >&2
fi

unset _ACTIVATE_DIR
