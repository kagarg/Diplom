from fastapi import FastAPI, Body
from fastapi.responses import FileResponse
import os
import asyncio
from typing import Dict, Any, Optional
from datetime import datetime, timedelta, timezone
import socketio
from socketio import AsyncClient
from pydantic import BaseModel
# ----------------------- Настройки приложения -----------------------
app = FastAPI(
    title="Roboturnir Overlay API",
    description="API для управления оверлеем трансляции робототехнических соревнований",
    version="1.0.0",
    contact={"name": "Поддержка", "email": "support@roboturnir.com"},
    license_info={"name": "NSTU"},
)

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
socket_app = socketio.ASGIApp(sio, app)

EXTERNAL_WS_URL = "wss://grmvzdlx-3008.euw.devtunnels.ms"
OVERLAY_FILE = "overlay.html"
external_task = None
# ----------------------- Модели данных -----------------------
class MessageRequest(BaseModel):
    text: str
    duration: Optional[int] = 5  # по умолчанию 5 секунд

# ----------------------- Состояние таймера -----------------------
class TimerState:
    def __init__(self):
        self.base_data: dict = {}
        self.remaining_seconds: Optional[int] = None
        self.timer_task: Optional[asyncio.Task] = None
        self.paused: bool = False

    def reset(self):
        self.__init__()

timer_state = TimerState()

# ----------------------- Обработка данных -----------------------
def process_overlay_data(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "team1": raw_data.get("team1", "Команда 1"),
        "team2": raw_data.get("team2", "Команда 2"),
        "logo1": raw_data.get("logo1", ""),
        "logo2": raw_data.get("logo2", ""),
        "score1": raw_data.get("score1", 0),
        "score2": raw_data.get("score2", 0),
        "round": raw_data.get("round", 1),
        "time": raw_data.get("time")  # только для отображения
    }

# ----------------------- Таймер -----------------------
async def run_timer(seconds: int, base_data: dict):
    timer_state.paused = False
    start_time = datetime.now(timezone.utc)
    end_time = start_time + timedelta(seconds=seconds)

    while True:
        if timer_state.paused:
            timer_state.remaining_seconds = max(0, int((end_time - datetime.now(timezone.utc)).total_seconds()))
            print(f"[pause] Таймер приостановлен. Осталось: {timer_state.remaining_seconds} сек.")
            break

        now = datetime.now(timezone.utc)
        remaining = (end_time - now).total_seconds()
        if remaining <= 0:
            base_data["time"] = now.isoformat()
            await sio.emit("overlay_update", base_data)
            break

        base_data["time"] = (now + timedelta(seconds=remaining)).isoformat()
        await sio.emit("overlay_update", base_data)
        await asyncio.sleep(1)

async def start_phase_timer(phase: str, base_data: dict):
    if phase == "prepare":
        print("[timer] Старт подготовки: 3 минуты")
        await run_timer(180, base_data)

    elif phase == "fight":
        print("[timer] Отсчёт до боя: 3 секунды")
        await run_timer(3, base_data)
        print("[timer] Старт боя: 3 минуты")
        timer_state.timer_task = asyncio.create_task(run_timer(180, base_data))

# ----------------------- Прокси -----------------------
async def forward_external_to_clients():
    sio_client = AsyncClient(reconnection=True)

    async def handle_overlay_data(data):
        processed = process_overlay_data(data)
        timer_state.base_data = processed
        await sio.emit("overlay_update", processed)

    async def handle_preparing_start(data):
        print("[event] BUTTONS: Preparing start sent.")
        timer_state.base_data = process_overlay_data(data)
        asyncio.create_task(start_phase_timer("prepare", timer_state.base_data.copy()))

    async def handle_fight_start(data):
        print("[event] BUTTONS: Fight start.")
        timer_state.base_data = process_overlay_data(data)
        asyncio.create_task(start_phase_timer("fight", timer_state.base_data.copy()))

    async def handle_fight_pause(data):
        print("[event] FRONT-END: Fight pause.")
        if timer_state.timer_task:
            timer_state.paused = True
            await asyncio.sleep(0.1)  # Дать циклу run_timer выйти
            timer_state.timer_task.cancel()
            timer_state.timer_task = None

    async def handle_fight_resume(data):
        print("[event] BACK-END: Fight resume sent.")
        if timer_state.paused and timer_state.remaining_seconds:
            timer_state.paused = False
            timer_state.timer_task = asyncio.create_task(
                run_timer(timer_state.remaining_seconds, timer_state.base_data.copy())
            )
            timer_state.remaining_seconds = None

    sio_client.on("BACK-END: Overlay data sent.", handle_overlay_data)
    sio_client.on("BUTTONS: Preparing start sent.", handle_preparing_start)
    sio_client.on("BUTTONS: Fight start.", handle_fight_start)
    sio_client.on("FRONT-END: Fight pause.", handle_fight_pause)
    sio_client.on("BACK-END: Fight resume sent.", handle_fight_resume)
    

    @sio_client.event
    async def connect():
        print("[proxy] Подключено к внешнему Socket.IO серверу")

    @sio_client.event
    async def disconnect():
        print("[proxy] Отключено от внешнего сервера")

    while True:
        try:
            print("[proxy] Подключение к внешнему серверу...")
            await sio_client.connect(EXTERNAL_WS_URL, transports=["websocket"], socketio_path="/socket.io")
            await sio_client.wait()
        except Exception as e:
            print(f"[proxy] Ошибка: {e}. Повтор через 5 секунд...")
            await asyncio.sleep(5)
            try:
                await sio_client.disconnect()
            except:
                pass

# ----------------------- WebSocket события клиента -----------------------
@sio.event
async def connect(sid, environ):
    print(f"[sio] Клиент подключён: {sid}")
    global external_task
    if not external_task:
        external_task = asyncio.create_task(forward_external_to_clients())
    if timer_state.base_data:
        await sio.emit("overlay_update", timer_state.base_data, to=sid)

@sio.event
async def disconnect(sid):
    print(f"[sio] Клиент отключён: {sid}")

# ----------------------- HTTP маршрут -----------------------
@app.get(
    "/overlay",
    summary="Получить HTML-оверлей",
    description="Возвращает готовый HTML-файл с интерактивным оверлеем для OBS",
    response_description="HTML-страница оверлея",
    tags=["Основные элементы"],
    responses={
        200: {"content": {"text/html": {}}},
        404: {"description": "Файл оверлея не найден"}
    }
)
@app.post("/api/show_message", tags=["Пользовательский текст"])
async def show_custom_message(message: MessageRequest):
    await sio.emit("custom_message", {
        "text": message.text,
        "duration": message.duration
    })
    return {"status": "ok", "sent": message.model_dump()}

async def get_overlay():
    if not os.path.exists(OVERLAY_FILE):
        return {"error": "Overlay file not found"}, 404
    return FileResponse(OVERLAY_FILE)

# ----------------------- Точка входа -----------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(socket_app, host="127.0.0.1", port=8000)
