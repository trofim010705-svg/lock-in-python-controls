# Windows EXE для MagnetCommandTester 1.4

Сборка использует проверенный исходный архив `updates/MagnetCommandTester_v1_4.zip`.
Контрольная сумма проверяется до распаковки; управление оборудованием в app.py,
driver.py и gui.py не изменяется. Новый launcher нужен для проверки собранного EXE.

GitHub Actions запускает Windows Server 2022 x64 и Python 3.11, устанавливает
закреплённые версии зависимостей, запускает тесты исходников и PyInstaller.
Готовая программа проверяется без оборудования в обычной и кириллической папках:
создаёт настоящее окно Qt, открывает симулятор, читает показания и завершает QThread.
Во время проверки реальные COM-порты не открываются.

Результат — `updates/MagnetCommandTester_v1_4_Windows_x64.zip` с EXE и папкой
`_internal`, плюс отдельная SHA-256 сумма. После успешных проверок workflow
сохраняет artifact и публикует ZIP в той же ветке work. Приложение не требует
установки Python. Драйвер CP210x остаётся нужен для USB-устройства.

Для локальной сборки на Windows установите Python 3.11 x64, затем:

```bat
py -3.11 -m venv venv
venv\Scripts\python.exe -m pip install --only-binary=:all: -r tools\magnet_windows\requirements-build.txt
venv\Scripts\python.exe tools\magnet_windows\build.py
```

В отдельном архиве Windows Builder достаточно запустить `build_exe.bat`.
Для повторной сборки из Source внутри готового приложения:

```bat
python -m pip install --only-binary=:all: -r requirements-build.txt
python build_windows.py
```

Сборка однопапочная: запускать нужно MagnetCommandTester.exe рядом с `_internal`.
Формат выбран, чтобы Qt загружался из постоянной папки, без распаковки DLL
во временный каталог при каждом запуске. Сборка не подписана сертификатом.
