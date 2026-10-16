# Sources

Record every external fact (model repo, license, API param) with its source and the date checked.

| Date | Fact | Source |
|---|---|---|
| 2026-10-16 | Odoo 18 Community official Docker image `odoo:18` | https://hub.docker.com/_/odoo |
| 2026-10-16 | Odoo External API is JSON-RPC / XML-RPC at `/jsonrpc` & `/xmlrpc/2` | https://www.odoo.com/documentation/18.0/developer/reference/external_api.html |
| 2026-10-16 | PostgreSQL 16 supported by Odoo 18 | https://www.odoo.com/documentation/18.0/administration/on_premise/deploy.html |

> TODO: add sources for Whisper variants, Qwen3 sizes/licenses, NAMAA-Saudi-TTS and Chatterbox
> when those benchmarks are set up (blocked on GPU box).
| 2026-10-16 | Qwen3 dense sizes: 0.6B,1.7B,4B,8B,14B,32B; Ollama tags expose tools+thinking; Ollama >=0.6.6 | https://registry.ollama.ai/library/qwen3/tags |
| 2026-10-16 | Ollama tool-calling via /api/chat `tools` param returns message.tool_calls | https://github.com/ollama/ollama/blob/main/docs/api.md |
