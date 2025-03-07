import asyncio
import mimetypes
from contextlib import asynccontextmanager
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Union
import typing
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState
from streamsync.ss_types import (AppProcessServerResponse, ComponentUpdateRequestPayload, EventResponsePayload, InitRequestBody, InitResponseBodyEdit,
                                 InitResponseBodyRun, InitSessionRequestPayload, InitSessionResponsePayload, ServeMode, StateEnquiryResponsePayload, StreamsyncEvent, StreamsyncWebsocketIncoming, StreamsyncWebsocketOutgoing)
import os
import uvicorn
from streamsync.app_runner import AppRunner
from urllib.parse import urlsplit
import logging
import pathlib
from streamsync import VERSION

MAX_WEBSOCKET_MESSAGE_SIZE = 201*1024*1024
logging.getLogger().setLevel(logging.INFO)


def get_asgi_app(
        user_app_path: str,
        serve_mode: ServeMode,
        enable_remote_edit: bool = False,
        on_load: Optional[Callable] = None,
        on_shutdown: Optional[Callable] = None) -> FastAPI:
    if serve_mode not in ["run", "edit"]:
        raise ValueError("""Invalid mode. Must be either "run" or "edit".""")

    _fix_mimetype()
    app_runner = AppRunner(user_app_path, serve_mode)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal app_runner

        app_runner.hook_to_running_event_loop()
        app_runner.load()

        if on_load is not None:
            on_load()

        try:
            yield
        except asyncio.CancelledError:
            pass

        app_runner.shut_down()
        if on_shutdown is not None:
            on_shutdown()

    asgi_app = FastAPI(lifespan=lifespan)

    def _get_extension_paths() -> List[str]:
        extensions_path = pathlib.Path(user_app_path) / "extensions"
        if not extensions_path.exists():
            return []
        filtered_files = [f for f in extensions_path.rglob(
            "*") if f.suffix.lower() in (".js", ".css") and f.is_file()]
        relative_paths = [f.relative_to(
            extensions_path).as_posix() for f in filtered_files]
        return relative_paths

    cached_extension_paths = _get_extension_paths()

    def _check_origin_header(origin_header: Optional[str]) -> bool:
        if serve_mode not in ("edit") or enable_remote_edit is True:
            return True
        if origin_header is None:
            return False
        hostname = urlsplit(origin_header).hostname
        if hostname in ("127.0.0.1", "localhost"):
            return True
        return False

    # Init

    def _get_run_starter_pack(payload: InitSessionResponsePayload):
        return InitResponseBodyRun(
            mode="run",
            sessionId=payload.sessionId,
            userState=payload.userState,
            mail=payload.mail,
            components=payload.components,
            userFunctions=payload.userFunctions,
            extensionPaths=cached_extension_paths
        )

    def _get_edit_starter_pack(payload: InitSessionResponsePayload):
        run_code: Optional[str] = app_runner.run_code

        return InitResponseBodyEdit(
            mode="edit",
            sessionId=payload.sessionId,
            userState=payload.userState,
            mail=payload.mail,
            components=payload.components,
            userFunctions=payload.userFunctions,
            runCode=run_code,
            extensionPaths=cached_extension_paths
        )

    @asgi_app.post("/api/init")
    async def init(initBody: InitRequestBody, request: Request) -> Union[InitResponseBodyRun, InitResponseBodyEdit]:

        """
        Handles session init and provides a "starter pack" to the frontend.
        """

        origin_header = request.headers.get("origin")
        if not _check_origin_header(origin_header):
            wrong_origin_message = "A session request with origin %s was rejected. For security reasons, only local origins are allowed in edit mode. "
            wrong_origin_message += "To circumvent this protection, use the --enable-remote-edit flag if running via command line."
            logging.error(wrong_origin_message, origin_header)
            raise HTTPException(
                status_code=403, detail="Incorrect origin. Only local origins are allowed.")

        response = await app_runner.init_session(InitSessionRequestPayload(
            cookies=dict(request.cookies),
            headers=dict(request.headers),
            proposedSessionId=initBody.proposedSessionId
        ))
        status = response.status

        if status == "error" or response.payload is None:
            raise HTTPException(status_code=403, detail="Session rejected.")

        if serve_mode == "run":
            return _get_run_starter_pack(response.payload)

        if serve_mode == "edit":
            return _get_edit_starter_pack(response.payload)

    # Streaming

    async def _stream_session_init(websocket: WebSocket):

        """
        Waits for the client to provide a session id to initialise the stream.
        Returns the session id received.
        """

        session_id = None
        while session_id is None:
            req_message_raw = await websocket.receive_json()

            try:
                req_message = StreamsyncWebsocketIncoming.model_validate(
                    req_message_raw)
            except ValidationError:
                logging.error("Incorrect incoming request.")
                return

            if req_message.type == "streamInit" and req_message.payload is not None:
                session_id = req_message.payload.get("sessionId")
        return session_id

    async def _stream_incoming_requests(websocket: WebSocket, session_id: str):

        """
        Handles incoming requests from client. 
        """

        pending_tasks: Set[asyncio.Task] = set()

        try:
            while True:
                req_message_raw = await websocket.receive_json()

                try:
                    req_message = StreamsyncWebsocketIncoming.model_validate(
                        req_message_raw)
                except ValidationError:
                    logging.error("Incorrect incoming request.")
                    break

                is_session_ok = await app_runner.check_session(session_id)
                if not is_session_ok:
                    break

                new_task = None

                if req_message.type == "event":
                    new_task = asyncio.create_task(
                        _handle_incoming_event(websocket, session_id, req_message))
                elif req_message.type == "keepAlive":
                    new_task = asyncio.create_task(
                        _handle_keep_alive_message(websocket, session_id, req_message))
                elif req_message.type == "stateEnquiry":
                    new_task = asyncio.create_task(
                        _handle_state_enquiry_message(websocket, session_id, req_message))
                elif serve_mode == "edit":
                    new_task = asyncio.create_task(
                        _handle_incoming_edit_message(websocket, session_id, req_message))

                if new_task:
                    pending_tasks.add(new_task)
                    new_task.add_done_callback(pending_tasks.discard)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        finally:
            # Cancel pending tasks

            for pending_task in pending_tasks.copy():
                pending_task.cancel()
                try:
                    await pending_task
                except asyncio.CancelledError:
                    pass

    async def _handle_incoming_event(websocket: WebSocket, session_id: str, req_message: StreamsyncWebsocketIncoming):
        response = StreamsyncWebsocketOutgoing(
            messageType=f"{req_message.type}Response",
            trackingId=req_message.trackingId,
            payload=None
        )
        res_payload: Optional[Dict[str, Any]] = None
        apsr: Optional[AppProcessServerResponse] = None
        apsr = await app_runner.handle_event(
            session_id, StreamsyncEvent(
                type=req_message.payload["type"],
                instancePath=req_message.payload["instancePath"],
                payload=req_message.payload["payload"]
            ))
        if apsr is not None and apsr.payload is not None:
            res_payload = typing.cast(
                EventResponsePayload, apsr.payload).model_dump()
        if res_payload is not None:
            response.payload = res_payload
        await websocket.send_json(response.model_dump())

    async def _handle_incoming_edit_message(websocket: WebSocket, session_id: str, req_message: StreamsyncWebsocketIncoming):
        response = StreamsyncWebsocketOutgoing(
            messageType=f"{req_message.type}Response",
            trackingId=req_message.trackingId,
            payload=None
        )
        if req_message.type == "componentUpdate":
            await app_runner.update_components(
                session_id, ComponentUpdateRequestPayload(
                    components=req_message.payload["components"]
                ))
        elif req_message.type == "codeSaveRequest":
            app_runner.save_code(
                session_id, req_message.payload["code"])
        elif req_message.type == "codeUpdate":
            app_runner.update_code(
                session_id, req_message.payload["code"])
        await websocket.send_json(response.model_dump())

    async def _handle_keep_alive_message(websocket: WebSocket, session_id: str, req_message: StreamsyncWebsocketIncoming):
        response = StreamsyncWebsocketOutgoing(
            messageType=f"keepAliveResponse",
            trackingId=req_message.trackingId,
            payload=None
        )
        await websocket.send_json(response.model_dump())

    async def _handle_state_enquiry_message(websocket: WebSocket, session_id: str, req_message: StreamsyncWebsocketIncoming):
        response = StreamsyncWebsocketOutgoing(
            messageType=f"{req_message.type}Response",
            trackingId=req_message.trackingId,
            payload=None
        )
        res_payload: Optional[Dict[str, Any]] = None
        apsr: Optional[AppProcessServerResponse] = None
        apsr = await app_runner.handle_state_enquiry(session_id)
        if apsr is not None and apsr.payload is not None:
            res_payload = typing.cast(
                StateEnquiryResponsePayload, apsr.payload).model_dump()
        if res_payload is not None:
            response.payload = res_payload
        await websocket.send_json(response.model_dump())

    async def _stream_outgoing_announcements(websocket: WebSocket):

        """
        Handles outgoing communications to client (announcements).
        """

        if app_runner.code_update_condition is None:
            raise ValueError("Code update condition not set.")

        await app_runner.code_update_condition.acquire()
        try:
            await app_runner.code_update_condition.wait()
        finally:
            app_runner.code_update_condition.release()

        announcement = StreamsyncWebsocketOutgoing(
            messageType="announcement",
            trackingId=-1,
            payload={
                "announce": "codeUpdate"
            }
        )

        if websocket.application_state == WebSocketState.DISCONNECTED:
            return

        try:
            await websocket.send_json(announcement.dict())
        except (WebSocketDisconnect):
            pass

    @asgi_app.websocket("/api/stream")
    async def stream(websocket: WebSocket):

        """ Initialises incoming and outgoing communications on the stream. """

        await websocket.accept()

        origin_header = websocket.headers.get("origin")
        if not _check_origin_header(origin_header):
            await websocket.close(code=1008)
            return

        try:
            session_id = await _stream_session_init(websocket)
        except WebSocketDisconnect:
            return

        is_session_ok = await app_runner.check_session(session_id)
        if not is_session_ok:
            await websocket.close(code=1008)  # Invalid permissions
            return

        task1 = asyncio.create_task(
            _stream_incoming_requests(websocket, session_id))
        task2 = asyncio.create_task(_stream_outgoing_announcements(websocket))

        try:
            await asyncio.wait((task1, task2), return_when=asyncio.FIRST_COMPLETED)
            await asyncio.sleep(1)
            task1.cancel()
            task2.cancel()
            await task1
            await task2
        except asyncio.CancelledError:
            pass

    # Mount static paths

    user_app_static_path = pathlib.Path(user_app_path) / "static"
    if user_app_static_path.exists():
        asgi_app.mount(
            "/static", StaticFiles(directory=str(user_app_static_path)), name="user_static")

    user_app_extensions_path = pathlib.Path(user_app_path) / "extensions"
    if user_app_extensions_path.exists():
        asgi_app.mount(
            "/extensions", StaticFiles(directory=str(user_app_extensions_path)), name="extensions")

    server_path = os.path.dirname(__file__)
    server_static_path = pathlib.Path(server_path) / "static"
    asgi_app.mount(
        "/", StaticFiles(directory=str(server_static_path), html=True), name="server_static")

    # Return

    return asgi_app


def print_init_message():
    GREEN_TOKEN = "\033[92m"
    END_TOKEN = "\033[0m"

    print(f"""{ GREEN_TOKEN }
     _                                     
 ___| |_ ___ ___ ___ _____ ___ _ _ ___ ___ 
|_ -|  _|  _| -_| .'|     |_ -| | |   |  _|
|___|_| |_| |___|__,|_|_|_|___|_  |_|_|___|  v{VERSION}
                              |___|    
{END_TOKEN}""")


def print_route_message(run_name: str, port: int, host: str):
    GREEN_TOKEN = "\033[92m"
    END_TOKEN = "\033[0m"

    print(f"{run_name} is available at:{END_TOKEN}{GREEN_TOKEN} http://{host}:{port}{END_TOKEN}")


def serve(app_path: str, mode: ServeMode, port, host, enable_remote_edit=False):
    """ Initialises the web server. """

    print_init_message()

    def on_load():
        run_name = "Builder" if mode == "edit" else "App"
        print_route_message(run_name, port, host)

    asgi_app = get_asgi_app(
        app_path, mode, enable_remote_edit, on_load)
    log_level = "warning"

    uvicorn.run(asgi_app, host=host,
                port=port, log_level=log_level, ws_max_size=MAX_WEBSOCKET_MESSAGE_SIZE)


def _fix_mimetype():
    """
    Fixes mimetypes for .js files. This is needed for the webserver to serve .js files correctly.
    """
    js_mimetype = mimetypes.guess_type("myfile.js")[0]
    if js_mimetype[0] != "text/javascript":
        mimetypes.add_type("text/javascript", ".js")
