# ────────────────────────────────────────────────────────────────────
# Edge TTS provider — uses Microsoft's free TTS via edge-tts.
#
# No API key needed. Supports many Chinese voices.
# Install: pip install edge-tts
#
# Popular Chinese voices:
#   zh-CN-XiaoxiaoNeural   (女声，自然)
#   zh-CN-YunxiNeural      (男声，自然)
#   zh-CN-XiaoyiNeural     (女声，温柔)
#   zh-CN-YunjianNeural    (男声，播音)
# ────────────────────────────────────────────────────────────────────

tts_check() {
  if ! command -v edge-tts >/dev/null; then
    echo "✗ edge-tts not found." >&2
    return 1
  fi
}

tts_install_help() {
  cat <<'EOF' >&2
To use the Edge TTS provider (free, no API key):

  Install:  pip install edge-tts

  Popular Chinese voices (set via --voice or PRESENTATION_TTS_VOICE):
    zh-CN-XiaoxiaoNeural   (female, natural)
    zh-CN-YunxiNeural      (male, natural)
    zh-CN-XiaoyiNeural     (female, gentle)
    zh-CN-YunjianNeural    (male, broadcast)

  List all voices:  edge-tts --list-voices | grep zh-CN
EOF
}

tts_synthesize() {
  local text="$1"
  local out="$2"
  local voice="${3:-}"

  if [[ -z "$voice" ]]; then
    voice="zh-CN-XiaoxiaoNeural"
  fi

  edge-tts --voice "$voice" --text "$text" --write-media "$out" \
    >/dev/null 2>&1
}
