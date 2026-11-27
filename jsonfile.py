"""JSON fayllarni xavfsiz yozish: avval vaqtinchalik faylga, keyin bir zumda almashtiriladi.

To'g'ridan-to'g'ri write_text() paytida kompyuter o'chib qolsa yoki jarayon yiqilsa, fayl yarim yozilib
buziladi va bot ishga tushmaydi. os.replace() esa faylni butunlay almashtiradi: eski yoki yangi nusxa qoladi.
"""
import json
import os
import time
from pathlib import Path


def write_atomic(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: fayl bir lahza boshqa jarayonda (antivirus, indeksator) ochiq
            if attempt == 4:
                raise
            time.sleep(0.1 * (attempt + 1))


def write_json(path: Path, data, indent: int | None = 1):
    write_atomic(path, json.dumps(data, ensure_ascii=False, indent=indent))
