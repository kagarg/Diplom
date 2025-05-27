from fastapi import FastAPI, status
from fastapi.responses import FileResponse
import os
import asyncio
from typing import Dict, Any
from datetime import datetime, timedelta, timezone
import socketio
from socketio import AsyncClient

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

# Хранилище состояния секундомера
timer_state = {
    "start_time": None,
    "paused": False,
    "pause_time": None,
    "accumulated_pause": timedelta(seconds=0),
    "task": None,
    "base_data": {}
}

def process_overlay_data(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "team1": raw_data.get("team1", "Команда 1"),
        "team2": raw_data.get("team2", "Команда 2"),
        "logo1": raw_data.get("logo1", ""),
        "logo2": raw_data.get("logo2", ""),
        "score1": raw_data.get("score1", 0),
        "score2": raw_data.get("score2", 0),
        "round": raw_data.get("round", 1),
        "time": raw_data.get("time")  # используется только при инициализации
    }

async def start_stopwatch_loop():
    while True:
        if timer_state["start_time"] and not timer_state["paused"]:
            now = datetime.now(timezone.utc)
            elapsed = now - timer_state["start_time"] - timer_state["accumulated_pause"]
            elapsed_seconds = max(0, int(elapsed.total_seconds()))

            # Подготовка данных
            data = timer_state["base_data"].copy()
            # Отправляем оставшееся время в формате ISO конца
            data["time"] = (datetime.now(timezone.utc) + timedelta(seconds=elapsed_seconds)).isoformat()
            await sio.emit("overlay_update", data)
        await asyncio.sleep(1)

async def handle_timer_control(raw_data: Dict[str, Any]):
    control = raw_data.get("timer_control")
    now = datetime.now(timezone.utc)

    if control == "pause" and not timer_state["paused"]:
        timer_state["paused"] = True
        timer_state["pause_time"] = now
        print("[timer] Секундомер приостановлен")

    elif control == "resume" and timer_state["paused"]:
        pause_duration = now - timer_state["pause_time"]
        timer_state["accumulated_pause"] += pause_duration
        timer_state["paused"] = False
        timer_state["pause_time"] = None
        print("[timer] Секундомер возобновлён")

async def forward_external_to_clients():
    sio_client = AsyncClient(reconnection=True)

    async def handle_external_data(data):
        print("[proxy] Получены данные:", data)

        if "time" in data:
            try:
                start_time = datetime.fromisoformat(data["time"].replace("Z", "+00:00"))
                timer_state["start_time"] = start_time
                timer_state["accumulated_pause"] = timedelta(seconds=0)
                timer_state["paused"] = False
                timer_state["pause_time"] = None
                timer_state["base_data"] = process_overlay_data(data)
                print(f"[timer] Секундомер стартовал от {start_time.isoformat()}")
            except Exception as e:
                print("[timer] Ошибка при парсинге времени:", e)

        if "timer_control" in data:
            await handle_timer_control(data)

        processed_data = process_overlay_data(data)
        await sio.emit("overlay_update", processed_data)

    sio_client.on("BACK-END: Overlay data sent.", handle_external_data)

    @sio_client.event
    async def connect():
        print("[proxy] Подключение к внешнему Socket.IO серверу установлено")

    @sio_client.event
    async def disconnect():
        print("[proxy] Отключение от внешнего сервера")

    if not timer_state["task"]:
        timer_state["task"] = asyncio.create_task(start_stopwatch_loop())

    while True:
        try:
            print("[proxy] Пытаемся подключиться к внешнему серверу...")
            await sio_client.connect(EXTERNAL_WS_URL, transports=["websocket"], socketio_path="/socket.io")
            await sio_client.wait()
        except Exception as e:
            print(f"[proxy] Ошибка: {e}. Повтор через 5 секунд...")
            await asyncio.sleep(5)
            try:
                await sio_client.disconnect()
            except:
                pass

@sio.event
async def connect(sid, environ):
    """
    WebSocket подключение: 
    - Инициирует подключение к внешнему серверу
    - Запускает фоновую задачу синхронизации
    """
    print(f"[sio] Клиент подключен: {sid}")
    global external_task
    if not external_task:
        external_task = asyncio.create_task(forward_external_to_clients())

@sio.event
async def disconnect(sid):
    print(f"[sio] Клиент отключен: {sid}")

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
        return {"error": "Overlay file not found"}, 404
    return FileResponse(OVERLAY_FILE)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(socket_app, host="127.0.0.1", port=8000)
