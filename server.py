from fastapi import FastAPI, Body,HTTPException
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
async def run_timer(seconds: int):
    try:
        timer_state.paused = False
        start_time = datetime.now(timezone.utc)
        end_time = start_time + timedelta(seconds=seconds)

        while True:
            if timer_state.paused:
                timer_state.remaining_seconds = max(0, int((end_time - datetime.now(timezone.utc)).total_seconds()))
                print(f"[pause] Таймер приостановлен. Осталось: {timer_state.remaining_seconds} сек.")
                break

            now = datetime.now(timezone.utc)
            remaining = int((end_time - now).total_seconds())

            if remaining <= 0:
                timer_state.base_data["remaining"] = 0
                await sio.emit("overlay_update", timer_state.base_data)
                break

            timer_state.base_data["remaining"] = remaining
            await sio.emit("overlay_update", timer_state.base_data)
            await asyncio.sleep(1)
    except Exception as e:
        print(f"[timer] Ошибка в run_timer: {e}")

async def start_phase_timer(phase: str):
    if not timer_state.base_data:
        print("[timer] Ошибка: base_data не установлен — данные о командах не получены.")
        return
    if phase == "prepare":
        print("[timer] Старт подготовки: 3 минуты")
        await run_timer(180)

    elif phase == "fight":
        print("[timer] Отсчёт до боя: 3 секунды")
        await run_timer(3)
        print("[timer] Старт боя: 3 минуты")
        timer_state.timer_task = asyncio.create_task(run_timer(180))

# ----------------------- Прокси -----------------------
async def forward_external_to_clients():
    sio_client = AsyncClient(reconnection=True)

    async def handle_overlay_data(data):
        processed = process_overlay_data(data)
        processed.pop("time", None)
        timer_state.base_data = processed
        await sio.emit("overlay_update", processed)

    async def safe_start_phase_timer(phase: str):
        try:
            await start_phase_timer(phase)
        except Exception as e:
            print(f"[error] Ошибка при запуске таймера для фазы '{phase}': {e}")

    async def handle_preparing_start(data):
        print("[event] Preparing start sent.")
        asyncio.create_task(safe_start_phase_timer("prepare"))

    async def handle_fight_start(data):
        print("[event] BUTTONS: Fight start.")
        asyncio.create_task(safe_start_phase_timer("fight"))

    async def handle_fight_pause():
        print("[event] Fight pause.")
        if timer_state.timer_task:
            timer_state.paused = True
            await sio.emit("pause")
            await timer_state.timer_task
            timer_state.timer_task = None

    async def handle_fight_resume():
        print("[event] Fight resume sent.")
        if timer_state.paused and timer_state.remaining_seconds is not None:
            await sio.emit("resume", {"remaining": timer_state.remaining_seconds})
            timer_state.paused = False
            timer_state.timer_task = asyncio.create_task(
                run_timer(timer_state.remaining_seconds)
            )
            timer_state.remaining_seconds = None

    sio_client.on("BACK-END: Overlay data sent.", handle_overlay_data)
    sio_client.on("BACK-END: Preparing start sent.", handle_preparing_start)
    sio_client.on("BACK-END: Fight start sent.", handle_fight_start)
    sio_client.on("BACK-END: Fight pause sent.", handle_fight_pause)
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
            print(f"[proxy] ✅ Установлено соединение с {EXTERNAL_WS_URL}")
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
async def get_overlay():
    if not os.path.exists(OVERLAY_FILE):
        raise HTTPException(status_code=404, detail="Overlay file not found")
    return FileResponse(OVERLAY_FILE)

@app.post("/api/show_message", tags=["Пользовательский текст"])
async def show_custom_message(message: MessageRequest):
    await sio.emit("custom_message", {
        "text": message.text,
        "duration": message.duration
    })
    return {"status": "ok", "sent": message.model_dump()}



# ----------------------- Точка входа -----------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(socket_app, host="127.0.0.1", port=8000)