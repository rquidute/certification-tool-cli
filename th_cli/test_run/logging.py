#
# Copyright (c) 2023 Project CHIP Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import datetime
import os
from typing import Optional

from loguru import logger

from th_cli.config import config

# Add custom logger for "chip-tool"
CHIPTOOL_LEVEL = "CHIPTOOL"
logger.level(CHIPTOOL_LEVEL, no=21, icon="🤖", color="<cyan>")

# Add custom logger for python tests
PYTHON_TEST_LEVEL = "PYTHON_TEST"
logger.level(PYTHON_TEST_LEVEL, no=22, icon="🐍", color="<cyan>")

# Global reference to log stream handler (if enabled)
_log_stream_handler: Optional["LogStreamHandler"] = None


def configure_logger_for_run(title: str, enable_log_streaming: bool = False) -> str:
    """Configure logger for a test run.

    Args:
        title: Title of the test run
        enable_log_streaming: Whether to enable real-time log streaming

    Returns:
        Path to the log file
    """
    global _log_stream_handler

    # Reset (Remove all sinks from logger)
    logger.remove()

    timestamp = datetime.datetime.now().isoformat(timespec="seconds").replace(":", "-")
    log_path = os.path.join(
        config.log_config.output_log_path,
        f"test_run_{title}_{timestamp}.log",
    )

    logger.add(log_path, enqueue=True, format=config.log_config.format, mode="w")

    # Add streaming sink if enabled
    if enable_log_streaming:
        try:
            from th_cli.test_run.log_stream_handler import LogStreamHandler

            _log_stream_handler = LogStreamHandler(port=8998)
            viewer_url = _log_stream_handler.start(test_run_title=title)
            # Add custom sink that forwards logs to the stream handler
            def stream_sink(message):
                """Custom sink that forwards logs to the HTTP stream."""
                try:
                    record = message.record
                    _log_stream_handler.add_log_entry(
                        message=record["message"], level=record["level"].name, timestamp=record["time"].isoformat()
                    )
                except Exception:
                    # Silently fail to avoid disrupting logging
                    pass

            # enqueue=True: run stream_sink on loguru's dedicated worker
            # thread rather than synchronously on the caller. Without it,
            # this sink runs inline on whichever thread logged the message -
            # including the SSE-server thread when it logs its own send
            # progress (logs_http_server.py), which then re-enters this same
            # sink/queue from inside itself.
            logger.add(stream_sink, format="{message}", enqueue=True, catch=True)
            logger.info(f"Real-time log streaming enabled: {viewer_url}")

        except Exception as e:
            logger.warning(f"Failed to enable log streaming: {e}")
            _log_stream_handler = None

    return log_path


def stop_log_streaming():
    """Stop the log streaming server if it's running."""
    global _log_stream_handler

    if _log_stream_handler:
        try:
            # stream_sink runs via enqueue=True, on loguru's worker thread.
            # complete() blocks until everything already queued has been
            # delivered, so the last log lines aren't dropped by stopping
            # the handler (which makes add_log_entry() a no-op) while some
            # are still in flight.
            logger.complete()
            _log_stream_handler.stop()
            logger.info("Log streaming stopped")
        except Exception as e:
            logger.warning(f"Error stopping log streaming: {e}")
        finally:
            _log_stream_handler = None


def set_download_run_id(run_id: int) -> None:
    """Tell the log viewer which run's log to link the "Download Logs" button
    to, once the run has been created and its id is known (the log-streaming
    server starts before the run exists, so this can't be known up front).
    """
    if _log_stream_handler:
        _log_stream_handler.set_run_id(run_id)


def get_log_stream_url() -> Optional[str]:
    """Get the URL for the log viewer if streaming is enabled.

    Returns:
        URL string or None if streaming is not enabled
    """
    if _log_stream_handler and _log_stream_handler.is_running:
        return _log_stream_handler._get_log_viewer_url()
    return None


def get_log_stream_handler() -> Optional["LogStreamHandler"]:
    """Return the active LogStreamHandler, or None if streaming is disabled."""
    if _log_stream_handler and _log_stream_handler.is_running:
        return _log_stream_handler
    return None


async def append_backend_log_to_run_log(run_id: int) -> None:
    """Append the run's log, read back from the backend DB, to the CLI log file.

    Used when the CLI asked the backend not to send log records over the
    websocket (--no-streaming): the test output then never passed through this
    process's logger, so it is fetched once, after the run, instead.

    Written through the logger's own sink (raw mode, no per-line formatting):
    that sink owns the file's write position, so a second handle appending to
    the same file would be overwritten by it on the next log call.
    """
    from httpx import Timeout

    from th_cli.api_lib_autogen.api_client import AsyncApis
    from th_cli.client import get_client

    client = get_client(timeout=Timeout(120.0, connect=10.0))
    try:
        content = await AsyncApis(client).test_run_executions_api.download_log_api_v1_test_run_executions__id__log_get(
            id=run_id, json_entries=False, download=False
        )
    finally:
        await client.aclose()

    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    if not content:
        logger.warning(f"No log content returned by the backend for test run execution {run_id}")
        return
    logger.opt(raw=True).info(f"\n---- Test run log from backend (test run execution {run_id}) ----\n{content}\n")
