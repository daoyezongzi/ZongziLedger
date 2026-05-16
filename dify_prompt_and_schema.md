# Dify Prompt and Schema

This project now uses `wetrace -> Dify workflow -> local ledger`.
UIA/Win32 capture is removed.

## Input payload (bridge/receiver)

```json
{
  "schema_version": "zongziledger-dify-clean-v1",
  "source_id": "wetrace",
  "timestamp": "2026-05-15 20:00:00",
  "messages": [
    {
      "message": "#记账26051501\n...\n结束",
      "raw_message": "#记账26051501\n...\n结束",
      "timestamp": "2026-05-15 19:59:58",
      "source_id": "wetrace:wxid_xxx",
      "message_hash": "sha1...",
      "message_captured_at": "2026-05-15 19:59:58",
      "normalize_hint": "structured_candidate|salvage_candidate|fallback_sample",
      "raw_context": {
        "source_type": "wetrace"
      }
    }
  ],
  "capture_meta": {
    "capture_mode": "wetrace",
    "capture_backend": "wetrace_api",
    "message_count": 1
  }
}
```

## Output payload (from Dify workflow)

```json
{
  "schema_version": "zongziledger-dify-clean-v1",
  "normalized_messages": [
    {
      "message": "#记账26051501\n...\n结束",
      "raw_message": "#记账26051501 ...",
      "timestamp": "2026-05-15 19:59:58",
      "source_id": "wetrace:wxid_xxx",
      "message_hash": "sha1...",
      "message_captured_at": "2026-05-15 19:59:58",
      "normalize_hint": "structured_candidate"
    }
  ],
  "unstructured_samples": [],
  "normalization_summary": {
    "input_messages_count": 1,
    "normalized_count": 1,
    "review_queue_count": 0
  }
}
```

## Prompt constraints

- Return JSON only.
- Keep schema version as `zongziledger-dify-clean-v1`.
- Put parseable ledger blocks into `normalized_messages`.
- Put non-parseable text into `unstructured_samples`.
