import sys
import os
import struct
import shutil
import subprocess
import tempfile
import threading
import importlib.util
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QFileDialog, QRadioButton,
                             QButtonGroup, QCheckBox, QComboBox, QGroupBox,
                             QTextEdit, QMessageBox, QProgressBar)
from PyQt6.QtCore import Qt, pyqtSignal, QObject, QBuffer, QIODevice
from PyQt6.QtGui import QImage, QImageReader, QPainter


# 日志信号类：子线程与UI通信核心
class LogSignal(QObject):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int, str)


# ---------------- 图标处理（纯 Qt 实现，不依赖 Pillow） ----------------
ICO_MAGIC = b"\x00\x00\x01\x00"
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def is_valid_ico(path):
    """按文件头 + 目录项校验是否为 PyInstaller 认可的标准 ICO。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return False

    if len(data) < 6 + 16 or data[:4] != ICO_MAGIC:
        return False  # 典型情况：PNG/JPG 改了 .ico 后缀

    count = int.from_bytes(data[4:6], "little")
    if count <= 0 or len(data) < 6 + 16 * count:
        return False

    for i in range(count):
        off = 6 + 16 * i
        width = data[off] or 256
        height = data[off + 1] or 256
        size = int.from_bytes(data[off + 8:off + 12], "little")
        pos = int.from_bytes(data[off + 12:off + 16], "little")
        if width > 256 or height > 256 or size <= 0 or pos + size > len(data):
            return False
    return True


def load_image_any(path):
    """用 Qt 按内容读取图片（不受错误扩展名影响），返回 ARGB32 图像。"""
    reader = QImageReader(path)
    reader.setAutoTransform(True)
    image = reader.read()
    if image.isNull():
        raise ValueError(reader.errorString() or "无法识别图片格式")
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def make_square_icon(image, size=256):
    """等比缩放后居中铺在透明正方形画布上，避免非正方形截图被拉伸。"""
    scaled = image.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    canvas = QImage(size, size, QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.drawImage((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def image_to_dib(image):
    """把正方形图像编码为 32 位 DIB（ICO 内嵌位图，兼容性最好）。"""
    width, height = image.width(), image.height()
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    bits = image.bits()
    bits.setsize(image.sizeInBytes())
    raw = bytes(bits)
    stride = image.bytesPerLine()
    # DIB 像素自下而上存储
    xor_bitmap = b"".join(raw[y * stride: y * stride + width * 4]
                          for y in range(height - 1, -1, -1))
    # 32 位图依赖 alpha 通道，AND 掩码置 0 即可
    mask_stride = ((width + 31) // 32) * 4
    and_bitmap = b"\x00" * (mask_stride * height)
    header = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0,
                         len(xor_bitmap) + len(and_bitmap), 0, 0, 0, 0)
    return header + xor_bitmap + and_bitmap


def image_to_png(image):
    """把图像编码为 PNG 字节（ICO 的 128/256 尺寸使用 PNG 压缩）。"""
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise ValueError("PNG 编码失败")
    return bytes(buffer.data())


def convert_image_to_ico(src_path, dst_path):
    """把任意 Qt 支持的图片（含伪装成 .ico 的 PNG/JPG）转成标准多尺寸 ICO。"""
    base = make_square_icon(load_image_any(src_path), 256)
    entries = []
    for size in ICO_SIZES:
        icon_image = base.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
        data = image_to_png(icon_image) if size >= 128 else image_to_dib(icon_image)
        entries.append((size, data))

    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = b""
    blobs = b""
    for size, data in entries:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)

    with open(dst_path, "wb") as f:
        f.write(header + directory + blobs)
    return [s for s, _ in entries]


# 主窗口类
class PyInstallerGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python 一键打包工具 ✨")
        self.setMinimumSize(1050, 720)
        self.setStyleSheet(self.get_global_style())

        self.log_signal = LogSignal()
        self.log_signal.log_signal.connect(self.append_log)
        self.log_signal.progress_signal.connect(self.update_progress)

        # 全局变量
        self.py_file_path = ""
        self.output_dir = ""
        self.custom_dist_dir = ""
        self.icon_path = ""
        self.python_path = sys.executable
        self.packaging_thread = None
        self.process = None
        self._temp_icon_dir = ""

        self.init_ui()

    def get_global_style(self):
        return """
        QMainWindow {
            background-color: #f7f8fa;
        }
        QGroupBox {
            font-size: 14px;
            font-weight: bold;
            color: #2c3e50;
            border: 1px solid #dcdfe6;
            border-radius: 10px;
            margin-top: 10px;
            padding-top: 10px;
            background-color: white;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 8px;
            color: #34495e;
        }
        QPushButton {
            background-color: #3498db;
            color: white;
            border: none;
            border-radius: 6px;
            padding: 7px 14px;
            font-size: 13px;
        }
        QPushButton:hover {
            background-color: #2980b9;
        }
        QPushButton:pressed {
            background-color: #1f618d;
        }
        QPushButton#startBtn {
            background-color: #2ecc71;
            font-size: 14px;
            padding: 10px 20px;
        }
        QPushButton#startBtn:hover {
            background-color: #27ae60;
        }
        QLineEdit {
            border: 1px solid #dcdfe6;
            border-radius: 6px;
            padding: 6px 10px;
            font-size: 13px;
            background-color: white;
            selection-background-color: #3498db;
        }
        QLineEdit:read-only {
            background-color: #f8f9fa;
            color: #555;
        }
        QTextEdit {
            border: 1px solid #dcdfe6;
            border-radius: 8px;
            padding: 8px;
            font-size: 13px;
            background-color: white;
        }
        QProgressBar {
            border: none;
            border-radius: 8px;
            height: 14px;
            background-color: #ecf0f1;
            text-align: center;
            font-size: 12px;
        }
        QProgressBar::chunk {
            border-radius: 8px;
            background-color: #3498db;
        }
        QRadioButton, QCheckBox {
            font-size: 13px;
            spacing: 6px;
        }
        QComboBox {
            border: 1px solid #dcdfe6;
            border-radius: 6px;
            padding: 5px 10px;
            font-size: 13px;
            background-color: white;
        }
        QLabel {
            font-size: 13px;
            color: #2c3e50;
        }
        """

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)
        main_layout.setSpacing(20)
        main_layout.setContentsMargins(25, 25, 25, 25)

        # 左侧面板
        left_layout = QVBoxLayout()
        left_layout.setSpacing(18)

        # 入口文件
        entry_group = QGroupBox("📄 入口文件")
        entry_layout = QHBoxLayout(entry_group)
        self.entry_edit = QLineEdit()
        self.entry_edit.setReadOnly(True)
        self.entry_btn = QPushButton("选择文件")
        self.entry_btn.clicked.connect(self.select_py_file)
        entry_layout.addWidget(self.entry_edit)
        entry_layout.addWidget(self.entry_btn)
        left_layout.addWidget(entry_group)

        # 资源与输出
        res_group = QGroupBox("🗂️ 输出设置")
        res_layout = QVBoxLayout(res_group)
        res_layout.setSpacing(10)

        # 自定义输出位置（可选）
        res_layout.addWidget(QLabel("输出目录:"))
        custom_output_layout = QHBoxLayout()
        self.custom_output_edit = QLineEdit()
        self.custom_output_edit.setPlaceholderText("留空则使用默认位置（入口文件所在目录/dist）")
        self.custom_output_btn = QPushButton("选择目录")
        self.custom_output_btn.clicked.connect(self.select_custom_output_dir)
        custom_output_layout.addWidget(self.custom_output_edit)
        custom_output_layout.addWidget(self.custom_output_btn)
        res_layout.addLayout(custom_output_layout)

        res_layout.addWidget(QLabel("应用图标:"))
        icon_layout = QHBoxLayout()
        self.icon_edit = QLineEdit()
        self.icon_edit.setPlaceholderText("可选（PNG/JPG 会自动转成标准 ICO）")
        self.icon_edit.setReadOnly(True)
        self.icon_select_btn = QPushButton("选择图标")
        self.icon_select_btn.clicked.connect(self.select_icon)
        icon_layout.addWidget(self.icon_edit)
        icon_layout.addWidget(self.icon_select_btn)
        res_layout.addLayout(icon_layout)
        left_layout.addWidget(res_group)
        left_layout.addStretch()

        # 右侧面板
        right_layout = QVBoxLayout()
        right_layout.setSpacing(18)

        build_group = QGroupBox("🚀 构建选项")
        build_layout = QVBoxLayout(build_group)
        build_layout.setSpacing(12)

        dep_layout = QHBoxLayout()
        self.auto_dep_check = QCheckBox("自动检测并安装缺失依赖")
        self.auto_dep_check.setChecked(True)
        self.manual_dep_btn = QPushButton("手动检查依赖")
        self.manual_dep_btn.clicked.connect(self.check_dependencies)
        dep_layout.addWidget(self.auto_dep_check)
        dep_layout.addWidget(self.manual_dep_btn)
        build_layout.addLayout(dep_layout)

        # 精简打包选项
        self.slim_pack_check = QCheckBox("只打包必需依赖 (精简模式)")
        self.slim_pack_check.setChecked(True)
        self.slim_pack_check.setToolTip("仅打包入口文件直接导入的模块，减少体积")
        build_layout.addWidget(self.slim_pack_check)

        option_layout = QHBoxLayout()
        self.hide_console_check = QCheckBox("隐藏控制台")
        self.hide_console_check.setChecked(True)
        option_layout.addWidget(self.hide_console_check)
        build_layout.addLayout(option_layout)

        tip_label = QLabel("✨ 打包为独立 EXE 文件（无需安装 Python 环境）")
        tip_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tip_label.setStyleSheet("color: #27ae60; font-weight: bold;")
        build_layout.addWidget(tip_label)
        right_layout.addWidget(build_group)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        right_layout.addWidget(self.progress_bar)

        log_group = QGroupBox("📜 打包日志")
        log_layout = QVBoxLayout(log_group)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        log_layout.addWidget(self.log_text)
        right_layout.addWidget(log_group)

        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始打包")
        self.start_btn.setObjectName("startBtn")
        self.start_btn.clicked.connect(self.start_packaging)
        self.clear_log_btn = QPushButton("清空日志")
        self.clear_log_btn.clicked.connect(self.clear_log)
        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.clear_log_btn)
        right_layout.addLayout(btn_layout)

        main_layout.addLayout(left_layout, 1)
        main_layout.addLayout(right_layout, 1)

    def select_py_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择Python文件", "", "*.py")
        if path:
            self.py_file_path = path
            self.entry_edit.setText(path)
            # 清空自定义输出选择，让用户重新决定
            self.custom_dist_dir = ""
            self.custom_output_edit.clear()

    def select_custom_output_dir(self):
        """选择自定义输出目录"""
        path = QFileDialog.getExistingDirectory(self, "选择打包输出目录")
        if path:
            self.custom_dist_dir = path
            self.custom_output_edit.setText(path)

    def select_icon(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图标", "", "图标文件 (*.ico *.png *.jpg *.jpeg *.bmp *.webp)")
        if path:
            self.icon_path = path
            self.icon_edit.setText(path)
            if path.lower().endswith('.ico') and is_valid_ico(path):
                self.log_signal.log_signal.emit(f"🎨 已选择图标: {os.path.basename(path)}")
            else:
                self.log_signal.log_signal.emit(
                    f"🎨 已选择图片，打包时会自动转换为多尺寸 ICO: {os.path.basename(path)}")

    def get_imported_modules(self):
        """解析Python文件，获取实际导入的模块列表（精简模式核心）"""
        if not self.py_file_path:
            return set()

        try:
            with open(self.py_file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception as e:
            self.log_signal.log_signal.emit(f"❌ 读取文件失败: {e}")
            return set()

        imports = set()
        lines = content.split('\n')

        for line in lines:
            line = line.strip()
            if line.startswith("import "):
                # import xxx, yyy
                parts = line[7:].split(',')
                for part in parts:
                    mod = part.strip().split('.')[0].split(' as ')[0]
                    if mod and mod not in imports:
                        imports.add(mod)
            elif line.startswith("from "):
                # from xxx import yyy
                parts = line[5:].split(' import ')
                if len(parts) > 1:
                    mod = parts[0].strip().split('.')[0]
                    if mod and mod not in imports:
                        imports.add(mod)

        # 过滤标准库和内置模块
        stdlib_modules = set(sys.builtin_module_names)
        # 常见标准库前缀
        stdlib_prefixes = ('_', 'abc', 'aifc', 'argparse', 'array', 'ast', 'asyncio',
                           'base64', 'bdb', 'binascii', 'bisect', 'builtins', 'bz2',
                           'calendar', 'cgi', 'cmath', 'cmd', 'codecs', 'collections',
                           'compileall', 'concurrent', 'configparser', 'contextlib',
                           'copy', 'cProfile', 'crypt', 'csv', 'ctypes', 'curses',
                           'datetime', 'dbm', 'decimal', 'difflib', 'dis', 'doctest',
                           'email', 'encodings', 'enum', 'errno', 'faulthandler',
                           'fcntl', 'filecmp', 'fileinput', 'fnmatch', 'fractions',
                           'ftplib', 'functools', 'gc', 'getopt', 'getpass', 'gettext',
                           'glob', 'gzip', 'hashlib', 'heapq', 'hmac', 'html', 'http',
                           'imaplib', 'imghdr', 'imp', 'importlib', 'inspect', 'io',
                           'itertools', 'json', 'keyword', 'lib2to3', 'linecache',
                           'locale', 'logging', 'lzma', 'mailbox', 'mailcap', 'marshal',
                           'math', 'mimetypes', 'mmap', 'modulefinder', 'msilib',
                           'multiprocessing', 'netrc', 'nis', 'nntplib', 'numbers',
                           'operator', 'optparse', 'os', 'parser', 'pathlib', 'pdb',
                           'pickle', 'pickletools', 'pipes', 'pkgutil', 'platform',
                           'plistlib', 'poplib', 'posix', 'pprint', 'profile', 'pstats',
                           'pty', 'pwd', 'py_compile', 'pyclbr', 'pydoc', 'queue',
                           'quopri', 'random', 're', 'readline', 'reprlib', 'resource',
                           'rlcompleter', 'runpy', 'sched', 'secrets', 'select',
                           'selectors', 'shelve', 'shlex', 'shutil', 'signal', 'smtpd',
                           'smtplib', 'sndhdr', 'socket', 'socketserver', 'spwd',
                           'sqlite3', 'ssl', 'stat', 'statistics', 'string', 'stringprep',
                           'struct', 'subprocess', 'sunau', 'symbol', 'symtable', 'sys',
                           'sysconfig', 'syslog', 'tabnanny', 'tarfile', 'telnetlib',
                           'tempfile', 'termios', 'textwrap', 'threading', 'time',
                           'timeit', 'tkinter', 'token', 'tokenize', 'trace', 'traceback',
                           'tracemalloc', 'tty', 'turtle', 'types', 'typing', 'unicodedata',
                           'unittest', 'urllib', 'uu', 'uuid', 'venv', 'warnings', 'wave',
                           'weakref', 'webbrowser', 'winreg', 'winsound', 'wsgiref',
                           'xdrlib', 'xml', 'xmlrpc', 'zipapp', 'zipfile', 'zipimport',
                           'zlib')

        third_party_modules = set()
        for mod in imports:
            if mod in stdlib_modules or mod in stdlib_prefixes or mod.startswith('_'):
                continue
            # 检查是否为第三方模块（非当前目录文件）
            spec = importlib.util.find_spec(mod)
            if spec and spec.origin and 'site-packages' in spec.origin:
                third_party_modules.add(mod)
            elif spec and spec.origin and not spec.origin.startswith(os.path.dirname(self.py_file_path)):
                third_party_modules.add(mod)

        return third_party_modules

    def _venv_python_near(self, script_path, max_levels=6):
        """在入口文件所在目录及上级目录寻找 .venv / venv 解释器。"""
        folder = os.path.dirname(os.path.abspath(script_path))
        for _ in range(max_levels):
            for name in (".venv", "venv", "env"):
                if sys.platform == "win32":
                    candidate = os.path.join(folder, name, "Scripts", "python.exe")
                else:
                    candidate = os.path.join(folder, name, "bin", "python3")
                if os.path.isfile(candidate):
                    return candidate
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent
        return None

    def python_has_pyinstaller(self, exe):
        """用指定解释器实际探测 PyInstaller 是否可用。"""
        try:
            res = subprocess.run(
                [exe, "-m", "PyInstaller", "--version"],
                capture_output=True, text=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            return res.returncode == 0
        except Exception:
            return False

    def resolve_build_python(self):
        """选择打包解释器：优先项目 venv 中已装 PyInstaller 的那个。"""
        candidates = []
        if not getattr(sys, "frozen", False):
            candidates.append(sys.executable)
        venv_exe = self._venv_python_near(self.py_file_path) if self.py_file_path else None
        if venv_exe:
            candidates.append(venv_exe)
        env_exe = os.environ.get("PYTHON_EXE")
        if env_exe:
            candidates.append(env_exe)

        unique = []
        seen = set()
        for exe in candidates:
            if not exe or not os.path.isfile(exe):
                continue
            key = os.path.normcase(os.path.abspath(exe))
            if key in seen:
                continue
            seen.add(key)
            unique.append(exe)

        # 1. 已有 PyInstaller 的解释器直接用
        for exe in unique:
            if self.python_has_pyinstaller(exe):
                return exe
        # 2. 否则优先装进项目 venv（依赖都在那里）
        if venv_exe:
            return venv_exe
        return unique[0] if unique else sys.executable

    PIP_SOURCES = (
        ("默认源", []),
        ("清华镜像", ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
                     "--trusted-host", "pypi.tuna.tsinghua.edu.cn"]),
        ("阿里云镜像", ["-i", "https://mirrors.aliyun.com/pypi/simple/",
                       "--trusted-host", "mirrors.aliyun.com"]),
    )

    def pip_install(self, package, timeout=600):
        """用选定解释器安装包，多个源依次尝试，返回 (成功, 最后错误信息)。"""
        last_error = ""
        for label, extra in self.PIP_SOURCES:
            cmd = [self.python_path, "-m", "pip", "install", package,
                   "--disable-pip-version-check"] + extra
            self.log_signal.log_signal.emit(f"🌐 尝试{label}: {package}")
            try:
                res = subprocess.run(cmd, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace", timeout=timeout)
            except subprocess.TimeoutExpired:
                last_error = "安装超时"
                self.log_signal.log_signal.emit(f"⏰ {label} 超时，换下一个源...")
                continue
            except Exception as e:
                last_error = str(e)
                self.log_signal.log_signal.emit(f"❌ {label} 异常：{e}")
                continue
            if res.returncode == 0:
                return True, ""
            last_error = (res.stderr or res.stdout or "").strip()
            tail = "\n".join(last_error.splitlines()[-3:]) if last_error else "未知错误"
            self.log_signal.log_signal.emit(f"⚠️ {label} 失败：{tail}")
        return False, last_error

    def check_dependencies(self):
        if not self.py_file_path:
            self.log_signal.log_signal.emit("❌ 请先选择入口文件")
            return

        self.log_signal.log_signal.emit("🔍 正在检查依赖...")
        self.python_path = self.resolve_build_python()
        self.log_signal.log_signal.emit(f"🐍 使用解释器: {self.python_path}")

        # 用选定的解释器探测 PyInstaller（不能直接 import，可能和 GUI 解释器不是同一个）
        if self.python_has_pyinstaller(self.python_path):
            self.log_signal.log_signal.emit("✅ PyInstaller 已安装")
        else:
            self.log_signal.log_signal.emit("⚠️ PyInstaller 未安装")
            if not self.auto_dep_check.isChecked():
                self.log_signal.log_signal.emit("❌ 请先安装 PyInstaller 再打包")
                return
            if not self.install_pyinstaller():
                return

        # 精简模式：只检查实际导入的模块
        if self.slim_pack_check.isChecked():
            imports = self.get_imported_modules()
            self.log_signal.log_signal.emit(f"📦 检测到导入的第三方模块: {', '.join(imports) if imports else '无'}")
        else:
            # 传统模式：扫描所有import语句
            try:
                with open(self.py_file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except:
                self.log_signal.log_signal.emit("❌ 读取文件失败")
                return

            imports = set()
            for line in lines:
                line = line.strip()
                if line.startswith("import "):
                    parts = line.split()
                    if len(parts) > 1:
                        mod = parts[1].split(".")[0]
                        imports.add(mod)
                elif line.startswith("from "):
                    parts = line.split()
                    if len(parts) > 1:
                        mod = parts[1].split(".")[0]
                        imports.add(mod)

        # 需要映射的特殊包名
        pkg_map = {
            "PIL": "Pillow",
            "cv2": "opencv-python",
            "fitz": "PyMuPDF",
            "np": "numpy",
            "pd": "pandas",
            "sklearn": "scikit-learn",
            "bs4": "beautifulsoup4",
            "yaml": "PyYAML",
            "MySQLdb": "mysqlclient",
            "psycopg2": "psycopg2-binary",
            "PIL.Image": "Pillow",
            "Pillow": "Pillow"
        }

        # 过滤标准库
        stdlib = {'os', 'sys', 're', 'json', 'time', 'threading', 'subprocess',
                  'PyQt6', 'PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.QtGui',
                  'collections', 'itertools', 'functools', 'pathlib', 'glob',
                  'shutil', 'tempfile', 'argparse', 'logging', 'datetime',
                  'math', 'random', 'statistics', 'hashlib', 'hmac', 'base64',
                  'binascii', 'textwrap', 'pprint', 'copy', 'weakref', 'enum',
                  'abc', 'contextlib', 'io', 'struct', 'pickle', 'shelve',
                  'sqlite3', 'xml', 'html', 'urllib', 'http', 'socket', 'ssl',
                  'email', 'json', 'csv', 'configparser'}

        missing = []
        for imp in imports:
            if imp in stdlib or imp.startswith('_'):
                continue
            pkg = pkg_map.get(imp, imp)
            # 用选定的解释器检查包是否存在
            try:
                res = subprocess.run(
                    [self.python_path, "-c", f"import {imp.split('.')[0]}"],
                    capture_output=True, text=True, timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                available = res.returncode == 0
            except Exception:
                available = False
            if not available:
                missing.append(pkg)

        if missing:
            self.log_signal.log_signal.emit(f"⚠️ 缺失依赖：{', '.join(missing)}")
            if self.auto_dep_check.isChecked():
                self.install_deps(missing)
        else:
            self.log_signal.log_signal.emit("✅ 所有依赖已安装")

    def install_pyinstaller(self):
        """在选定的解释器中安装 PyInstaller。"""
        self.log_signal.log_signal.emit(f"📦 正在安装 PyInstaller 到: {self.python_path}")
        ok, error = self.pip_install("PyInstaller")
        if ok and self.python_has_pyinstaller(self.python_path):
            self.log_signal.log_signal.emit("✅ PyInstaller 安装成功")
            return True
        self.log_signal.log_signal.emit("❌ PyInstaller 安装失败")
        if error:
            for line in error.splitlines()[-8:]:
                self.log_signal.log_signal.emit("   " + line)
        self.log_signal.log_signal.emit(
            f'💡 可手动执行: "{self.python_path}" -m pip install pyinstaller')
        return False

    def install_deps(self, packages):
        total = len(packages)
        for i, pkg in enumerate(packages, 1):
            self.log_signal.progress_signal.emit(int(i / total * 100), f"安装 {pkg}")
            ok, error = self.pip_install(pkg)
            if ok:
                self.log_signal.log_signal.emit(f"✅ 安装成功：{pkg}")
            else:
                self.log_signal.log_signal.emit(f"❌ 安装失败：{pkg}")
                if error:
                    for line in error.splitlines()[-5:]:
                        self.log_signal.log_signal.emit("   " + line)
        self.log_signal.progress_signal.emit(100, "依赖安装完成")

    def append_log(self, msg):
        self.log_text.append(msg)
        self.log_text.verticalScrollBar().setValue(self.log_text.verticalScrollBar().maximum())

    def update_progress(self, v, t):
        self.progress_bar.setValue(v)
        self.progress_bar.setFormat(f"{v}% • {t}")

    def clear_log(self):
        self.log_text.clear()
        self.progress_bar.setValue(0)

    def start_packaging(self):
        if not self.py_file_path:
            QMessageBox.warning(self, "提示", "请先选择Python文件")
            return

        # 确定输出目录
        if self.custom_output_edit.text().strip():
            output_path = self.custom_output_edit.text().strip()
            os.makedirs(output_path, exist_ok=True)
            self.output_dir = output_path
        else:
            # 默认输出到入口文件所在目录的 dist 文件夹
            default_out = os.path.join(os.path.dirname(self.py_file_path), "dist")
            os.makedirs(default_out, exist_ok=True)
            self.output_dir = default_out

        self.start_btn.setEnabled(False)
        self.packaging_thread = threading.Thread(target=self.run_package)
        self.packaging_thread.daemon = True
        self.packaging_thread.start()

    def resolve_icon(self):
        """返回可用的 .ico 绝对路径；PNG/JPG 或伪装成 .ico 的图片会自动转换。"""
        icon = self.icon_path
        if not icon:
            return None
        if not os.path.isfile(icon):
            self.log_signal.log_signal.emit(f"⚠️ 图标文件不存在，已忽略: {icon}")
            return None

        # 标准 ICO：直接使用
        if icon.lower().endswith('.ico') and is_valid_ico(icon):
            self.log_signal.log_signal.emit(f"🎨 使用图标: {os.path.basename(icon)}")
            return icon

        # 内容不是标准 ICO（常见于截图改后缀）——用 Qt 转成多尺寸 ICO
        try:
            self._temp_icon_dir = tempfile.mkdtemp(prefix="pyinstaller_icon_")
            target = os.path.join(self._temp_icon_dir, "app.ico")
            sizes = convert_image_to_ico(icon, target)
            self.log_signal.log_signal.emit(
                f"🎨 图标已转换为标准 ICO（尺寸 {sizes}，含透明通道）")
            return target
        except Exception as e:
            self.log_signal.log_signal.emit(f"⚠️ 图标转换失败，将使用默认图标: {e}")
            return None

    def run_package(self):
        try:
            # 选择打包解释器（GUI 自己的解释器可能没装 PyInstaller）
            self.python_path = self.resolve_build_python()
            self.log_signal.log_signal.emit(f"🐍 打包解释器: {self.python_path}")

            # 检查依赖（内部会按需安装 PyInstaller）
            if self.auto_dep_check.isChecked():
                self.check_dependencies()

            if not self.python_has_pyinstaller(self.python_path):
                self.log_signal.log_signal.emit("❌ 当前解释器没有可用的 PyInstaller，已停止打包：")
                self.log_signal.log_signal.emit(f"   {self.python_path}")
                self.log_signal.log_signal.emit("   💡 可尝试设置环境变量 PYTHON_EXE 指向已装 PyInstaller 的 python.exe")
                self.log_signal.progress_signal.emit(100, "打包失败")
                return

            # 构建 PyInstaller 命令
            cmd = [self.python_path, "-m", "PyInstaller"]

            # 固定为单文件模式（只打包为 exe）
            cmd.append("--onefile")

            # 控制台选项
            if self.hide_console_check.isChecked():
                cmd.append("--windowed")

            # 图标（自动识别并转换为标准 ICO）
            icon_for_build = self.resolve_icon()
            if icon_for_build:
                cmd.extend(["--icon", icon_for_build])

            # 固定不启用 UPX：对 Python/PyQt6 体积收益很小，还会拖慢启动并易被误报
            cmd.append("--noupx")

            # 精简模式：排除不必要的包
            if self.slim_pack_check.isChecked():
                # 获取实际导入的模块
                imported_modules = self.get_imported_modules()
                if imported_modules:
                    # 使用 --exclude-module 排除未导入的常见大包
                    common_excludes = ['matplotlib', 'pandas', 'numpy', 'scipy', 'PIL',
                                       'cv2', 'tensorflow', 'torch', 'keras', 'sklearn',
                                       'scikit_learn', 'django', 'flask', 'requests',
                                       'beautifulsoup4', 'selenium', 'scrapy']
                    for exclude in common_excludes:
                        if exclude not in imported_modules:
                            cmd.extend(['--exclude-module', exclude])
                            self.log_signal.log_signal.emit(f"🔧 排除未使用的模块: {exclude}")

                    # 添加仅导入的隐藏导入
                    for mod in imported_modules:
                        if mod not in ['PyQt6']:  # PyQt6 单独处理
                            cmd.extend(['--hidden-import', mod])
                else:
                    self.log_signal.log_signal.emit("ℹ️ 未检测到第三方模块，使用标准打包模式")

            # 关键参数
            cmd.extend([
                "--noconfirm",
                "--clean",
                "--log-level", "INFO",
                "--distpath", self.output_dir,
                "--workpath", os.path.join(self.output_dir, "build_temp"),
                "--specpath", self.output_dir,
                self.py_file_path
            ])

            # 添加 PyQt6 相关隐藏导入
            cmd.extend([
                "--hidden-import", "PyQt6.sip",
                "--hidden-import", "PyQt6.QtCore",
                "--hidden-import", "PyQt6.QtWidgets",
                "--hidden-import", "PyQt6.QtGui"
            ])

            self.log_signal.log_signal.emit(f"🚀 开始打包...")
            self.log_signal.log_signal.emit(f"📁 输出目录: {self.output_dir}")
            self.log_signal.progress_signal.emit(30, "准备打包...")

            # 执行打包
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )

            self.log_signal.progress_signal.emit(50, "打包中...")

            # 实时输出日志
            for line in self.process.stdout:
                if line.strip():
                    self.log_signal.log_signal.emit(line.strip())

            self.process.wait()

            # 清理临时文件
            file_stem = os.path.splitext(os.path.basename(self.py_file_path))[0]
            temp_spec = os.path.join(self.output_dir, file_stem + ".spec")
            if os.path.exists(temp_spec):
                try:
                    os.remove(temp_spec)
                except OSError:
                    pass

            # 清理临时构建目录
            temp_build = os.path.join(self.output_dir, "build_temp")
            if os.path.exists(temp_build):
                shutil.rmtree(temp_build, ignore_errors=True)

            if self.process.returncode == 0:
                output_path = os.path.join(self.output_dir, file_stem + ".exe")
                self.log_signal.log_signal.emit(f"✅ 打包成功！")
                self.log_signal.log_signal.emit(f"📦 EXE 位置: {output_path}")
                if icon_for_build:
                    self.log_signal.log_signal.emit("🖼 自定义图标已写入 EXE（若资源管理器仍显示旧图标，请重命名/移动 EXE 或刷新图标缓存）")
                    # 通知资源管理器刷新该文件的图标缓存
                    try:
                        import ctypes
                        ctypes.windll.shell32.SHChangeNotify(
                            0x00002000, 0x0005,  # SHCNE_UPDATEITEM, SHCNF_PATHW
                            ctypes.c_wchar_p(os.path.abspath(output_path)), None)
                    except Exception:
                        pass
                self.log_signal.progress_signal.emit(100, "打包完成")
            else:
                self.log_signal.log_signal.emit(f"❌ 打包失败，错误代码：{self.process.returncode}")
                self.log_signal.progress_signal.emit(100, "打包失败")

        except Exception as e:
            self.log_signal.log_signal.emit(f"❌ 异常：{str(e)}")
            import traceback
            self.log_signal.log_signal.emit(traceback.format_exc())
            self.log_signal.progress_signal.emit(100, "错误")
        finally:
            if self._temp_icon_dir:
                shutil.rmtree(self._temp_icon_dir, ignore_errors=True)
                self._temp_icon_dir = ""
            self.start_btn.setEnabled(True)
            self.process = None


if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = PyInstallerGUI()
    w.show()
    sys.exit(app.exec())