# AminiNews Brief Poller

This poller checks your Supabase Edge Function for the latest daily brief, downloads the markdown when a new brief appears, stores the markdown and metadata locally, and optionally hands the file to a NotebookLM automation command.

## Files

- `poll_daily_brief.py` - executable poller script
- `.env.example` - configuration template

## Setup

1. Copy `.env.example` to `.env` in the same directory.
2. Fill in `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `OPENCLAW_BRIDGE_TOKEN`, `AMININEWS_USER_ID`, and `BRIEF_OUTPUT_DIR`.
3. Optional: set `NOTEBOOKLM_COMMAND` if you already have a local NotebookLM automation entrypoint.
4. Recommended for reliable daily runs: configure `NOTEBOOKLM_AUTH_COMMAND` to a lightweight auth preflight command.
4. Make the script executable:

```bash
chmod +x poll_daily_brief.py
```

## Run

```bash
./poll_daily_brief.py
```

You can also point to a different env file:

```bash
./poll_daily_brief.py --env-file /path/to/brief-poller.env
```

## Behavior

- `404` from `get-latest-brief` logs `no brief yet` and exits `0`.
- `401` logs an auth failure and exits non-zero.
- New briefs are saved under `${BRIEF_OUTPUT_DIR}/${brief_date}/daily-brief.md`.
- Metadata is saved as `${BRIEF_OUTPUT_DIR}/${brief_date}/metadata.json`.
- Processed brief state is tracked in `${BRIEF_OUTPUT_DIR}/.brief-poller-state.json` by default.
- If `NOTEBOOKLM_COMMAND` is not set, the script logs a placeholder and exits successfully after saving files.
- If `NOTEBOOKLM_AUTH_COMMAND` is set, the script runs it before downloading and ingesting a new brief.
- If auth preflight fails, the script exits with code `3` and does not update state; next run can retry automatically.
- If NotebookLM automation fails, the files remain saved locally and the script prints a fallback message with the markdown path.
- If `NOTEBOOKLM_CLEANUP_OLD_CONTENT=true`, NotebookLM automation keeps only the newest AminiNews brief source and newest audio artifact, deleting older ones from NotebookLM.

## NotebookLM Hook

`NOTEBOOKLM_COMMAND` is optional. The command string supports these placeholders:

- `{markdown_path}`
- `{metadata_path}`
- `{brief_date}`
- `{checksum}`

Example:

```bash
NOTEBOOKLM_COMMAND="/usr/local/bin/notebooklm-ingest {markdown_path}"
```

For long-running automation, also configure auth preflight:

```bash
NOTEBOOKLM_REQUIRE_AUTH=true
NOTEBOOKLM_AUTH_COMMAND="/home/openclaw/.openclaw/skills/notebooklm/scripts/ingest_daily_brief.py --auth-only"
NOTEBOOKLM_CLEANUP_OLD_CONTENT=true
NOTEBOOKLM_AUDIO_LENGTH=default
```

When auth expires, re-auth once and rerun the saved brief command from logs:

```bash
/home/openclaw/.openclaw/skills/notebooklm/scripts/run_nlm.sh login --profile amininews
/home/openclaw/.openclaw/skills/notebooklm/scripts/ingest_daily_brief.py /home/openclaw/amininews-briefs/YYYY-MM-DD/daily-brief.md
```

## Cron

Example cron entry for `07:05` every morning:

```cron
5 7 * * * cd /home/openclaw/openclaw-dashboard/scripts/amininews-brief-poller && /usr/bin/env python3 ./poll_daily_brief.py >> /home/openclaw/amininews-brief-poller.log 2>&1
```

If you keep the `.env` file next to the script, no extra cron environment setup is needed.