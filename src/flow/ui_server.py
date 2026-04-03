"""Local HTTP API for the graph UI."""

from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

from .paths import logs_dir
from .store import connect, daemon_status, enqueue_command, get_agent, init_db
from .ui_data import build_focus_snapshot, build_overview_snapshot


class MoveBody(BaseModel):
    state: str


@dataclass
class UIServerHandle:
    host: str
    port: int
    _server: uvicorn.Server
    _thread: threading.Thread

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def close(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=timeout)


def create_ui_app() -> FastAPI:
    app = FastAPI(title="flow-ui", docs_url=None, redoc_url=None)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/flows/{flow_name}")
    def flow_overview(flow_name: str) -> dict[str, Any]:
        with contextlib.closing(connect()) as conn:
            init_db(conn)
            try:
                return build_overview_snapshot(conn, flow_name)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/flows/{flow_name}/agents/{agent_id}")
    def flow_focus(flow_name: str, agent_id: int) -> dict[str, Any]:
        with contextlib.closing(connect()) as conn:
            init_db(conn)
            try:
                return build_focus_snapshot(conn, flow_name, agent_id)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/agents/{agent_id}/pause")
    def pause_agent(agent_id: int) -> dict[str, Any]:
        return _queue_command(agent_id, "pause", {})

    @app.post("/api/agents/{agent_id}/interrupt")
    def interrupt_agent(agent_id: int) -> dict[str, Any]:
        return _queue_command(agent_id, "interrupt", {})

    @app.post("/api/agents/{agent_id}/resume")
    def resume_agent(agent_id: int) -> dict[str, Any]:
        return _queue_command(agent_id, "resume", {})

    @app.post("/api/agents/{agent_id}/wake")
    def wake_agent(agent_id: int) -> dict[str, Any]:
        return _queue_command(agent_id, "wake", {})

    @app.post("/api/agents/{agent_id}/stop")
    def stop_agent(agent_id: int) -> dict[str, Any]:
        return _queue_command(agent_id, "stop", {})

    @app.post("/api/agents/{agent_id}/move")
    def move_agent(agent_id: int, body: MoveBody) -> dict[str, Any]:
        return _queue_command(agent_id, "move", {"state": body.state})

    return app


def start_ui_server(*, host: str = "127.0.0.1", port: int = 0) -> UIServerHandle:
    selected_port = port or _find_open_port()
    app = create_ui_app()
    config = uvicorn.Config(app, host=host, port=selected_port, log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="flow-ui-server", daemon=True)
    thread.start()
    _wait_for_server(host, selected_port, thread)
    return UIServerHandle(host=host, port=selected_port, _server=server, _thread=thread)


def _queue_command(agent_id: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    with contextlib.closing(connect()) as conn:
        init_db(conn)
        if get_agent(conn, agent_id) is None:
            raise HTTPException(status_code=404, detail=f"unknown agent {agent_id}")
        if not _ensure_daemon(conn):
            raise HTTPException(status_code=503, detail="failed to start runtime daemon")
        command_id = enqueue_command(conn, agent_id, kind, payload)
        conn.commit()
        error = _wait_for_command(conn, command_id)
        if error:
            raise HTTPException(status_code=409, detail=error)
        current = get_agent(conn, agent_id)
        return {
            "ok": True,
            "agent_id": agent_id,
            "command": kind,
            "agent": dict(current) if current is not None else None,
        }


def _ensure_daemon(conn: Any) -> bool:
    status = daemon_status(conn)
    if status["active"] == "1":
        return True
    if shutil.which(sys.executable) is None:
        return False
    log_path = logs_dir() / "daemon.log"
    with log_path.open("a", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "flow.cli", "_daemon", "--foreground"],
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    deadline = time.time() + 5.0
    while time.time() < deadline:
        time.sleep(0.1)
        status = daemon_status(conn)
        if status["active"] == "1":
            return True
        if process.poll() is not None:
            break
    return False


def _wait_for_command(conn: Any, command_id: int, timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = conn.execute("SELECT processed_at, error_text FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            return ""
        if row["processed_at"]:
            return str(row["error_text"] or "")
        time.sleep(0.1)
    return "timed out waiting for command processing"


def _find_open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_server(host: str, port: int, thread: threading.Thread, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not thread.is_alive():
            raise RuntimeError("flow UI server exited before becoming ready")
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("timed out waiting for flow UI server")
