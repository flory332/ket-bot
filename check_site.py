"""Диагностика: python check_site.py"""
import asyncio
import re
from urllib.parse import urljoin

import aiohttp

from schedule_parser import parse_file, xlsx_to_lines

PAGE_URL = "https://spo-ket.gosuslugi.ru/grafik-zanyatiy/"


async def main():
    headers = {"User-Agent": "Mozilla/5.0 (ket-schedule-bot)"}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as s:
        try:
            async with s.get(PAGE_URL) as r:
                print("1) Страница: HTTP", r.status)
                page = await r.text()
        except Exception as e:
            print("1) НЕ УДАЛОСЬ открыть страницу:", repr(e))
            return
        links = re.findall(r"""href=["']([^"']+\.xlsx)["']""", page, re.I)
        print("2) Найдено xlsx-ссылок:", len(links))
        for l in links[:8]:
            print("   ", l)
        if not links:
            print("   Ссылок нет. Фрагмент страницы:", page[:300].replace("\n", " "))
            return
        url = urljoin(PAGE_URL, links[0])
        async with s.get(url) as r:
            print("3) Скачивание", url, "-> HTTP", r.status)
            raw = await r.read()
        print("   Размер:", len(raw), "байт")
        try:
            lines = xlsx_to_lines(raw)
            print("4) Строк текста в файле:", len(lines))
            for l in lines[:12]:
                print("   |", l)
            print("5) Найдено групп:", len(parse_file(raw, "xlsx")))
        except Exception as e:
            print("4) Ошибка чтения xlsx:", repr(e))


asyncio.run(main())
