#!/usr/bin/env sh
# Pull the local models Magpie uses by default and create a 16k-context variant of the chat
# model. Ollama's default context is 4096 tokens, which silently truncates the recomposition
# prompt (system prompt with the JSON schema gets cut first) and makes small models drift.
#
# Usage: scripts/setup_ollama_models.sh
set -eu

ollama pull qwen3:8b        # chat: task understanding, text analysis, recomposition
ollama pull qwen2.5vl:7b    # vision: image description / style keywords
ollama pull bge-m3          # embeddings (multilingual)

TMP="$(mktemp)"
printf 'FROM qwen3:8b\nPARAMETER num_ctx 16384\n' > "$TMP"
ollama create qwen3:8b-16k -f "$TMP"
rm -f "$TMP"

echo
echo "done. .env defaults: MAGPIE_CHAT_MODEL=qwen3:8b-16k MAGPIE_VISION_MODEL=qwen2.5vl:7b MAGPIE_EMBED_MODEL=bge-m3"
echo "check with: magpie doctor"
