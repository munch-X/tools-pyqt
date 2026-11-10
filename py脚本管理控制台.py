import sys
import os
import shutil
import subprocess
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QFileDialog, QListWidget, QTextEdit, QLabel, QMessageBox,
    QGroupBox, QSplitter, QListWidgetItem, QFrame, QInputDialog,
    QLineEdit, QTabWidget, QPlainTextEdit, QDialog, QDialogButtonBox,
    QToolButton, QMenu
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer, QFileSystemWatcher, QSize, QSharedMemory
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtGui import QFont, QColor, QPalette, QBrush, QIcon, QAction, QKeySequence, QPixmap, QPainter
from datetime import datetime
import locale
import json


# ===== 配置持久化（记住上次使用的目录） =====
CONFIG_ENV_VAR = "SCRIPT_MANAGER_CONFIG"

# 单实例通信：再次启动时让已有窗口刷新列表
MSG_REFRESH = "__refresh__"


def config_file_path():
    """配置文件路径：优先环境变量，其次 %APPDATA%\\MultiScriptManager\\config.json"""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return override
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "MultiScriptManager", "config.json")


def load_config():
    """读取配置；文件不存在或损坏时返回空字典，不影响启动"""
    try:
        with open(config_file_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data):
    """原子写入配置，失败只返回 False，不打断界面操作"""
    path = config_file_path()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


# ===== 单实例管理器 =====
class SingleInstanceManager:
    """管理应用程序的单实例运行"""

    def __init__(self, app_name="MultiScriptManager"):
        self.app_name = app_name
        self.shared_memory = QSharedMemory()
        self.shared_memory.setKey(f"{app_name}_shared")
        self.local_server = None
        self.is_first_instance = False
        self.window = None
        self._buffers = {}  # QLocalSocket -> 已收到的字节

    def try_lock(self):
        """尝试获取单实例锁"""
        # 首先尝试连接到已有的服务器
        if self.connect_to_existing():
            return False

        # 尝试创建共享内存
        if self.shared_memory.attach():
            # 共享内存已存在，说明有实例在运行
            self.shared_memory.detach()
            if self.connect_to_existing():
                return False
            return False

        # 创建新的共享内存
        if not self.shared_memory.create(1):
            return False

        # 创建本地服务器
        self.local_server = QLocalServer()
        if not self.local_server.listen(self.app_name):
            return False

        self.local_server.newConnection.connect(self.handle_new_connection)

        self.is_first_instance = True
        return True

    def connect_to_existing(self):
        """连接到已存在的实例"""
        try:
            socket = QLocalSocket()
            socket.connectToServer(self.app_name)
            if socket.waitForConnected(1000):
                socket.disconnectFromServer()
                return True
        except:
            pass
        return False

    def handle_new_connection(self):
        """处理新实例的连接请求（异步读取，避免数据还没到达就读到空内容）"""
        if not self.local_server:
            return

        connection = self.local_server.nextPendingConnection()
        if not connection:
            return

        self._buffers[connection] = b""
        connection.readyRead.connect(lambda c=connection: self._on_ready_read(c))
        connection.disconnected.connect(lambda c=connection: self._on_disconnected(c))

    def _consume_messages(self, connection):
        """按换行拆包，派发完整消息"""
        buf = self._buffers.get(connection, b"")
        consumed = False
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            self._dispatch_message(line.decode("utf-8", "replace"))
            consumed = True
        self._buffers[connection] = buf
        if consumed:
            # 回执：让发送方确认消息已被接收，再断开连接
            try:
                connection.write(b"ok\n")
                connection.flush()
            except Exception:
                pass

    def _on_ready_read(self, connection):
        try:
            self._buffers[connection] = self._buffers.get(connection, b"") + connection.readAll().data()
            self._consume_messages(connection)
        except Exception as e:
            print(f"读取连接数据时出错: {e}")

    def _on_disconnected(self, connection):
        try:
            # 客户端未发送换行时，把剩余内容也当作一条消息
            self._buffers[connection] = self._buffers.get(connection, b"") + connection.readAll().data()
            self._consume_messages(connection)
            leftover = self._buffers.pop(connection, b"").strip()
            if leftover:
                self._dispatch_message(leftover.decode("utf-8", "replace"))
        except Exception as e:
            print(f"处理断开连接时出错: {e}")
        finally:
            self._buffers.pop(connection, None)
            connection.deleteLater()

    def _dispatch_message(self, data):
        data = data.strip()
        if not data or not self.window:
            return
        try:
            self.window.handle_message(data)
        except Exception as e:
            print(f"处理消息时出错: {e}")

    def send_message_to_existing(self, message):
        """向已存在的实例发送一条消息（路径或刷新指令），等待回执后断开"""
        try:
            socket = QLocalSocket()
            socket.connectToServer(self.app_name)
            if socket.waitForConnected(1000):
                socket.write((message + "\n").encode('utf-8'))
                socket.flush()
                socket.waitForBytesWritten(1000)
                # 等对方处理完并回执；旧版本没有回执时最多等 800ms
                socket.waitForReadyRead(800)
                socket.readAll()
                socket.disconnectFromServer()
                return True
        except Exception as e:
            print(f"发送消息到已有实例时出错: {e}")
        return False

    def send_file_to_existing(self, file_path):
        """向已存在的实例发送文件路径"""
        return self.send_message_to_existing(file_path)


# ===== 脚本编辑器对话框 =====
class ScriptEditorDialog(QDialog):
    def __init__(self, file_path, parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.is_new_file = not os.path.exists(file_path)
        self.setWindowTitle(f"📝 编辑脚本 - {os.path.basename(file_path)}")
        self.resize(800, 600)
        self.setMinimumSize(600, 400)

        # 主布局
        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        # 文件信息
        info_label = QLabel(f"📄 {os.path.basename(file_path)}")
        info_label.setStyleSheet("font-size: 14px; font-weight: bold; color: #1e293b;")
        layout.addWidget(info_label)

        # 编辑器
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas", 11))
        self.editor.setStyleSheet("""
            QPlainTextEdit {
                border: 2px solid #e2e8f0;
                border-radius: 8px;
                padding: 12px;
                background-color: #fafbfc;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 13px;
                line-height: 1.6;
            }
            QPlainTextEdit:focus {
                border-color: #818cf8;
            }
        """)

        # 加载内容
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    self.editor.setPlainText(content)
            except Exception as e:
                self.editor.setPlainText(f"# 读取文件失败: {str(e)}")
        else:
            # 新建文件模板
            template = f'''# -*- coding: utf-8 -*-
"""
{os.path.basename(file_path)}
Created: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
"""

import sys

def main():
    print("Hello, World!")

if __name__ == "__main__":
    main()
'''
            self.editor.setPlainText(template)

        layout.addWidget(self.editor)

        # 按钮
        button_box = QDialogButtonBox()
        save_btn = QPushButton("💾 保存")
        save_btn.setStyleSheet("""
            QPushButton {
                background-color: #10b981;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 24px;
                font-weight: bold;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #059669;
            }
        """)
        save_btn.clicked.connect(self.save_file)

        close_btn = QPushButton("❌ 关闭")
        close_btn.setStyleSheet("""
            QPushButton {
                background-color: #6b7280;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 24px;
                font-weight: bold;
                min-width: 100px;
            }
            QPushButton:hover {
                background-color: #4b5563;
            }
        """)
        close_btn.clicked.connect(self.reject)

        button_box.addButton(save_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        button_box.addButton(close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(button_box)

        self.setLayout(layout)

        # 应用样式
        self.setStyleSheet("""
            QDialog {
                background-color: #f8fafc;
            }
        """)

    def save_file(self):
        """保存文件"""
        try:
            content = self.editor.toPlainText()
            os.makedirs(os.path.dirname(self.file_path), exist_ok=True)

            with open(self.file_path, 'w', encoding='utf-8') as f:
                f.write(content)

            QMessageBox.information(self, "成功", f"✅ 文件已保存: {os.path.basename(self.file_path)}")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "错误", f"❌ 保存失败: {str(e)}")


# ===== 解释器定位（打包成 EXE 后 sys.executable 指向 EXE 自身，不能再拿来跑脚本） =====
_PYTHON_EXE_CACHE = None
_PYTHON_LOGGED = False


def _is_usable_python(exe):
    """实际启动一次，确认是 Python 3（排除 Microsoft Store 的占位别名）。"""
    try:
        res = subprocess.run(
            [exe, "-c", "import sys; print(sys.version_info[0])"],
            capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
        )
        return res.returncode == 0 and res.stdout.strip() == "3"
    except Exception:
        return False


def _venv_python_near(script_path, max_levels=6):
    """在脚本所在目录及上级目录寻找 .venv / venv，优先使用项目自带解释器。"""
    folder = os.path.dirname(os.path.abspath(script_path))
    for _ in range(max_levels):
        for name in (".venv", "venv", "env"):
            if sys.platform == 'win32':
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


def _registry_pythons():
    """读取 Windows 注册表中登记的 Python 安装路径（新版本优先）。"""
    found = []
    try:
        import winreg
    except ImportError:
        return found

    hives = [(winreg.HKEY_CURRENT_USER, winreg.KEY_READ)]
    for access in (winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
                   winreg.KEY_READ | winreg.KEY_WOW64_32KEY):
        hives.append((winreg.HKEY_LOCAL_MACHINE, access))

    for hive, access in hives:
        try:
            root = winreg.OpenKey(hive, r"SOFTWARE\Python\PythonCore", 0, access)
        except OSError:
            continue
        versions = []
        i = 0
        while True:
            try:
                versions.append(winreg.EnumKey(root, i))
                i += 1
            except OSError:
                break
        for ver in sorted(versions, reverse=True):
            try:
                key = winreg.OpenKey(root, ver + r"\InstallPath")
                exe = winreg.QueryValueEx(key, "ExecutablePath")[0]
                if exe:
                    found.append(exe)
            except OSError:
                continue
    return found


def resolve_python_interpreter(script_path=None):
    """返回运行脚本用的 Python 解释器路径；找不到返回 None。"""
    global _PYTHON_EXE_CACHE

    # 源码运行时，管理器自己的解释器就是最合适的
    if not getattr(sys, "frozen", False):
        return sys.executable

    # 1. 环境变量显式指定
    env_exe = os.environ.get("PYTHON_EXE")
    if env_exe and os.path.isfile(env_exe) and _is_usable_python(env_exe):
        return env_exe

    # 2. 脚本附近的项目虚拟环境（最贴合依赖）
    if script_path:
        venv_exe = _venv_python_near(script_path)
        if venv_exe:
            return venv_exe

    # 3. 全局解释器（缓存，避免每个脚本都扫描注册表）
    if _PYTHON_EXE_CACHE and os.path.isfile(_PYTHON_EXE_CACHE):
        return _PYTHON_EXE_CACHE

    candidates = _registry_pythons()
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    # 4. py 启动器兜底
    if sys.platform == 'win32':
        py_launcher = shutil.which("py")
        if py_launcher:
            try:
                res = subprocess.run(
                    [py_launcher, "-3", "-c", "import sys; print(sys.executable)"],
                    capture_output=True, text=True, timeout=8,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if res.returncode == 0 and res.stdout.strip():
                    candidates.append(res.stdout.strip())
            except Exception:
                pass

    current = os.path.normcase(os.path.abspath(sys.executable))
    for exe in candidates:
        if not exe or not os.path.isfile(exe):
            continue
        # 跳过 Microsoft Store 的 python.exe 占位别名
        if "windowsapps" in os.path.normcase(exe):
            continue
        if os.path.normcase(os.path.abspath(exe)) == current:
            continue
        if _is_usable_python(exe):
            _PYTHON_EXE_CACHE = exe
            return exe

    return None


def build_child_env():
    """构造子进程环境：去掉 PyInstaller / 管理器自带 Qt 注入的变量，避免干扰外部脚本。"""
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
    if not getattr(sys, "frozen", False):
        return env

    for key in list(env):
        if key.startswith("_PYI_") or key in ("_MEIPASS2", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
            env.pop(key, None)

    # 剔除 PyInstaller 解包目录（_MEIxxxx）及其子目录，例如 Qt6\bin
    mei_root = ""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mei_root = os.path.normcase(os.path.abspath(meipass))

    path_parts = []
    for part in env.get("PATH", "").split(os.pathsep):
        if not part:
            continue
        norm = os.path.normcase(os.path.abspath(part))
        if mei_root and (norm == mei_root or norm.startswith(mei_root + os.sep)):
            continue
        if os.path.basename(part.rstrip("\\/")).upper().startswith("_MEI"):
            continue
        path_parts.append(part)
    env["PATH"] = os.pathsep.join(path_parts)
    return env


# ===== 单任务线程 =====
class TaskThread(QThread):
    log_signal = pyqtSignal(str)
    status_signal = pyqtSignal(str, str)
    finished_signal = pyqtSignal(str)

    def __init__(self, file_path, name):
        super().__init__()
        self.file_path = file_path
        self.name = name
        self.process = None
        self.running = True

    def run(self):
        global _PYTHON_LOGGED
        self.status_signal.emit(self.name, "running")

        try:
            # 注意：打包成 EXE 后 sys.executable 是管理器自身，必须定位真正的 Python
            python_exe = resolve_python_interpreter(self.file_path)
            if not python_exe:
                self.log_signal.emit(
                    f"[{self.name}] ❌ 未找到可用的 Python 解释器。"
                    f"请安装 Python 3 并加入 PATH，或设置环境变量 PYTHON_EXE 指向 python.exe")
                self.status_signal.emit(self.name, "stopped")
                self.finished_signal.emit(self.name)
                return

            if not _PYTHON_LOGGED:
                self.log_signal.emit(f"[系统] 使用 Python 解释器: {python_exe}")
                _PYTHON_LOGGED = True

            script_dir = os.path.dirname(os.path.abspath(self.file_path)) or None
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0

            self.process = subprocess.Popen(
                [python_exe, self.file_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding='utf-8',
                errors='replace',
                cwd=script_dir,
                env=build_child_env(),
                creationflags=creationflags,
            )

            for line in self.process.stdout:
                if not self.running:
                    break
                line = line.strip()
                if line:
                    self.log_signal.emit(f"[{self.name}] {line}")

        except Exception as e:
            self.log_signal.emit(f"[{self.name}] 错误: {str(e)}")

        if self.process:
            self.process.wait()
            if self.process.returncode != 0 and self.running:
                self.log_signal.emit(f"[{self.name}] 进程退出代码: {self.process.returncode}")

        self.status_signal.emit(self.name, "stopped")
        self.finished_signal.emit(self.name)

    def stop(self):
        self.running = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except:
                try:
                    self.process.kill()
                except:
                    pass


# ===== 现代化样式表（移除不支持的 CSS 属性） =====
STYLE_SHEET = """
/* 全局样式 */
* {
    margin: 0;
    padding: 0;
}

QWidget {
    font-family: 'Segoe UI', 'Microsoft YaHei', 'PingFang SC', sans-serif;
    font-size: 13px;
    background-color: #f0f2f5;
}

/* 主窗口 */
QMainWindow, QWidget#mainWidget {
    background-color: #f8f9fa;
}

/* 分组框 - 现代化卡片设计 */
QGroupBox {
    font-weight: 600;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    margin-top: 16px;
    padding: 20px 16px 16px 16px;
    background-color: white;
}

QGroupBox::title {
    subcontrol-origin: margin;
    left: 16px;
    padding: 0 12px;
    color: #2c3e50;
    font-size: 14px;
    font-weight: 600;
    background-color: transparent;
}

/* 图标按钮样式 */
QToolButton {
    border: none;
    border-radius: 8px;
    padding: 8px;
    min-width: 40px;
    min-height: 40px;
    font-size: 18px;
    background-color: transparent;
}

QToolButton:hover {
    background-color: rgba(0, 0, 0, 0.05);
}

QToolButton:pressed {
    background-color: rgba(0, 0, 0, 0.1);
}

QToolButton:disabled {
    opacity: 0.4;
}

/* 运行图标按钮 */
QToolButton#btnRun {
    color: #059669;
}

QToolButton#btnRun:hover {
    background-color: #d1fae5;
}

/* 停止图标按钮 */
QToolButton#btnStop {
    color: #dc2626;
}

QToolButton#btnStop:hover {
    background-color: #fee2e2;
}

/* 新建图标按钮 */
QToolButton#btnNew {
    color: #2563eb;
}

QToolButton#btnNew:hover {
    background-color: #dbeafe;
}

/* 编辑图标按钮 */
QToolButton#btnEdit {
    color: #d97706;
}

QToolButton#btnEdit:hover {
    background-color: #fef3c7;
}

/* 删除图标按钮 */
QToolButton#btnDelete {
    color: #dc2626;
}

QToolButton#btnDelete:hover {
    background-color: #fee2e2;
}

/* 刷新图标按钮 */
QToolButton#btnRefresh {
    color: #0891b2;
}

QToolButton#btnRefresh:hover {
    background-color: #cffafe;
}

/* 选择文件夹按钮 - 保持文字 */
QPushButton#btnSelect {
    background-color: #8b5cf6;
    color: white;
    border: none;
    border-radius: 8px;
    padding: 10px 20px;
    font-weight: 600;
    font-size: 13px;
    min-width: 140px;
    min-height: 38px;
}

QPushButton#btnSelect:hover {
    background-color: #7c3aed;
}

QPushButton#btnSelect:pressed {
    background-color: #6d28d9;
}

/* 清空日志按钮 */
QPushButton#btnClearLog {
    background-color: #6b7280;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    min-width: 80px;
    min-height: 32px;
    font-size: 12px;
    font-weight: 600;
}

QPushButton#btnClearLog:hover {
    background-color: #4b5563;
}

QPushButton#btnClearLog:pressed {
    background-color: #374151;
}

/* 列表样式 - 现代化 */
QListWidget {
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    background-color: #fafbfc;
    padding: 6px;
    outline: none;
}

QListWidget::item {
    border-radius: 6px;
    padding: 12px 14px;
    margin: 3px 0px;
    background-color: white;
    border: 1px solid #f1f3f4;
}

QListWidget::item:selected {
    background-color: #eef2ff;
    border: 2px solid #818cf8;
    color: #1e293b;
}

QListWidget::item:hover {
    background-color: #f8fafc;
    border: 1px solid #d1d5db;
}

QListWidget::item:selected:hover {
    background-color: #eef2ff;
    border: 2px solid #818cf8;
}

/* 日志文本框 - 暗色主题 */
QTextEdit {
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    background-color: #1e293b;
    color: #e2e8f0;
    padding: 14px 16px;
    font-family: 'Cascadia Code', 'Consolas', 'Courier New', monospace;
    font-size: 12px;
    line-height: 1.6;
}

QTextEdit:focus {
    border: 1px solid #818cf8;
}

/* 状态标签 */
QLabel#statusLabel {
    color: #64748b;
    font-size: 13px;
    padding: 6px 12px;
    background-color: #f1f5f9;
    border-radius: 6px;
}

/* 目录标签 */
QLabel#dirLabel {
    background-color: #fafbfc;
    border: 2px dashed #d1d5db;
    border-radius: 8px;
    padding: 14px 18px;
    color: #6b7280;
    font-size: 13px;
}

/* 工具栏分隔线 */
QFrame#separator {
    background-color: #e5e7eb;
    max-height: 1px;
}

/* 滚动条样式 - 暗色主题日志区 */
QTextEdit QScrollBar:vertical {
    border: none;
    background: transparent;
    width: 8px;
    border-radius: 4px;
}

QTextEdit QScrollBar::handle:vertical {
    background: #475569;
    border-radius: 4px;
    min-height: 30px;
}

QTextEdit QScrollBar::handle:vertical:hover {
    background: #64748b;
}

QTextEdit QScrollBar::add-line:vertical,
QTextEdit QScrollBar::sub-line:vertical {
    height: 0px;
}

/* 列表滚动条 */
QListWidget QScrollBar:vertical {
    border: none;
    background: transparent;
    width: 6px;
    border-radius: 3px;
}

QListWidget QScrollBar::handle:vertical {
    background: #d1d5db;
    border-radius: 3px;
    min-height: 30px;
}

QListWidget QScrollBar::handle:vertical:hover {
    background: #9ca3af;
}

QListWidget QScrollBar::add-line:vertical,
QListWidget QScrollBar::sub-line:vertical {
    height: 0px;
}

/* 右键菜单样式 */
QMenu {
    background-color: white;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 4px;
}

QMenu::item {
    padding: 8px 24px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: #eef2ff;
    color: #1e293b;
}

QMenu::separator {
    height: 1px;
    background: #e5e7eb;
    margin: 4px 8px;
}

/* ToolTip 样式 */
QToolTip {
    background-color: #1e293b;
    color: #e2e8f0;
    border: none;
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 12px;
}
"""


# ===== 主界面 =====
class MultiScriptManager(QWidget):
    def __init__(self, initial_file=None):
        super().__init__()
        self.setWindowTitle("✨ 多脚本管理器 v1.0")
        self.resize(1100, 750)
        self.setMinimumSize(900, 600)
        self.setObjectName("mainWidget")

        # 设置样式
        self.setStyleSheet(STYLE_SHEET)

        # 主布局
        main_layout = QVBoxLayout()
        main_layout.setSpacing(16)
        main_layout.setContentsMargins(24, 24, 24, 24)

        # ===== 顶部：文件夹选择区 =====
        top_group = QGroupBox("📁 工作目录")
        top_layout = QVBoxLayout()
        top_layout.setSpacing(12)

        dir_layout = QHBoxLayout()
        dir_layout.setSpacing(12)

        self.dir_label = QLabel("📂 请选择包含 Python 脚本的文件夹")
        self.dir_label.setObjectName("dirLabel")
        self.dir_label.setWordWrap(True)

        self.btn_select = QPushButton("📂 浏览文件夹")
        self.btn_select.setObjectName("btnSelect")
        self.btn_select.setMinimumWidth(140)
        self.btn_select.clicked.connect(self.select_folder)

        dir_layout.addWidget(self.dir_label, 1)
        dir_layout.addWidget(self.btn_select)

        top_layout.addLayout(dir_layout)
        top_group.setLayout(top_layout)
        main_layout.addWidget(top_group)

        # ===== 中部：脚本列表和控制区 =====
        middle_splitter = QSplitter(Qt.Orientation.Horizontal)
        middle_splitter.setHandleWidth(4)
        middle_splitter.setStyleSheet("""
            QSplitter::handle {
                background: #e5e7eb;
                border-radius: 2px;
                margin: 20px 0px;
            }
            QSplitter::handle:hover {
                background: #818cf8;
            }
        """)

        # 左侧：脚本列表
        list_group = QGroupBox("📝 脚本列表")
        list_layout = QVBoxLayout()
        list_layout.setSpacing(10)

        # 脚本统计信息
        info_layout = QHBoxLayout()
        self.script_count_label = QLabel("📊 共 0 个脚本")
        self.script_count_label.setStyleSheet("color: #64748b; font-size: 12px;")
        self.running_count_label = QLabel("🟢 运行中: 0")
        self.running_count_label.setStyleSheet("color: #64748b; font-size: 12px;")
        info_layout.addWidget(self.script_count_label)
        info_layout.addStretch()
        info_layout.addWidget(self.running_count_label)
        list_layout.addLayout(info_layout)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list_widget.setAlternatingRowColors(False)
        self.list_widget.itemDoubleClicked.connect(self.run_script)
        self.list_widget.setMinimumWidth(280)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self.show_context_menu)

        list_layout.addWidget(self.list_widget)

        # ===== 工具栏 - 图标按钮 =====
        toolbar_layout = QHBoxLayout()
        toolbar_layout.setSpacing(4)
        toolbar_layout.setContentsMargins(0, 4, 0, 4)

        # 创建图标按钮
        self.btn_run = QToolButton()
        self.btn_run.setObjectName("btnRun")
        self.btn_run.setText("▶")
        self.btn_run.setToolTip("运行选中的脚本")
        self.btn_run.clicked.connect(self.run_script)
        self.btn_run.setMinimumSize(44, 44)
        self.btn_run.setFont(QFont("Segoe UI", 16))

        self.btn_stop = QToolButton()
        self.btn_stop.setObjectName("btnStop")
        self.btn_stop.setText("⏹")
        self.btn_stop.setToolTip("停止选中的脚本")
        self.btn_stop.clicked.connect(self.stop_script)
        self.btn_stop.setMinimumSize(44, 44)
        self.btn_stop.setFont(QFont("Segoe UI", 16))

        # 分隔符
        separator1 = QFrame()
        separator1.setFrameShape(QFrame.Shape.VLine)
        separator1.setFrameShadow(QFrame.Shadow.Sunken)
        separator1.setStyleSheet("background-color: #e5e7eb; max-width: 1px;")

        self.btn_new = QToolButton()
        self.btn_new.setObjectName("btnNew")
        self.btn_new.setText("➕")
        self.btn_new.setToolTip("创建新的 Python 脚本")
        self.btn_new.clicked.connect(self.create_new_script)
        self.btn_new.setMinimumSize(44, 44)
        self.btn_new.setFont(QFont("Segoe UI", 16))

        self.btn_edit = QToolButton()
        self.btn_edit.setObjectName("btnEdit")
        self.btn_edit.setText("✏️")
        self.btn_edit.setToolTip("编辑选中的脚本")
        self.btn_edit.clicked.connect(self.edit_script)
        self.btn_edit.setMinimumSize(44, 44)
        self.btn_edit.setFont(QFont("Segoe UI", 16))

        self.btn_delete = QToolButton()
        self.btn_delete.setObjectName("btnDelete")
        self.btn_delete.setText("🗑")
        self.btn_delete.setToolTip("删除选中的脚本")
        self.btn_delete.clicked.connect(self.delete_script)
        self.btn_delete.setMinimumSize(44, 44)
        self.btn_delete.setFont(QFont("Segoe UI", 16))

        self.btn_refresh = QToolButton()
        self.btn_refresh.setObjectName("btnRefresh")
        self.btn_refresh.setText("🔄")
        self.btn_refresh.setToolTip("刷新脚本列表（F5）：同步外部新增 / 删除的脚本")
        self.btn_refresh.clicked.connect(self.refresh_files)
        self.btn_refresh.setMinimumSize(44, 44)
        self.btn_refresh.setFont(QFont("Segoe UI", 16))

        # 添加到工具栏
        toolbar_layout.addWidget(self.btn_run)
        toolbar_layout.addWidget(self.btn_stop)
        toolbar_layout.addWidget(separator1)
        toolbar_layout.addWidget(self.btn_new)
        toolbar_layout.addWidget(self.btn_edit)
        toolbar_layout.addWidget(self.btn_delete)
        toolbar_layout.addWidget(self.btn_refresh)
        toolbar_layout.addStretch()

        # F5 快捷键刷新
        self.refresh_action = QAction("刷新脚本列表", self)
        self.refresh_action.setShortcut(QKeySequence("F5"))
        self.refresh_action.triggered.connect(self.refresh_files)
        self.addAction(self.refresh_action)

        # 提示标签
        tip_label = QLabel("💡 双击脚本快速运行")
        tip_label.setStyleSheet("color: #94a3b8; font-size: 12px; padding: 0 8px;")
        toolbar_layout.addWidget(tip_label)

        list_layout.addLayout(toolbar_layout)
        list_group.setLayout(list_layout)

        # 右侧：日志区
        log_group = QGroupBox("📋 运行日志")
        log_layout = QVBoxLayout()
        log_layout.setSpacing(10)

        # 日志控制栏
        log_control_layout = QHBoxLayout()
        log_control_layout.setSpacing(10)

        self.status_label = QLabel("⚪ 就绪")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setMinimumWidth(120)

        # 日志统计
        self.log_count_label = QLabel("📝 0 条日志")
        self.log_count_label.setStyleSheet("color: #94a3b8; font-size: 12px;")

        log_control_layout.addWidget(self.status_label)
        log_control_layout.addWidget(self.log_count_label)
        log_control_layout.addStretch()

        self.btn_clear_log = QPushButton("🗑 清空")
        self.btn_clear_log.setObjectName("btnClearLog")
        self.btn_clear_log.clicked.connect(self.clear_log)
        self.btn_clear_log.setToolTip("清空所有日志")

        log_control_layout.addWidget(self.btn_clear_log)

        log_layout.addLayout(log_control_layout)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("📌 运行日志将显示在这里...")

        log_layout.addWidget(self.log)
        log_group.setLayout(log_layout)

        middle_splitter.addWidget(list_group)
        middle_splitter.addWidget(log_group)
        middle_splitter.setSizes([380, 720])

        main_layout.addWidget(middle_splitter, 1)

        self.setLayout(main_layout)

        # 数据
        self.current_folder = ""
        self.tasks = {}  # file_name -> thread
        self.log_counter = 0
        self.pending_file = initial_file  # 待打开的文件
        self.config = load_config()

        # 目录监听：外部新增 / 删除脚本时自动刷新列表
        self.folder_watcher = QFileSystemWatcher(self)
        self.folder_watcher.directoryChanged.connect(self.on_folder_changed)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(400)
        self._refresh_timer.timeout.connect(self.auto_refresh_files)

        # 显示欢迎信息
        self.append_log("🚀 多脚本管理器 v1.0 已启动", "#10b981")
        self.append_log("💡 单实例模式：双击 .py 文件将在当前窗口打开", "#60a5fa")
        self.append_log("=" * 50 + "\n", "#94a3b8")

        # 如果有初始文件，延迟打开（等待窗口完全加载）；否则恢复上次目录
        if self.pending_file:
            QTimer.singleShot(500, self.open_pending_file)
        else:
            self.restore_last_folder()

    def restore_last_folder(self):
        """启动时恢复上次使用的工具目录"""
        folder = self.config.get("last_folder")
        if not isinstance(folder, str) or not folder.strip():
            return
        folder = folder.strip()
        if not os.path.isdir(folder):
            self.append_log(f"⚠️ 上次使用的目录已不存在，已忽略: {folder}", "#f59e0b")
            self.config.pop("last_folder", None)
            save_config(self.config)
            return
        self.apply_folder(folder)
        self.append_log(f"🕘 已恢复上次目录: {folder}", "#60a5fa")

    def remember_folder(self, folder):
        """记住当前工具目录，供下次启动恢复"""
        if not folder:
            return
        if self.config.get("last_folder") == folder:
            return
        self.config["last_folder"] = folder
        if not save_config(self.config):
            self.append_log("⚠️ 目录记忆保存失败（配置文件不可写）", "#f59e0b")

    def apply_folder(self, folder):
        """切换工作目录并刷新脚本列表"""
        self.current_folder = folder
        self.dir_label.setText(f"📂 {folder}")
        self.dir_label.setStyleSheet("""
            background-color: #f0fdf4;
            border: 2px solid #86efac;
            border-radius: 8px;
            padding: 14px 18px;
            color: #166534;
            font-size: 13px;
            font-weight: 500;
        """)
        self.load_files(folder)
        self.watch_folder(folder)

    def watch_folder(self, folder):
        """监听当前目录，外部增删脚本时触发自动刷新"""
        try:
            old_paths = self.folder_watcher.directories()
            if old_paths:
                self.folder_watcher.removePaths(old_paths)
            if folder and os.path.isdir(folder):
                self.folder_watcher.addPath(folder)
        except Exception:
            pass

    def on_folder_changed(self, path):
        """目录内容变化：防抖后刷新（避免一次操作触发多次）"""
        if os.path.normcase(path) == os.path.normcase(self.current_folder):
            self._refresh_timer.start()

    def auto_refresh_files(self):
        """目录被外部修改后自动刷新列表"""
        if not self.current_folder or not os.path.isdir(self.current_folder):
            return
        before = self.list_widget.count()
        self._reload_list(quiet=True)
        after = self.list_widget.count()
        if after != before:
            self.append_log(f"🔄 检测到目录变化，已自动刷新：{after} 个脚本", "#60a5fa")

    def refresh_files(self):
        """手动刷新脚本列表（刷新按钮 / F5）"""
        if not self.current_folder or not os.path.isdir(self.current_folder):
            self.append_log("⚠️ 当前目录不存在，无法刷新，请重新选择文件夹", "#f59e0b")
            return
        self._reload_list(quiet=False)
        self.append_log(f"🔄 已刷新脚本列表：{self.list_widget.count()} 个脚本", "#60a5fa")

    def _reload_list(self, quiet=True):
        """重新读取目录，并尽量保留原来的选中项"""
        selected = {
            item.data(Qt.ItemDataRole.UserRole)
            for item in self.list_widget.selectedItems()
        }
        current = self.list_widget.currentItem()
        current_name = current.data(Qt.ItemDataRole.UserRole) if current else None

        self.load_files(self.current_folder, quiet=quiet)

        current_item = None
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            name = item.data(Qt.ItemDataRole.UserRole)
            if name in selected:
                item.setSelected(True)
            if name == current_name:
                current_item = item
        if current_item is not None:
            self.list_widget.setCurrentItem(current_item)
            self.list_widget.scrollToItem(current_item)

    def bring_to_front(self):
        """把窗口从最小化恢复并置前"""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.activateWindow()
        self.raise_()

    def handle_message(self, data):
        """处理第二个实例发来的消息"""
        if data == MSG_REFRESH:
            self.bring_to_front()
            self.refresh_files()
        else:
            self.handle_file_open(data)

    def open_pending_file(self):
        """延迟打开文件或目录"""
        if not self.pending_file:
            return
        path = self.pending_file
        self.pending_file = None
        if os.path.isdir(path):
            self.apply_folder(path)
            self.remember_folder(path)
            self.append_log(f"✅ 已切换到目录: {path}", "#10b981")
        elif os.path.isfile(path) and path.lower().endswith('.py'):
            self.open_script_file(path)
        else:
            self.append_log(f"⚠️ 无法打开: {path}", "#f59e0b")

    # ===== 处理从其他实例传来的文件 =====
    def handle_file_open(self, file_path):
        """处理从其他实例传来的文件打开请求"""
        if not file_path or not os.path.exists(file_path):
            return

        # 激活窗口
        self.bring_to_front()

        # 打开文件
        if file_path.lower().endswith('.py'):
            self.open_script_file(file_path)
        elif os.path.isdir(file_path):
            self.apply_folder(file_path)
            self.remember_folder(file_path)
            self.append_log(f"✅ 已切换到目录: {file_path}", "#10b981")

    # ===== 打开脚本文件 =====
    def open_script_file(self, file_path):
        """打开指定的脚本文件（用于文件关联）"""
        if not os.path.exists(file_path):
            self.append_log(f"❌ 文件不存在: {file_path}", "#ef4444")
            return

        # 设置工作目录为文件所在目录
        folder = os.path.dirname(file_path)
        if not folder:
            folder = os.getcwd()

        self.apply_folder(folder)
        self.remember_folder(folder)
        self.append_log(f"✅ 已切换到目录: {folder}", "#10b981")
        self.append_log(f"📄 打开文件: {os.path.basename(file_path)}", "#60a5fa")

        # 在列表中选中该文件
        file_name = os.path.basename(file_path)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == file_name:
                self.list_widget.setCurrentItem(item)
                # 滚动到该项
                self.list_widget.scrollToItem(item)
                break

    # ===== 增强日志功能 =====
    def append_log(self, text, color=None):
        """增强的日志添加功能"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        if color:
            formatted_text = f'<span style="color: {color};">[{timestamp}] {text}</span>'
        else:
            formatted_text = f'[{timestamp}] {text}'

        self.log.append(formatted_text)
        self.log_counter += 1
        self.log_count_label.setText(f"📝 {self.log_counter} 条日志")

        # 自动滚动到底部
        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    # ===== 选择目录 =====
    def select_folder(self):
        start_dir = self.current_folder if os.path.isdir(self.current_folder) else ""
        folder = QFileDialog.getExistingDirectory(
            self,
            "选择包含 Python 脚本的文件夹",
            start_dir,
            options=QFileDialog.Option.ShowDirsOnly
        )
        if folder:
            self.apply_folder(folder)
            self.remember_folder(folder)
            self.append_log(f"✅ 已切换到目录: {folder}", "#10b981")

    def load_files(self, folder, quiet=False):
        self.list_widget.clear()
        py_files = []

        try:
            for f in os.listdir(folder):
                # 只展示真正的 *.py 文件（排除目录、其他后缀）
                if not f.lower().endswith(".py"):
                    continue
                if not os.path.isfile(os.path.join(folder, f)):
                    continue
                py_files.append(f)

            py_files.sort()

            for f in py_files:
                item = QListWidgetItem(f"🐍 {f}")
                item.setData(Qt.ItemDataRole.UserRole, f)
                item.setToolTip(f"双击运行 {f}")
                self.list_widget.addItem(item)

            count = len(py_files)
            self.script_count_label.setText(f"📊 共 {count} 个脚本")

            if count == 0 and not quiet:
                self.append_log("⚠️ 该文件夹中没有找到 .py 脚本文件", "#f59e0b")

        except Exception as e:
            self.append_log(f"❌ 加载文件夹失败: {str(e)}", "#ef4444")

    # ===== 创建新脚本 =====
    def create_new_script(self):
        if not self.current_folder:
            QMessageBox.warning(self, "提示", "请先选择工作目录")
            return

        name, ok = QInputDialog.getText(
            self,
            "新建脚本",
            "请输入脚本名称：",
            QLineEdit.EchoMode.Normal,
            "new_script.py"
        )

        if not ok or not name:
            return

        if not name.endswith(".py"):
            name += ".py"

        file_path = os.path.join(self.current_folder, name)

        if os.path.exists(file_path):
            reply = QMessageBox.question(
                self,
                "文件已存在",
                f"文件 {name} 已存在，是否覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        editor = ScriptEditorDialog(file_path, self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            self.load_files(self.current_folder)
            self.append_log(f"✅ 已创建脚本: {name}", "#10b981")

            for i in range(self.list_widget.count()):
                item = self.list_widget.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == name:
                    self.list_widget.setCurrentItem(item)
                    break

    # ===== 编辑脚本 =====
    def edit_script(self):
        selected_items = self.list_widget.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "提示", "请先选择要编辑的脚本")
            return

        item = selected_items[0]
        name = item.data(Qt.ItemDataRole.UserRole)
        file_path = os.path.join(self.current_folder, name)

        if not os.path.exists(file_path):
            QMessageBox.warning(self, "错误", f"文件不存在: {name}")
            return

        if name in self.tasks and self.tasks[name].isRunning():
            reply = QMessageBox.question(
                self,
                "脚本正在运行",
                f"脚本 {name} 正在运行，编辑后需要重新启动才能生效。\n是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

        editor = ScriptEditorDialog(file_path, self)
        if editor.exec() == QDialog.DialogCode.Accepted:
            self.append_log(f"✅ 已更新脚本: {name}", "#10b981")

    # ===== 删除脚本 =====
    def delete_script(self):
        selected_items = self.list_widget.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "提示", "请先选择要删除的脚本")
            return

        files_to_delete = []
        for item in selected_items:
            name = item.data(Qt.ItemDataRole.UserRole)
            files_to_delete.append(name)

        msg = f"确定要删除以下 {len(files_to_delete)} 个脚本吗？\n\n"
        msg += "\n".join(f"  • {name}" for name in files_to_delete)
        msg += "\n\n⚠️ 此操作不可恢复！"

        reply = QMessageBox.question(
            self,
            "确认删除",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.No:
            return

        running_files = [name for name in files_to_delete if name in self.tasks and self.tasks[name].isRunning()]
        if running_files:
            msg = f"以下脚本正在运行，需要先停止：\n\n"
            msg += "\n".join(f"  • {name}" for name in running_files)
            msg += "\n\n是否停止并删除？"

            reply = QMessageBox.question(
                self,
                "脚本正在运行",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                return

            for name in running_files:
                if name in self.tasks:
                    self.tasks[name].stop()
                    self.tasks[name].wait()
                    del self.tasks[name]

        deleted_count = 0
        for name in files_to_delete:
            file_path = os.path.join(self.current_folder, name)
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                    deleted_count += 1
                    self.append_log(f"🗑 已删除: {name}", "#ef4444")
            except Exception as e:
                self.append_log(f"❌ 删除失败 {name}: {str(e)}", "#ef4444")

        self.load_files(self.current_folder)
        self.update_status_label()

        if deleted_count > 0:
            self.append_log(f"✅ 已删除 {deleted_count} 个脚本", "#10b981")

    # ===== 右键菜单 =====
    def show_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if not item:
            return

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: white;
                border: 1px solid #e5e7eb;
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                padding: 8px 24px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #eef2ff;
            }
        """)

        run_action = menu.addAction("▶ 运行")
        run_action.triggered.connect(self.run_script)

        stop_action = menu.addAction("⏹ 停止")
        stop_action.triggered.connect(self.stop_script)

        menu.addSeparator()

        edit_action = menu.addAction("✏️ 编辑")
        edit_action.triggered.connect(self.edit_script)

        delete_action = menu.addAction("🗑 删除")
        delete_action.triggered.connect(self.delete_script)

        menu.addSeparator()

        name = item.data(Qt.ItemDataRole.UserRole)
        info_action = menu.addAction(f"📄 {name}")
        info_action.setEnabled(False)

        menu.exec(self.list_widget.mapToGlobal(pos))

    # ===== 运行 =====
    def run_script(self):
        selected_items = self.list_widget.selectedItems()
        if not selected_items:
            QMessageBox.information(
                self,
                "提示",
                "请先选择要运行的脚本\n💡 双击脚本可直接运行",
                QMessageBox.StandardButton.Ok
            )
            return

        started_count = 0
        for item in selected_items:
            name = item.data(Qt.ItemDataRole.UserRole)
            if not name:
                continue

            file_path = os.path.join(self.current_folder, name)

            if not os.path.exists(file_path):
                self.append_log(f"❌ 文件不存在: {file_path}", "#ef4444")
                continue

            if name in self.tasks and self.tasks[name].isRunning():
                self.append_log(f"⚠️ {name} 已在运行中", "#f59e0b")
                continue

            thread = TaskThread(file_path, name)
            thread.log_signal.connect(self.update_log)
            thread.status_signal.connect(self.update_status)
            thread.finished_signal.connect(self.on_task_finished)

            self.tasks[name] = thread
            thread.start()

            item.setText(f"▶ {name}")
            item.setForeground(QBrush(QColor("#059669")))
            item.setToolTip(f"🟢 正在运行: {name}")
            started_count += 1

        if started_count > 0:
            self.append_log(f"🚀 已启动 {started_count} 个脚本", "#10b981")

        self.update_status_label()

    # ===== 停止 =====
    def stop_script(self):
        selected_items = self.list_widget.selectedItems()
        if not selected_items:
            QMessageBox.information(self, "提示", "请先选择要停止的脚本")
            return

        stopped_count = 0
        for item in selected_items:
            name = item.data(Qt.ItemDataRole.UserRole)
            if name and name in self.tasks:
                self.tasks[name].stop()
                self.tasks[name].wait()
                del self.tasks[name]
                self.append_log(f"⏹️ {name} 已手动停止", "#ef4444")

                item.setText(f"■ {name}")
                item.setForeground(QBrush(QColor("#6b7280")))
                item.setToolTip(f"双击运行 {name}")
                stopped_count += 1

        if stopped_count > 0:
            self.append_log(f"🛑 已停止 {stopped_count} 个脚本", "#ef4444")

        self.update_status_label()

    # ===== 清理日志 =====
    def clear_log(self):
        reply = QMessageBox.question(
            self,
            "确认清空",
            "确定要清空所有日志吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.log.clear()
            self.log_counter = 0
            self.log_count_label.setText("📝 0 条日志")
            self.append_log("🗑 日志已清空", "#94a3b8")

    # ===== 回调函数 =====
    def update_log(self, text):
        self.append_log(text)

    def update_status(self, script_name, status):
        emotions = {
            "running": "▶",
            "stopped": "■",
            "error": "⚠"
        }
        emoji = emotions.get(status, "■")
        status_text = {
            "running": "🟢 运行中",
            "stopped": "⏹ 已停止",
            "error": "❌ 错误"
        }
        text = status_text.get(status, "未知")

        if status == "running":
            color = "#10b981"
        elif status == "stopped":
            color = "#6b7280"
        else:
            color = "#ef4444"

        self.append_log(f"{emoji} {script_name}: {text}", color)

    def on_task_finished(self, script_name):
        if script_name in self.tasks:
            del self.tasks[script_name]

        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == script_name:
                item.setText(f"■ {script_name}")
                item.setForeground(QBrush(QColor("#6b7280")))
                item.setToolTip(f"双击运行 {script_name}")
                break

        self.update_status_label()

    def update_status_label(self):
        running_count = len([t for t in self.tasks.values() if t.isRunning()])
        self.running_count_label.setText(f"🟢 运行中: {running_count}")

        if running_count > 0:
            self.status_label.setText(f"🟢 运行中: {running_count} 个脚本")
            self.status_label.setStyleSheet("""
                background-color: #d1fae5;
                color: #065f46;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 13px;
                font-weight: 600;
            """)
        else:
            self.status_label.setText("⚪ 就绪")
            self.status_label.setStyleSheet("""
                background-color: #f1f5f9;
                color: #64748b;
                border-radius: 6px;
                padding: 6px 12px;
                font-size: 13px;
            """)

    # ===== 窗口关闭事件 =====
    def closeEvent(self, event):
        running_tasks = [name for name, task in self.tasks.items() if task.isRunning()]
        if running_tasks:
            reply = QMessageBox.question(
                self,
                "确认退出",
                f"还有 {len(running_tasks)} 个脚本在运行中：\n\n" +
                "\n".join(f"  • {name}" for name in running_tasks) +
                "\n\n是否停止并退出？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes
            )
            if reply == QMessageBox.StandardButton.Yes:
                for name in list(self.tasks.keys()):
                    if self.tasks[name].isRunning():
                        self.tasks[name].stop()
                        self.tasks[name].wait()
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


# ===== 入口 =====
if __name__ == "__main__":
    app = QApplication(sys.argv)

    font = QFont("Microsoft YaHei", 10)
    app.setFont(font)

    app.setStyle('Fusion')

    # 单实例管理
    instance_manager = SingleInstanceManager()

    # 检查命令行参数
    initial_file = None
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
        if os.path.exists(file_path) and file_path.lower().endswith('.py'):
            initial_file = file_path
        elif os.path.isdir(file_path):
            initial_file = file_path

    # 尝试获取单实例锁
    if not instance_manager.try_lock():
        # 已有实例在运行：有文件/目录就打开，否则让它刷新当前列表并置前
        if initial_file:
            instance_manager.send_file_to_existing(initial_file)
        else:
            instance_manager.send_message_to_existing(MSG_REFRESH)
        # 退出当前实例
        sys.exit(0)

    # 创建主窗口
    window = MultiScriptManager(initial_file)
    instance_manager.window = window
    window.show()

    sys.exit(app.exec())