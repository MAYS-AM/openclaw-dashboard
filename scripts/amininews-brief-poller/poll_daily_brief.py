#!/usr/bin/env python3

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request


DEFAULT_ENV_FILE = ".env"
DEFAULT_MARKDOWN_FILENAME = "daily-brief.md"
DEFAULT_METADATA_FILENAME = "metadata.json"
DEFAULT_STATE_FILENAME = ".brief-poller-state.json"
DEFAULT_TIMEOUT_SECONDS = 30


class PollerError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll the latest AminiNews brief and optionally hand it to NotebookLM automation."
    )
    parser.add_argument(
        "--env-file",
        default=str(Path(__file__).with_name(DEFAULT_ENV_FILE)),
        help="Path to the .env file. Existing process environment variables take precedence.",
    )
    return parser.parse_args()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def load_dotenv(env_file: Path) -> None:
    if not env_file.exists():
        return

    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            logging.warning("Skipping malformed .env line: %s", raw_line)
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if value and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]

        current = os.environ.get(key)
        if current is None or not current.strip():
            os.environ[key] = value


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PollerError(f"Missing required environment variable: {name}", exit_code=2)
    return value


def read_config() -> dict[str, Any]:
    output_dir = Path(require_env("BRIEF_OUTPUT_DIR")).expanduser()
    state_file = Path(
        os.environ.get("BRIEF_STATE_FILE", str(output_dir / DEFAULT_STATE_FILENAME))
    ).expanduser()

    timeout_raw = os.environ.get("BRIEF_REQUEST_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    try:
        timeout_seconds = int(timeout_raw)
    except ValueError as exc:
        raise PollerError(
            "BRIEF_REQUEST_TIMEOUT_SECONDS must be an integer",
            exit_code=2,
        ) from exc

    return {
        "supabase_url": require_env("SUPABASE_URL").rstrip("/"),
        "supabase_publishable_key": require_env("SUPABASE_PUBLISHABLE_KEY"),
        "openclaw_bridge_token": require_env("OPENCLAW_BRIDGE_TOKEN"),
        "amininews_user_id": require_env("AMININEWS_USER_ID"),
        "brief_output_dir": output_dir,
        "state_file": state_file,
        "timeout_seconds": timeout_seconds,
        "notebooklm_command": os.environ.get("NOTEBOOKLM_COMMAND", "").strip(),
        "notebooklm_profile": os.environ.get("NOTEBOOKLM_PROFILE", "default").strip() or "default",
        "notebooklm_auth_command": os.environ.get("NOTEBOOKLM_AUTH_COMMAND", "").strip(),
        "notebooklm_require_auth": env_bool("NOTEBOOKLM_REQUIRE_AUTH", True),
    }


def load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {}

    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PollerError(f"State file is not valid JSON: {state_file}", exit_code=2) from exc


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temp_path = Path(handle.name)

    temp_path.replace(path)


def build_latest_brief_request(config: dict[str, Any]) -> request.Request:
    query = parse.urlencode({"userId": config["amininews_user_id"]})
    url = f"{config['supabase_url']}/functions/v1/get-latest-brief?{query}"
    headers = {
        "apikey": config["supabase_publishable_key"],
        "Authorization": f"Bearer {config['supabase_publishable_key']}",
        "x-bridge-token": config["openclaw_bridge_token"],
    }
    return request.Request(url, headers=headers, method="GET")


def fetch_latest_brief_metadata(config: dict[str, Any]) -> dict[str, Any] | None:
    req = build_latest_brief_request(config)
    try:
        with request.urlopen(req, timeout=config["timeout_seconds"]) as response:
            raw_body = response.read().decode("utf-8")
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError as exc:
                raise PollerError("Latest brief endpoint returned invalid JSON", exit_code=1) from exc

            if not isinstance(payload, dict):
                raise PollerError("Latest brief endpoint returned an unexpected payload", exit_code=1)
            return payload
    except error.HTTPError as exc:
        if exc.code == 404:
            logging.info("no brief yet")
            return None
        if exc.code == 401:
            raise PollerError("auth failure while requesting latest brief", exit_code=1) from exc

        body = exc.read().decode("utf-8", errors="replace")
        raise PollerError(
            f"Latest brief request failed with HTTP {exc.code}: {body}",
            exit_code=1,
        ) from exc
    except error.URLError as exc:
        raise PollerError(f"Network error while requesting latest brief: {exc}", exit_code=1) from exc


def brief_is_already_processed(state: dict[str, Any], metadata: dict[str, Any]) -> bool:
    last_checksum = str(state.get("last_checksum", "")).strip()
    last_brief_date = str(state.get("last_brief_date", "")).strip()
    checksum = str(metadata.get("checksum", "")).strip()
    brief_date = str(metadata.get("brief_date", "")).strip()

    if checksum and checksum == last_checksum:
        return True
    if brief_date and brief_date == last_brief_date:
        return True
    return False


def validate_latest_brief_metadata(metadata: dict[str, Any]) -> None:
    required_keys = [
        "brief_date",
        "storage_path",
        "topic_count",
        "checksum",
        "signed_url",
        "expires_in_seconds",
    ]
    missing = [key for key in required_keys if key not in metadata or metadata[key] in (None, "")]
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise PollerError(f"Latest brief payload missing required fields: {missing_text}", exit_code=1)


def download_markdown(signed_url: str, timeout_seconds: int) -> str:
    req = request.Request(signed_url, method="GET")
    try:
        with request.urlopen(req, timeout=timeout_seconds) as response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read().decode("utf-8")
            if "text/markdown" not in content_type and "text/plain" not in content_type:
                logging.info("Downloaded brief with content type: %s", content_type or "unknown")
            return body
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PollerError(
            f"Signed URL download failed with HTTP {exc.code}: {body}",
            exit_code=1,
        ) from exc
    except error.URLError as exc:
        raise PollerError(f"Network error while downloading markdown: {exc}", exit_code=1) from exc


def save_brief_files(
    config: dict[str, Any], metadata: dict[str, Any], markdown_content: str
) -> tuple[Path, Path]:
    brief_dir = config["brief_output_dir"] / str(metadata["brief_date"])
    brief_dir.mkdir(parents=True, exist_ok=True)

    markdown_path = brief_dir / DEFAULT_MARKDOWN_FILENAME
    metadata_path = brief_dir / DEFAULT_METADATA_FILENAME

    markdown_path.write_text(markdown_content, encoding="utf-8")
    metadata_payload = {
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "brief": metadata,
    }
    write_json_atomic(metadata_path, metadata_payload)
    return markdown_path, metadata_path


def run_notebooklm_integration(
    config: dict[str, Any],
    markdown_path: Path,
    metadata_path: Path,
    metadata: dict[str, Any],
) -> bool:
    command_template = config["notebooklm_command"]
    if not command_template:
        logging.info(
            "NotebookLM integration not configured; brief saved locally at %s",
            markdown_path,
        )
        return True

    try:
        command = command_template.format(
            markdown_path=str(markdown_path),
            metadata_path=str(metadata_path),
            brief_date=str(metadata["brief_date"]),
            checksum=str(metadata["checksum"]),
        )
    except KeyError as exc:
        logging.error("NotebookLM command template uses an unknown placeholder: %s", exc)
        return False
    except ValueError as exc:
        logging.error("NotebookLM command template is malformed: %s", exc)
        return False

    logging.info("Running NotebookLM integration command")
    try:
        args = shlex.split(command)
        if not args:
            logging.error("NotebookLM command template produced an empty command")
            return False
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        logging.error("NotebookLM integration failed to start: %s", exc)
        return False

    if completed.stdout.strip():
        logging.info("NotebookLM stdout: %s", completed.stdout.strip())
    if completed.stderr.strip():
        logging.warning("NotebookLM stderr: %s", completed.stderr.strip())

    if completed.returncode != 0:
        combined = f"{completed.stdout}\n{completed.stderr}".lower()
        if "not authenticated" in combined or "cookies have expired" in combined:
            retry_command = command_template.format(
                markdown_path=str(markdown_path),
                metadata_path=str(metadata_path),
                brief_date=str(metadata["brief_date"]),
                checksum=str(metadata["checksum"]),
            )
            logging.error(
                "NotebookLM authentication appears invalid or expired for profile '%s'. "
                "Re-authenticate and retry with: %s",
                config.get("notebooklm_profile", "default"),
                retry_command,
            )
        logging.error("NotebookLM integration exited with status %s", completed.returncode)
        return False

    logging.info("NotebookLM integration completed successfully")
    return True


def update_state(state_file: Path, metadata: dict[str, Any], markdown_path: Path) -> None:
    payload = {
        "last_brief_date": metadata.get("brief_date"),
        "last_checksum": metadata.get("checksum"),
        "last_storage_path": metadata.get("storage_path"),
        "last_saved_markdown": str(markdown_path),
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json_atomic(state_file, payload)


def run_notebooklm_auth_preflight(config: dict[str, Any]) -> bool:
    command_template = config.get("notebooklm_command", "")
    if not command_template:
        return True
    if not config.get("notebooklm_require_auth", True):
        return True

    auth_command = str(config.get("notebooklm_auth_command", "")).strip()
    if not auth_command:
        return True

    logging.info("Running NotebookLM auth preflight command")
    try:
        args = shlex.split(auth_command)
        if not args:
            logging.error("NotebookLM auth preflight command produced an empty command")
            return False
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        logging.error("NotebookLM auth preflight failed to start: %s", exc)
        return False

    if completed.stdout.strip():
        logging.info("NotebookLM auth preflight stdout: %s", completed.stdout.strip())
    if completed.stderr.strip():
        logging.warning("NotebookLM auth preflight stderr: %s", completed.stderr.strip())

    if completed.returncode != 0:
        logging.error("NotebookLM auth preflight failed with status %s", completed.returncode)
        return False

    logging.info("NotebookLM auth preflight succeeded")
    return True


def main() -> int:
    args = parse_args()
    setup_logging()
    load_dotenv(Path(args.env_file).expanduser())
    config = read_config()

    metadata = fetch_latest_brief_metadata(config)
    if metadata is None:
        return 0

    validate_latest_brief_metadata(metadata)
    state = load_state(config["state_file"])
    if brief_is_already_processed(state, metadata):
        logging.info(
            "Brief already processed for date=%s checksum=%s",
            metadata.get("brief_date"),
            metadata.get("checksum"),
        )
        return 0

    if not run_notebooklm_auth_preflight(config):
        logging.error("NotebookLM auth preflight failed. Skipping integration until authentication is healthy.")
        return 3

    markdown_content = download_markdown(metadata["signed_url"], config["timeout_seconds"])
    markdown_path, metadata_path = save_brief_files(config, metadata, markdown_content)
    logging.info("Saved brief to %s", markdown_path)
    logging.info("Saved metadata to %s", metadata_path)

    notebooklm_ok = run_notebooklm_integration(config, markdown_path, metadata_path, metadata)
    if not notebooklm_ok:
        logging.error(
            "NotebookLM automation failed. Fallback: process the saved brief manually at %s",
            markdown_path,
        )
        return 3

    update_state(config["state_file"], metadata, markdown_path)
    logging.info(
        "Processed brief successfully for date=%s checksum=%s",
        metadata.get("brief_date"),
        metadata.get("checksum"),
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PollerError as exc:
        logging.error(str(exc))
        sys.exit(exc.exit_code)
    except KeyboardInterrupt:
        logging.error("Interrupted")
        sys.exit(130)