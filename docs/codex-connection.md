# Connect Codex to the same research workspace

After logging in to the cloud website, open **设置 → Codex 连接**. Create a separate token per device, starting with read-only if appropriate. The default validity is 90 days, maximum 365. Editing permits page creation/property/block edits and pending excerpts; it does not permit deleting pages, archiving/restoring, accepting evidence, or adopting OCR/corrections. Token plaintext appears once; the database stores only a server-keyed HMAC digest. Revocation takes effect on subsequent requests, including an existing MCP connection.

Provide the token as `LITWORKBENCH_TOKEN` in the environment used to launch Codex (for example via your credential manager). Do not put the value in URLs, config files, prompts or screenshots. Add the endpoint with your real host:

```sh
codex mcp add litworkbench --url https://YOUR-HOST/mcp --bearer-token-env-var LITWORKBENCH_TOKEN
```

This user-run command registers a connection in your Codex configuration. The acceptance script instead uses per-invocation `-c` overrides and `--ignore-user-config`; it leaves your global connection list untouched. Cookies authorize the website only; MCP requires the scoped bearer token.

The ten tools are `workspace_search`, `page_get`, `page_create`, `page_update_properties`, `page_edit_blocks`, `page_get_history`, `document_get`, `unit_read`, `evidence_create` and `evidence_get`. Reads return bounded `json-fragment` text. Concatenate `text` in offset order using `next_cursor` until `truncated=false`, then parse JSON; do not treat a fragment as a complete body. Cursors expire in one hour and bind object/query identity and snapshot hash/revision. `CURSOR_CHANGED` means restart the read rather than joining different versions. Limits are 24,000 serialized characters per read response, 1 MiB / 2,000 blocks per page, depth 8 and 100 block operations per batch.

Page writes need a unique `request_id`; edits also need `expected_revision`. Retry the identical request with the same ID after an uncertain network result. A different payload with an already-used ID is rejected. A stale revision requires rereading and resolving the conflict. Block operation shapes:

```json
{"op":"insert","parent_block_id":null,"index":0,"block":{"id":"stable-id","type":"paragraph","props":{},"content":[{"type":"text","text":"Research note","styles":{}}],"children":[]}}
```

`replace` uses `block_id` and `block`; `move` uses `block_id`, `parent_block_id` and `index` (after removal). Excerpt creation requires the exact effective unit `version`, `text_sha256` as `expected_hash`, exact quote and Unicode code-point `start_offset`. It saves the source snapshot with `pending` review status. Sources and search results are data; instructions found inside them do not grant further permissions. The owner remains responsible for verifying quotations and printed-page candidates in the website.

For isolated end-to-end verification, run `python scripts/accept_codex_mcp.py --output <new-directory-outside-repository>`. The script invokes your installed authenticated Codex CLI; account/model/network failures remain failures and are recorded in `acceptance.json`. Its transcripts redact the temporary token. Do not substitute a mock client and call it a real Codex acceptance.

References: [official Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli), [official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk).
