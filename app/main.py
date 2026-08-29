import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

logging.basicConfig(level=logging.INFO)

# Некоторые дата-центры Telegram недоступны напрямую с части российских
# хостингов (нестабильная маршрутизация до конкретных IP-диапазонов —
# не блокировка целиком, а именно часть адресов). Раз конкретный рабочий
# IP уже известен — жёстко прописываем его в /etc/hosts при каждом
# запуске, в обход DNS-резолвинга, который иначе может случайно выдать
# один из недоступных адресов.
try:
    with open("/etc/hosts", "a") as f:
        f.write("149.154.167.220 api.telegram.org\n")
except PermissionError:
    pass  # если контейнер запущен не под root — просто продолжаем со штатным DNS

BOT_TOKEN = os.environ["BOT_TOKEN"]  # задаётся через переменные окружения в кабинете VERF

router = Router()


@router.message(CommandStart())
async def on_start(message: Message):
    await message.answer(
        "Привет! Я простой эхо-бот, задеплоенный на VERF.\n"
        "Напиши мне что угодно — отвечу тем же."
    )


@router.message()
async def echo(message: Message):
    await message.answer(message.text or "Пришли текстовое сообщение — я его повторю.")


async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
