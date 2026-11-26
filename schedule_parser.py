"""Разбор файлов «Изменения в основное расписание» Костромского энергетического техникума.

Файл (xlsx или pdf) превращается в строки текста, а строки разбираются так:

    1-11. 1-2 ур нет            <- строка с названием группы и первой парой
    3-4 ур Математика каб.36    <- следующие пары этой же группы
    2-1 хао 1-4 ур ТЛР ...      <- группа с суффиксом отделения
    1-2. Экзамен по ...         <- у группы есть только заметка, без пар
"""
import io
import re
import sys
from datetime import date

GROUP_RE = re.compile(r"^(\d-\d{1,2})\.?\s*(?:(хао|эко|ис|тто|газ|это)\b)?\s*(.*)$", re.I)
PAIR_RE = re.compile(r"^(\d-\d)\s*ур\.?\s*(.*)$", re.I)
SKIP_PREFIXES = ("изменения в основное", "на понедельник", "на вторник", "на среду",
                 "на четверг", "на пятницу", "на субботу", "(начало", "(продолжение",
                 "отделение")


def parse_lines(lines):
    """Возвращает {группа: {"lessons": [[слот, текст], ...], "notes": [текст, ...]}}."""
    result = {}
    current = None
    for raw in lines:
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or line.lower().startswith(SKIP_PREFIXES):
            continue

        m = PAIR_RE.match(line)
        if m:
            if current is not None:
                current["lessons"].append([m.group(1), m.group(2).strip()])
            continue

        m = GROUP_RE.match(line)
        if m:
            name = m.group(1) + (" " + m.group(2).upper() if m.group(2) else "")
            current = result.setdefault(name, {"lessons": [], "notes": []})
            rest = m.group(3).strip()
            if rest:
                pm = PAIR_RE.match(rest)
                if pm:
                    current["lessons"].append([pm.group(1), pm.group(2).strip()])
                else:
                    current["notes"].append(rest)
            continue

        # всё остальное — продолжение заметки текущей группы
        if current is not None:
            current["notes"].append(line)
    return result


def xlsx_to_lines(data: bytes):
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = []
            for c in row:
                if c is None:
                    continue
                s = str(c).strip()
                if s:
                    cells.append(s.replace("\n", " "))
            if cells:
                lines.append(" ".join(cells))
    return lines


def pdf_to_lines(data: bytes):
    import pdfplumber
    lines = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            lines.extend((page.extract_text() or "").split("\n"))
    return lines


MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
          "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}


def extract_date(lines):
    """Ищет строку вида «на пятницу, 27 ноября 2026 года»."""
    for line in lines:
        m = re.search(r"на\s+\w+,\s*(\d{1,2})\s+([а-яё]+)\s+(\d{4})", line, re.I)
        if m and m.group(2).lower() in MONTHS:
            return date(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1)))
    return None


def date_from_name(name: str):
    """Имя файла вида 27_11_2026_v1.xlsx."""
    m = re.search(r"(\d{2})_(\d{2})_(\d{4})", name or "")
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def parse_file(data: bytes, kind: str):
    lines = xlsx_to_lines(data) if kind == "xlsx" else pdf_to_lines(data)
    return parse_lines(lines)


def parse_file_full(data: bytes, kind: str, filename: str = ""):
    """Возвращает (дата или None, {группа: ...})."""
    lines = xlsx_to_lines(data) if kind == "xlsx" else pdf_to_lines(data)
    return extract_date(lines) or date_from_name(filename), parse_lines(lines)


if __name__ == "__main__":
    # проверка: python schedule_parser.py файл.xlsx|файл.pdf [группа]
    path = sys.argv[1]
    kind = "pdf" if path.lower().endswith(".pdf") else "xlsx"
    with open(path, "rb") as f:
        groups = parse_file(f.read(), kind)
    print(f"Найдено групп: {len(groups)}")
    for name, g in groups.items():
        if len(sys.argv) > 2 and name != sys.argv[2]:
            continue
        print(f"\n== {name}")
        for slot, text in g["lessons"]:
            print(f"  {slot} ур | {text}")
        for n in g["notes"]:
            print(f"  ! {n}")
