#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""小云雀 SQL 查询工具（PyQt6）

特点：
- 数据库连接与查询语句全部保存在配置文件中，可自由新增 / 编辑 / 删除
- 首次运行使用示例配置（占位主机/库名/表名），请在界面中改成自己的数据库信息
- 界面中的修改会写入配置文件并优先生效；改代码里的默认示例不会覆盖已保存的配置，
  需要重置时点「恢复示例」（会自动备份原配置文件）
- 查询参数由 SQL 自动推导：%(name)s 生成命名参数输入框，%s 生成位置参数输入框
- 查询结果以表格展示；结果中含 Cookie 时可一键转换为 Cookie-Editor 格式并导入浏览器

配置文件位置：
- 默认：%APPDATA%\\小云雀查询\\config.json
- 可通过环境变量 XYQ_CONFIG 指定其它路径
"""

import csv
import json
import os
import sys
import time
import webbrowser
from datetime import datetime

import pymysql

from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QGroupBox, QTabWidget, QPlainTextEdit, QSplitter,
    QMessageBox, QComboBox, QFileDialog, QDialog, QDialogButtonBox,
    QFormLayout, QSpinBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView
)

# ==================== 常量 ====================
APP_NAME = "小云雀查询"
APP_TITLE = "小云雀 SQL 查询工具"
CONFIG_ENV = "XYQ_CONFIG"
MAX_ROWS = 1000
DEFAULT_BROWSER_URL = "https://xyq.jianying.com/home"
CHARSET_CHOICES = ["utf8mb4", "utf8", "gbk", "latin1"]

STATUS_COLORS = {
    "idle": "#909399",
    "info": "#409eff",
    "ok": "#67c23a",
    "warn": "#e6a23c",
    "error": "#f56c6c",
}


# ==================== 配置 ====================
def config_path():
    """配置文件路径（可用环境变量 XYQ_CONFIG 覆盖）"""
    override = os.environ.get(CONFIG_ENV, "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME, "config.json")


def default_databases():
    """初始数据库示例：请填写自己的连接信息，之后可自行增删"""
    return [
        {
            "name": "示例数据库",
            "host": "127.0.0.1",
            "port": 3306,
            "user": "root",
            "password": "",
            "database": "your_database",
            "charset": "utf8mb4",
        }
    ]


def default_queries():
    """初始查询语句：仅作为示例，可在界面中修改或删除"""
    return [
        {
            "name": "按手机号查 Cookie",
            "db": "示例数据库",
            "description": "按手机号取最新一条记录，结果会自动加载到 Cookie-Editor 标签页",
            "sql": (
                "SELECT id, mobile, uid, cookie\n"
                "FROM your_table\n"
                "WHERE mobile = %(mobile)s\n"
                "ORDER BY id DESC\n"
                "LIMIT 1"
            ),
        },
        {
            "name": "按 UID 查 Cookie",
            "db": "示例数据库",
            "description": "按用户 UID 取最新一条记录，结果会自动加载到 Cookie-Editor 标签页",
            "sql": (
                "SELECT id, mobile, uid, cookie\n"
                "FROM your_table\n"
                "WHERE uid = %(uid)s\n"
                "ORDER BY id DESC\n"
                "LIMIT 1"
            ),
        },
        {
            "name": "按手机号查最近 50 条",
            "db": "示例数据库",
            "description": "按手机号列出最近的记录（示例，可自行修改 SQL）",
            "sql": (
                "SELECT id, mobile, uid, create_time\n"
                "FROM your_table\n"
                "WHERE mobile = %(mobile)s\n"
                "ORDER BY id DESC\n"
                "LIMIT 50"
            ),
        },
    ]


def default_config():
    return {
        "version": 1,
        "last_db": "示例数据库",
        "last_query": "按手机号查 Cookie",
        "browser_url": DEFAULT_BROWSER_URL,
        "databases": default_databases(),
        "queries": default_queries(),
    }


def _text(value, default=""):
    if value is None:
        return default
    return str(value)


def _unique_name(name, used):
    """used 为小写名称集合，返回不重复的名称并登记"""
    candidate = name
    n = 2
    while candidate.lower() in used:
        candidate = f"{name}{n}"
        n += 1
    used.add(candidate.lower())
    return candidate


def normalize_config(raw):
    """把任意结构整理成可用配置，容错处理损坏/旧版数据"""
    if not isinstance(raw, dict):
        raw = {}

    # ---- 数据库 ----
    dbs = []
    used = set()
    raw_dbs = raw.get("databases")
    for i, item in enumerate(raw_dbs if raw_dbs is not None else default_databases(), 1):
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name")).strip() or f"数据库{i}"
        name = _unique_name(name, used)
        try:
            port = int(item.get("port") or 3306)
        except (TypeError, ValueError):
            port = 3306
        if not 0 < port < 65536:
            port = 3306
        charset = _text(item.get("charset")).strip() or "utf8mb4"
        dbs.append({
            "name": name,
            "host": _text(item.get("host")).strip(),
            "port": port,
            "user": _text(item.get("user")).strip(),
            "password": _text(item.get("password")),
            "database": _text(item.get("database")).strip(),
            "charset": charset,
        })
    if raw_dbs is None and not dbs:
        dbs = default_databases()

    db_names = [db["name"] for db in dbs]
    fallback_db = db_names[0] if db_names else ""

    # ---- 查询语句 ----
    queries = []
    used_q = set()
    raw_queries = raw.get("queries")
    for i, item in enumerate(raw_queries if raw_queries is not None else default_queries(), 1):
        if not isinstance(item, dict):
            continue
        sql = _text(item.get("sql")).strip()
        if not sql:
            continue
        name = _text(item.get("name")).strip() or f"查询{i}"
        name = _unique_name(name, used_q)
        db = _text(item.get("db")).strip()
        if db not in db_names:
            db = fallback_db
        queries.append({
            "name": name,
            "db": db,
            "description": _text(item.get("description")).strip(),
            "sql": sql,
        })
    if raw_queries is None and not queries:
        queries = default_queries()

    cfg = {
        "version": 1,
        "last_db": _text(raw.get("last_db")).strip(),
        "last_query": _text(raw.get("last_query")).strip(),
        "browser_url": _text(raw.get("browser_url")).strip() or DEFAULT_BROWSER_URL,
        "databases": dbs,
        "queries": queries,
    }
    if cfg["last_db"] not in db_names:
        cfg["last_db"] = db_names[0] if db_names else ""
    query_names = [q["name"] for q in queries]
    if cfg["last_query"] not in query_names:
        cfg["last_query"] = query_names[0] if query_names else ""
    return cfg


def save_config(cfg):
    path = config_path()
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_config():
    """读取配置；文件损坏时备份并重建默认配置"""
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return normalize_config(json.load(f))
        except Exception:
            backup = f"{path}.bak-{datetime.now():%Y%m%d%H%M%S}"
            try:
                os.replace(path, backup)
            except OSError:
                pass
    cfg = normalize_config(default_config())
    try:
        save_config(cfg)
    except Exception:
        pass
    return cfg


# ==================== 通用工具 ====================
def make_db_kwargs(db, timeout=10):
    """把配置里的数据库字典转成 pymysql.connect 参数"""
    kwargs = {
        "host": _text(db.get("host")).strip(),
        "port": int(db.get("port") or 3306),
        "user": _text(db.get("user")).strip(),
        "password": _text(db.get("password")),
        "charset": _text(db.get("charset")).strip() or "utf8mb4",
        "connect_timeout": timeout,
        "read_timeout": timeout * 3,
        "write_timeout": timeout * 3,
        "autocommit": True,
    }
    db_name = _text(db.get("database")).strip()
    if db_name:
        kwargs["database"] = db_name
    return kwargs


def parse_sql_placeholders(sql):
    """解析 SQL 中的 pymysql 占位符。

    返回 (命名参数列表, 位置参数个数)：
    - %(mobile)s -> 命名参数 mobile
    - %s         -> 位置参数
    - %%         -> 转义的字面量百分号
    """
    named = []
    positional = 0
    i = 0
    n = len(sql or "")
    while i < n:
        if sql[i] != "%":
            i += 1
            continue
        if i + 1 >= n:
            break
        nxt = sql[i + 1]
        if nxt == "%":
            i += 2
            continue
        if nxt == "s":
            positional += 1
            i += 2
            continue
        if nxt == "(":
            end = sql.find(")s", i + 2)
            if end == -1:
                i += 1
                continue
            name = sql[i + 2:end].strip()
            if name and name not in named:
                named.append(name)
            i = end + 2
            continue
        i += 1
    return named, positional


def cell_text(value):
    """把数据库返回值安全转成表格文本"""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(value).hex()
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def parse_cookie_value(value):
    """把结果单元格解析为 Cookie 列表（JSON 数组 / {cookies:[...]} / 单个 Cookie）"""
    data = value
    if isinstance(data, (bytes, bytearray)):
        data = cell_text(data)
    for _ in range(2):  # 兼容被二次编码的 JSON 字符串
        if not isinstance(data, str):
            break
        text = data.strip()
        if not text:
            raise ValueError("Cookie 内容为空")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Cookie 不是合法 JSON（{exc.msg}）") from exc
    if isinstance(data, dict):
        if isinstance(data.get("cookies"), list):
            data = data["cookies"]
        elif "name" in data and "value" in data:
            data = [data]
        else:
            raise ValueError("Cookie 对象缺少 cookies / name 字段")
    if not isinstance(data, list):
        raise ValueError("Cookie 结构不支持（应为数组或对象）")
    cookies = [item for item in data if isinstance(item, dict) and ("name" in item or "value" in item)]
    if not cookies:
        raise ValueError("Cookie 列表为空或格式不正确")
    return cookies


def same_site_for_editor(same_site):
    """转成 Cookie-Editor 导出的取值：lax / strict / no_restriction / null"""
    text = _text(same_site).strip().lower()
    if text == "lax":
        return "lax"
    if text == "strict":
        return "strict"
    if text in ("none", "no_restriction"):
        return "no_restriction"
    return None


def cookie_expiry(cookie):
    """读取 Cookie 的过期时间戳；会话 Cookie 返回 None"""
    expires = cookie.get("expires", cookie.get("expirationDate"))
    if expires is None or isinstance(expires, bool):
        return None
    if isinstance(expires, str):
        text = expires.strip()
        if not text:
            return None
        try:
            expires = float(text)
        except ValueError:
            try:
                expires = datetime.fromisoformat(text).timestamp()
            except ValueError:
                return None
    try:
        expires = float(expires)
    except (TypeError, ValueError):
        return None
    if expires <= 0:
        return None
    return expires


def cookie_to_editor(cookies, now=None):
    """转换为 Cookie-Editor 扩展可导入的格式（与其自身导出格式保持一致）。

    Cookie-Editor 通过 chrome.cookies.set 写入 Cookie，因此这里按 Chrome 的规则做处理：
    - 已过期的 Cookie 无法写入（Chrome 会丢弃 / 报 Failed to parse or set cookie），直接跳过；
    - 会话 Cookie 只给 session=True，不给 expirationDate；
    - SameSite=None 必须同时 Secure，否则 Chrome 拒绝；
    - 带 hostOnly 标记，保证 host-only Cookie 写回原主机而不是变成域 Cookie；
    - 未指定 SameSite 时输出 null（与 Cookie-Editor 导出一致）。
    """
    if now is None:
        now = time.time()
    result = []
    for c in cookies:
        name = _text(c.get("name"))
        domain = _text(c.get("domain")).strip()
        if not name or not domain:
            continue
        expires = cookie_expiry(c)
        if expires is not None and expires <= now:
            # 过期 Cookie 写入必然失败，保留原始数据在「原始JSON」页，导入时跳过
            continue

        secure = bool(c.get("secure", False))
        same_site = same_site_for_editor(c.get("sameSite"))
        if same_site == "no_restriction":
            secure = True

        entry = {"domain": domain}
        if expires is not None:
            entry["expirationDate"] = round(expires, 6)
        entry.update({
            "hostOnly": not domain.startswith("."),
            "httpOnly": bool(c.get("httpOnly", False)),
            "name": name,
            "path": _text(c.get("path")).strip() or "/",
            "sameSite": same_site,
            "secure": secure,
            "session": expires is None,
            "storeId": None,
            "value": _text(c.get("value")),
        })
        result.append(entry)
    return result


def import_ready_count(cookies, now=None):
    """当前数据里可以直接导入 Cookie-Editor 的条数"""
    return len(cookie_to_editor(cookies, now=now))


def format_cookie_display(cookies):
    now = time.time()
    lines = []
    for i, c in enumerate(cookies, 1):
        domain = _text(c.get("domain"))
        lines.append("=" * 64)
        lines.append(f"Cookie {i}:")
        lines.append(f"  名称: {_text(c.get('name'), 'N/A')}")
        lines.append(f"  值: {_text(c.get('value'), 'N/A')}")
        lines.append(f"  域名: {_text(c.get('domain'), 'N/A')}"
                     + (f"（{'host-only' if not domain.startswith('.') else '域 Cookie'}）"
                        if domain else ""))
        lines.append(f"  路径: {_text(c.get('path'), 'N/A')}")
        lines.append(f"  安全: {bool(c.get('secure', False))}")
        lines.append(f"  HTTP Only: {bool(c.get('httpOnly', False))}")
        lines.append(f"  SameSite: {_text(c.get('sameSite'), 'N/A')}")
        ts = cookie_expiry(c)
        if ts is None:
            lines.append("  过期时间: 会话 Cookie")
        else:
            try:
                stamp = f"{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M:%S}"
            except (OSError, OverflowError, ValueError):
                stamp = str(ts)
            if ts <= now:
                lines.append(f"  过期时间: {stamp}（已过期，导入时跳过）")
            else:
                lines.append(f"  过期时间: {stamp}")
        lines.append("")
    return "\n".join(lines)


# ==================== 查询线程 ====================
class QueryWorker(QThread):
    succeeded = pyqtSignal(list, list, int)   # 列名, 行, 总行数/影响行数
    failed = pyqtSignal(str)

    def __init__(self, db, sql, params=None, parent=None):
        super().__init__(parent)
        self.db = dict(db or {})
        self.sql = sql
        self.params = params

    def run(self):
        conn = None
        try:
            conn = pymysql.connect(**make_db_kwargs(self.db))
            with conn.cursor() as cursor:
                cursor.execute(self.sql, self.params if self.params is not None else None)
                if cursor.description:
                    columns = [str(d[0]) for d in cursor.description]
                    total = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
                    rows = cursor.fetchmany(MAX_ROWS)
                    self.succeeded.emit(columns, [list(r) for r in rows], total or len(rows))
                else:
                    affected = cursor.rowcount if cursor.rowcount is not None else 0
                    self.succeeded.emit(["影响行数"], [[affected]], affected)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


# ==================== 数据库编辑对话框 ====================
class DatabaseDialog(QDialog):
    def __init__(self, parent=None, db=None, used_names=None):
        super().__init__(parent)
        self.setWindowTitle("编辑数据库" if db else "新增数据库")
        self.setMinimumWidth(520)
        self.used_names = {n.lower() for n in (used_names or []) if n}
        self.result_data = None
        self._test_worker = None
        self._build_ui(db or {})

    def _build_ui(self, db):
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        form = QFormLayout()
        form.setSpacing(8)

        self.name_edit = QLineEdit(_text(db.get("name")))
        self.name_edit.setPlaceholderText("例如：示例数据库")
        form.addRow("名称：", self.name_edit)

        self.host_edit = QLineEdit(_text(db.get("host"), "127.0.0.1"))
        self.host_edit.setPlaceholderText("MySQL 主机地址")
        form.addRow("主机：", self.host_edit)

        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        try:
            self.port_spin.setValue(int(db.get("port") or 3306))
        except (TypeError, ValueError):
            self.port_spin.setValue(3306)
        form.addRow("端口：", self.port_spin)

        self.user_edit = QLineEdit(_text(db.get("user")))
        form.addRow("用户名：", self.user_edit)

        pwd_row = QWidget()
        pwd_layout = QHBoxLayout(pwd_row)
        pwd_layout.setContentsMargins(0, 0, 0, 0)
        pwd_layout.setSpacing(6)
        self.pwd_edit = QLineEdit(_text(db.get("password")))
        self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_pwd_btn = QPushButton("显示")
        self.show_pwd_btn.setCheckable(True)
        self.show_pwd_btn.setFixedWidth(60)
        self.show_pwd_btn.toggled.connect(self._toggle_password)
        pwd_layout.addWidget(self.pwd_edit, 1)
        pwd_layout.addWidget(self.show_pwd_btn)
        form.addRow("密码：", pwd_row)

        self.db_edit = QLineEdit(_text(db.get("database")))
        self.db_edit.setPlaceholderText("默认库名，可留空")
        form.addRow("数据库：", self.db_edit)

        self.charset_combo = QComboBox()
        self.charset_combo.setEditable(True)
        self.charset_combo.addItems(CHARSET_CHOICES)
        charset = _text(db.get("charset")).strip() or "utf8mb4"
        self.charset_combo.setCurrentText(charset)
        form.addRow("字符集：", self.charset_combo)

        layout.addLayout(form)

        test_row = QHBoxLayout()
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self._test_connection)
        self.test_status = QLabel("")
        self.test_status.setWordWrap(True)
        self.test_status.setStyleSheet("color: #909399;")
        test_row.addWidget(self.test_btn)
        test_row.addWidget(self.test_status, 1)
        layout.addLayout(test_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_password(self, checked):
        self.pwd_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def _collect(self):
        return {
            "name": self.name_edit.text().strip(),
            "host": self.host_edit.text().strip(),
            "port": self.port_spin.value(),
            "user": self.user_edit.text().strip(),
            "password": self.pwd_edit.text(),
            "database": self.db_edit.text().strip(),
            "charset": self.charset_combo.currentText().strip() or "utf8mb4",
        }

    def _on_accept(self):
        if self._test_worker is not None and self._test_worker.isRunning():
            QMessageBox.information(self, "提示", "正在测试连接，请稍候…")
            return
        db = self._collect()
        if not db["name"]:
            QMessageBox.warning(self, "提示", "请填写数据库名称")
            return
        if db["name"].lower() in self.used_names:
            QMessageBox.warning(self, "提示", f"名称「{db['name']}」已存在，请换一个")
            return
        if not db["host"]:
            QMessageBox.warning(self, "提示", "请填写主机地址")
            return
        if not db["user"]:
            QMessageBox.warning(self, "提示", "请填写用户名")
            return
        self.result_data = db
        self.accept()

    def _test_connection(self):
        if self._test_worker is not None and self._test_worker.isRunning():
            return
        db = self._collect()
        if not db["host"] or not db["user"]:
            QMessageBox.warning(self, "提示", "请先填写主机和用户名")
            return
        self.test_btn.setEnabled(False)
        self.test_status.setStyleSheet("color: #409eff;")
        self.test_status.setText("正在连接…")
        worker = QueryWorker(
            db,
            "SELECT VERSION() AS version, DATABASE() AS db_name",
            None,
            self,
        )
        worker.succeeded.connect(self._on_test_ok)
        worker.failed.connect(self._on_test_fail)
        worker.finished.connect(self._on_test_done)
        self._test_worker = worker
        worker.start()

    def _on_test_ok(self, columns, rows, total):
        version = cell_text(rows[0][0]) if rows and rows[0] else "未知"
        db_name = cell_text(rows[0][1]) if rows and len(rows[0]) > 1 else ""
        extra = f"，当前库：{db_name}" if db_name else ""
        self.test_status.setStyleSheet("color: #67c23a;")
        self.test_status.setText(f"连接成功，MySQL {version}{extra}")

    def _on_test_fail(self, message):
        self.test_status.setStyleSheet("color: #f56c6c;")
        self.test_status.setText(f"连接失败：{message}")

    def _on_test_done(self):
        self.test_btn.setEnabled(True)
        if self._test_worker is not None:
            self._test_worker.deleteLater()
            self._test_worker = None

    def reject(self):
        if self._test_worker is not None and self._test_worker.isRunning():
            QMessageBox.information(self, "提示", "正在测试连接，请稍候…")
            return
        super().reject()

    def closeEvent(self, event):
        if self._test_worker is not None and self._test_worker.isRunning():
            QMessageBox.information(self, "提示", "正在测试连接，请稍候…")
            event.ignore()
            return
        super().closeEvent(event)


# ==================== 查询语句编辑对话框 ====================
class QueryDialog(QDialog):
    def __init__(self, parent=None, query=None, databases=None, used_names=None):
        super().__init__(parent)
        self.setWindowTitle("编辑查询" if query else "新增查询")
        self.setMinimumSize(760, 560)
        self.used_names = {n.lower() for n in (used_names or []) if n}
        self.databases = databases or []
        self.result_data = None
        self._build_ui(query or {})

    def _build_ui(self, query):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)
        self.name_edit = QLineEdit(_text(query.get("name")))
        self.name_edit.setPlaceholderText("例如：按手机号查 Cookie")
        form.addRow("查询名称：", self.name_edit)

        self.db_combo = QComboBox()
        for db in self.databases:
            self.db_combo.addItem(db["name"])
        wanted = _text(query.get("db")).strip()
        if wanted and self.db_combo.findText(wanted) >= 0:
            self.db_combo.setCurrentText(wanted)
        form.addRow("使用数据库：", self.db_combo)

        self.desc_edit = QLineEdit(_text(query.get("description")))
        self.desc_edit.setPlaceholderText("可选，显示在查询类型下方")
        form.addRow("说明：", self.desc_edit)
        layout.addLayout(form)

        layout.addWidget(QLabel("SQL 语句（支持 %(名称)s 命名参数，也支持 %s 位置参数）："))
        self.sql_edit = QPlainTextEdit()
        self.sql_edit.setFont(QFont("Consolas", 10))
        self.sql_edit.setPlaceholderText(
            "SELECT * FROM your_table\n"
            "WHERE mobile = %(mobile)s\n"
            "LIMIT 100"
        )
        self.sql_edit.setPlainText(_text(query.get("sql")))
        layout.addWidget(self.sql_edit, 1)

        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        layout.addWidget(self.hint_label)
        self.sql_edit.textChanged.connect(self._update_hint)
        self._update_hint()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_hint(self):
        named, positional = parse_sql_placeholders(self.sql_edit.toPlainText())
        if named and positional:
            self.hint_label.setStyleSheet("color: #f56c6c;")
            self.hint_label.setText(
                "SQL 同时使用了 %(名称)s 和 %s，pymysql 不支持混用，请统一为一种。"
            )
            return
        if named:
            self.hint_label.setStyleSheet("color: #409eff;")
            self.hint_label.setText(
                f"检测到 {len(named)} 个命名参数：{'、'.join(named)}；"
                "执行时会自动生成对应输入框。"
            )
        elif positional:
            self.hint_label.setStyleSheet("color: #409eff;")
            self.hint_label.setText(
                f"检测到 {positional} 个位置参数（%s），执行时按出现顺序生成输入框。"
            )
        else:
            self.hint_label.setStyleSheet("color: #909399;")
            self.hint_label.setText(
                "该 SQL 不含参数。若 SQL 中需要字面量百分号，请写成 %% 。"
            )

    def _on_accept(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请填写查询名称")
            return
        if name.lower() in self.used_names:
            QMessageBox.warning(self, "提示", f"名称「{name}」已存在，请换一个")
            return
        db = self.db_combo.currentText().strip()
        if not db:
            QMessageBox.warning(self, "提示", "请选择数据库")
            return
        sql = self.sql_edit.toPlainText().strip()
        if not sql:
            QMessageBox.warning(self, "提示", "请填写 SQL 语句")
            return
        named, positional = parse_sql_placeholders(sql)
        if named and positional:
            QMessageBox.warning(
                self, "提示",
                "同一条 SQL 不能同时使用 %(名称)s 和 %s 两种参数，请统一为一种。"
            )
            return
        self.result_data = {
            "name": name,
            "db": db,
            "description": self.desc_edit.text().strip(),
            "sql": sql,
        }
        self.accept()


# ==================== 主窗口 ====================
class CookieQueryApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1020, 760)

        self.config = load_config()
        self.current_cookie_json = None
        self.param_edits = {}
        self._worker = None
        self._worker_mode = None
        self._last_params = None
        self._busy = False
        self._status_token = 0

        self.init_ui()
        self.refresh_db_combo(self.config.get("last_db"))
        self.refresh_query_combo(self.config.get("last_query"))
        self._refresh_enabled_state()

    # ---------- 样式 ----------
    def _apply_styles(self):
        self.setStyleSheet("""
            QWidget {
                font-family: "Microsoft YaHei", "Segoe UI", Arial;
                font-size: 12px;
            }
            #root {
                background-color: #f5f7fa;
            }
            QGroupBox {
                border: 1px solid #dcdfe6;
                border-radius: 6px;
                background-color: #ffffff;
                margin-top: 10px;
                padding: 12px 8px 8px 8px;
                font-weight: normal;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 10px;
                padding: 0 4px;
                color: #303133;
                font-weight: bold;
            }
            QLineEdit, QSpinBox, QComboBox {
                padding: 6px 10px;
                border: 1px solid #dcdfe6;
                border-radius: 5px;
                background-color: #ffffff;
                min-height: 20px;
            }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
                border-color: #409eff;
            }
            QPushButton {
                padding: 6px 14px;
                border: 1px solid #dcdfe6;
                border-radius: 5px;
                background-color: #ffffff;
                color: #303133;
            }
            QPushButton:hover {
                border-color: #409eff;
                color: #409eff;
            }
            QPushButton:disabled {
                color: #c0c4cc;
                border-color: #e4e7ed;
                background-color: #f8f8f8;
            }
            QPushButton#primary {
                background-color: #409eff;
                border: none;
                color: #ffffff;
                font-weight: bold;
            }
            QPushButton#primary:hover {
                background-color: #66b1ff;
            }
            QPushButton#primary:disabled {
                background-color: #a0cfff;
                color: #ffffff;
            }
            QPushButton#success {
                background-color: #67c23a;
                border: none;
                color: #ffffff;
            }
            QPushButton#success:hover {
                background-color: #85ce61;
            }
            QPushButton#success:disabled {
                background-color: #b3e19d;
                color: #ffffff;
            }
            QPlainTextEdit {
                border: 1px solid #dcdfe6;
                border-radius: 5px;
                padding: 8px;
                background-color: #fafafa;
            }
            QTabWidget::pane {
                border: 1px solid #dcdfe6;
                border-radius: 5px;
                background-color: #ffffff;
            }
            QTabBar::tab {
                padding: 8px 14px;
                margin-right: 2px;
            }
            QTableWidget {
                border: 1px solid #dcdfe6;
                border-radius: 5px;
                background-color: #ffffff;
                gridline-color: #ebeef5;
            }
            QHeaderView::section {
                background-color: #f5f7fa;
                padding: 6px;
                border: none;
                border-right: 1px solid #ebeef5;
                border-bottom: 1px solid #ebeef5;
            }
        """)

    # ---------- UI ----------
    def init_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 14, 16, 12)
        main_layout.setSpacing(10)

        title = QLabel(APP_TITLE)
        title_font = QFont("Microsoft YaHei", 16)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #2c3e50; padding: 4px;")
        main_layout.addWidget(title)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # ===== 上部：数据源 + 查询 + 参数 =====
        top_widget = QWidget()
        top_layout = QVBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(10)

        # 数据源
        db_group = QGroupBox("数据源")
        db_row = QHBoxLayout(db_group)
        db_row.setSpacing(8)
        db_row.addWidget(QLabel("数据库："))
        self.db_combo = QComboBox()
        self.db_combo.setMinimumWidth(240)
        self.db_combo.currentTextChanged.connect(self.on_db_changed)
        db_row.addWidget(self.db_combo, 1)
        self.db_add_btn = QPushButton("新增")
        self.db_add_btn.clicked.connect(self.add_database)
        self.db_edit_btn = QPushButton("编辑")
        self.db_edit_btn.clicked.connect(self.edit_database)
        self.db_del_btn = QPushButton("删除")
        self.db_del_btn.clicked.connect(self.delete_database)
        self.db_test_btn = QPushButton("测试连接")
        self.db_test_btn.clicked.connect(self.test_current_db)
        self.db_reset_btn = QPushButton("恢复示例")
        self.db_reset_btn.setToolTip(
            "把数据库连接和查询语句恢复为内置示例（会先备份当前配置文件）"
        )
        self.db_reset_btn.clicked.connect(self.reset_config)
        db_row.addWidget(self.db_add_btn)
        db_row.addWidget(self.db_edit_btn)
        db_row.addWidget(self.db_del_btn)
        db_row.addWidget(self.db_test_btn)
        db_row.addWidget(self.db_reset_btn)
        top_layout.addWidget(db_group)

        # 查询类型
        query_group = QGroupBox("查询类型")
        query_box = QVBoxLayout(query_group)
        query_box.setSpacing(6)
        query_row = QHBoxLayout()
        query_row.setSpacing(8)
        query_row.addWidget(QLabel("查询语句："))
        self.query_combo = QComboBox()
        self.query_combo.setMinimumWidth(240)
        self.query_combo.currentTextChanged.connect(self.on_query_changed)
        query_row.addWidget(self.query_combo, 1)
        self.query_add_btn = QPushButton("新增")
        self.query_add_btn.clicked.connect(self.add_query)
        self.query_edit_btn = QPushButton("编辑")
        self.query_edit_btn.clicked.connect(self.edit_query)
        self.query_del_btn = QPushButton("删除")
        self.query_del_btn.clicked.connect(self.delete_query)
        query_row.addWidget(self.query_add_btn)
        query_row.addWidget(self.query_edit_btn)
        query_row.addWidget(self.query_del_btn)
        query_box.addLayout(query_row)
        self.query_desc = QLabel("")
        self.query_desc.setWordWrap(True)
        self.query_desc.setStyleSheet("color: #909399; font-weight: normal;")
        query_box.addWidget(self.query_desc)
        top_layout.addWidget(query_group)

        # SQL 预览 + 参数
        sql_group = QGroupBox("SQL 与参数")
        sql_row = QHBoxLayout(sql_group)
        sql_row.setSpacing(10)

        left = QVBoxLayout()
        left.addWidget(QLabel("SQL 预览："))
        self.sql_preview = QPlainTextEdit()
        self.sql_preview.setReadOnly(True)
        self.sql_preview.setFont(QFont("Consolas", 9))
        self.sql_preview.setMaximumHeight(110)
        left.addWidget(self.sql_preview)
        sql_row.addLayout(left, 3)

        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(4)
        right.addWidget(QLabel("查询参数："))
        self.params_container = QWidget()
        self.params_form = QFormLayout(self.params_container)
        self.params_form.setContentsMargins(0, 0, 0, 0)
        self.params_form.setSpacing(6)
        right.addWidget(self.params_container)
        self.params_note = QLabel("")
        self.params_note.setWordWrap(True)
        self.params_note.setStyleSheet("color: #909399;")
        right.addWidget(self.params_note)
        right.addStretch()
        sql_row.addWidget(right_widget, 2)
        top_layout.addWidget(sql_group)

        # 执行按钮
        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.run_btn = QPushButton("执行查询 (F5)")
        self.run_btn.setObjectName("primary")
        self.run_btn.setMinimumHeight(34)
        self.run_btn.clicked.connect(self.run_query)
        self.clear_params_btn = QPushButton("清空参数 (Esc)")
        self.clear_params_btn.clicked.connect(self.clear_params)
        self.copy_results_btn = QPushButton("复制结果")
        self.copy_results_btn.clicked.connect(self.copy_results)
        self.export_csv_btn = QPushButton("导出 CSV")
        self.export_csv_btn.clicked.connect(self.export_csv)
        action_row.addWidget(self.run_btn)
        action_row.addWidget(self.clear_params_btn)
        action_row.addWidget(self.copy_results_btn)
        action_row.addWidget(self.export_csv_btn)
        self.result_summary = QLabel("")
        self.result_summary.setStyleSheet("color: #909399;")
        action_row.addWidget(self.result_summary)
        action_row.addStretch()
        top_layout.addLayout(action_row)

        splitter.addWidget(top_widget)

        # ===== 下部：结果 + Cookie =====
        bottom_widget = QWidget()
        bottom_layout = QVBoxLayout(bottom_widget)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(8)

        self.tabs = QTabWidget()

        self.result_table = QTableWidget()
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.setWordWrap(False)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.result_table.verticalHeader().setDefaultSectionSize(24)
        self.result_table.cellDoubleClicked.connect(self.on_cell_double_clicked)

        self.cookie_editor_text = QPlainTextEdit()
        self.cookie_editor_text.setReadOnly(True)
        self.cookie_editor_text.setFont(QFont("Consolas", 10))

        self.formatted_text = QPlainTextEdit()
        self.formatted_text.setReadOnly(True)
        self.formatted_text.setFont(QFont("Consolas", 10))

        self.raw_json_text = QPlainTextEdit()
        self.raw_json_text.setReadOnly(True)
        self.raw_json_text.setFont(QFont("Consolas", 10))

        self.tabs.addTab(self.result_table, "查询结果")
        self.tabs.addTab(self.cookie_editor_text, "Cookie-Editor格式")
        self.tabs.addTab(self.formatted_text, "格式化显示")
        self.tabs.addTab(self.raw_json_text, "原始JSON")
        bottom_layout.addWidget(self.tabs)

        cookie_row = QHBoxLayout()
        cookie_row.setSpacing(8)
        self.copy_editor_btn = QPushButton("复制Cookie-Editor格式")
        self.copy_editor_btn.clicked.connect(
            lambda: self.copy_to_clipboard(self.cookie_editor_text.toPlainText(), "Cookie-Editor 格式")
        )
        self.copy_formatted_btn = QPushButton("复制格式化内容")
        self.copy_formatted_btn.clicked.connect(
            lambda: self.copy_to_clipboard(self.formatted_text.toPlainText(), "格式化内容")
        )
        self.copy_raw_btn = QPushButton("复制原始JSON")
        self.copy_raw_btn.clicked.connect(
            lambda: self.copy_to_clipboard(self.raw_json_text.toPlainText(), "原始 JSON")
        )
        self.export_cookie_btn = QPushButton("导出 Cookie JSON")
        self.export_cookie_btn.clicked.connect(self.export_cookie_json)
        self.browser_btn = QPushButton("打开浏览器并导入Cookie")
        self.browser_btn.setObjectName("success")
        self.browser_btn.clicked.connect(self.open_browser)
        for btn in (self.copy_editor_btn, self.copy_formatted_btn, self.copy_raw_btn,
                    self.export_cookie_btn, self.browser_btn):
            cookie_row.addWidget(btn)
        cookie_row.addStretch()
        bottom_layout.addLayout(cookie_row)

        splitter.addWidget(bottom_widget)
        splitter.setSizes([420, 460])
        main_layout.addWidget(splitter, 1)

        # 状态栏
        status_row = QHBoxLayout()
        self.status_label = QLabel("就绪")
        self.status_label.setStyleSheet("color: #909399; padding: 4px;")
        status_row.addWidget(self.status_label, 1)
        self.config_label = QLabel(f"配置文件：{config_path()}")
        self.config_label.setStyleSheet("color: #c0c4cc; font-size: 11px;")
        self.config_label.setToolTip(
            f"{config_path()}\n\n界面中修改的数据库和查询会保存在这里，并优先于代码里的默认示例。"
        )
        status_row.addWidget(self.config_label)
        main_layout.addLayout(status_row)

        self._apply_styles()

    # ---------- 状态与可用性 ----------
    def set_status(self, text, kind="idle", auto=False):
        self.status_label.setText(text)
        color = STATUS_COLORS.get(kind, STATUS_COLORS["idle"])
        self.status_label.setStyleSheet(f"color: {color}; padding: 4px; font-weight: bold;")
        self._status_token += 1
        if auto:
            token = self._status_token
            QTimer.singleShot(4000, lambda: self._reset_status(token))

    def _reset_status(self, token):
        if token != self._status_token:
            return
        self.status_label.setText("就绪")
        self.status_label.setStyleSheet("color: #909399; padding: 4px;")

    def _refresh_enabled_state(self):
        idle = not self._busy
        has_db = self.db_combo.count() > 0
        has_query = self.query_combo.count() > 0
        sql = self.sql_preview.toPlainText()
        named, positional = parse_sql_placeholders(sql)
        sql_ok = bool(sql.strip()) and not (named and positional)

        self.db_combo.setEnabled(idle and has_db)
        self.query_combo.setEnabled(idle and has_query)
        self.db_add_btn.setEnabled(idle)
        self.db_edit_btn.setEnabled(idle and has_db)
        self.db_del_btn.setEnabled(idle and has_db)
        self.db_test_btn.setEnabled(idle and has_db)
        self.query_add_btn.setEnabled(idle and has_db)
        self.query_edit_btn.setEnabled(idle and has_query)
        self.query_del_btn.setEnabled(idle and has_query)
        self.run_btn.setEnabled(idle and has_db and has_query and sql_ok)
        self.clear_params_btn.setEnabled(idle and bool(self.param_edits))

        cookie_ready = bool(self.current_cookie_json)
        editor_ready = cookie_ready and import_ready_count(self.current_cookie_json) > 0
        for btn in (self.copy_editor_btn, self.export_cookie_btn, self.browser_btn):
            btn.setEnabled(editor_ready)
        for btn in (self.copy_formatted_btn, self.copy_raw_btn):
            btn.setEnabled(cookie_ready)

        has_rows = self.result_table.rowCount() > 0
        self.copy_results_btn.setEnabled(idle and has_rows)
        self.export_csv_btn.setEnabled(idle and has_rows)

    def set_busy(self, busy, message=None):
        self._busy = busy
        if busy:
            self.run_btn.setText("查询中…" if self._worker_mode == "query" else "执行查询 (F5)")
            self.db_test_btn.setText("测试中…" if self._worker_mode == "test" else "测试连接")
        else:
            self.run_btn.setText("执行查询 (F5)")
            self.db_test_btn.setText("测试连接")
        self._refresh_enabled_state()
        if message:
            self.set_status(message, "info")

    # ---------- 配置辅助 ----------
    def current_db(self):
        name = self.db_combo.currentText().strip()
        for db in self.config["databases"]:
            if db["name"] == name:
                return db
        return None

    def current_query(self):
        name = self.query_combo.currentText().strip()
        for q in self.config["queries"]:
            if q["name"] == name:
                return q
        return None

    def persist(self):
        cfg = self.config
        cfg["last_db"] = self.db_combo.currentText().strip()
        cfg["last_query"] = self.query_combo.currentText().strip()
        try:
            save_config(cfg)
        except Exception as exc:
            self.set_status(f"配置保存失败：{exc}", "error")
            return False
        return True

    def refresh_db_combo(self, select=None):
        current = select or self.db_combo.currentText()
        names = [db["name"] for db in self.config["databases"]]
        self.db_combo.blockSignals(True)
        self.db_combo.clear()
        self.db_combo.addItems(names)
        if current in names:
            self.db_combo.setCurrentText(current)
        elif names:
            self.db_combo.setCurrentIndex(0)
        self.db_combo.blockSignals(False)
        self._refresh_enabled_state()

    def refresh_query_combo(self, select=None):
        current = select or self.query_combo.currentText()
        names = [q["name"] for q in self.config["queries"]]
        self.query_combo.blockSignals(True)
        self.query_combo.clear()
        self.query_combo.addItems(names)
        if current in names:
            self.query_combo.setCurrentText(current)
        elif names:
            self.query_combo.setCurrentIndex(0)
        self.query_combo.blockSignals(False)
        self.on_query_changed()

    # ---------- 交互 ----------
    def on_db_changed(self, name):
        self._refresh_enabled_state()

    def on_query_changed(self):
        q = self.current_query()
        if not q:
            self.query_desc.setText(
                "尚未配置查询语句，点击「新增」添加；添加前需先有数据库连接。"
            )
            self.sql_preview.setPlainText("")
            self.rebuild_params("")
            self._refresh_enabled_state()
            return

        self.query_desc.setText(q.get("description") or "")
        self.sql_preview.setPlainText(q["sql"])

        if q.get("db") and q["db"] != self.db_combo.currentText():
            idx = self.db_combo.findText(q["db"])
            if idx >= 0:
                self.db_combo.blockSignals(True)
                self.db_combo.setCurrentIndex(idx)
                self.db_combo.blockSignals(False)

        self.rebuild_params(q["sql"])
        self._refresh_enabled_state()

    def rebuild_params(self, sql):
        while self.params_form.rowCount():
            self.params_form.removeRow(0)
        self.param_edits = {}

        if not sql.strip():
            self.params_note.setStyleSheet("color: #909399;")
            self.params_note.setText("选择查询语句后，这里会按 SQL 自动生成参数输入框。")
            return

        named, positional = parse_sql_placeholders(sql)
        if named and positional:
            self.params_note.setStyleSheet("color: #f56c6c;")
            self.params_note.setText("该 SQL 混用了 %(名称)s 与 %s，pymysql 不支持，请编辑查询统一为一种。")
            return

        if named:
            for name in named:
                edit = QLineEdit()
                edit.setPlaceholderText(f"请输入 {name}")
                edit.returnPressed.connect(self.run_query)
                self.param_edits[name] = edit
                self.params_form.addRow(f"{name}：", edit)
            self.params_note.setStyleSheet("color: #909399;")
            self.params_note.setText(f"共 {len(named)} 个命名参数，按上表填写后执行。")
        elif positional:
            for i in range(1, positional + 1):
                edit = QLineEdit()
                edit.setPlaceholderText(f"第 {i} 个 %s 参数")
                edit.returnPressed.connect(self.run_query)
                self.param_edits[f"_pos_{i}"] = edit
                self.params_form.addRow(f"参数{i}：", edit)
            self.params_note.setStyleSheet("color: #909399;")
            self.params_note.setText(
                f"共 {positional} 个位置参数，按 SQL 中出现的顺序填写。"
            )
        else:
            self.params_form.addRow(QLabel("无需参数"))
            self.params_note.setStyleSheet("color: #909399;")
            self.params_note.setText("该 SQL 不含参数，可直接执行。")

    def collect_params(self, sql):
        named, positional = parse_sql_placeholders(sql)
        if named and positional:
            raise ValueError("SQL 混用了 %(名称)s 与 %s，请编辑查询统一为一种。")
        if named:
            params = {}
            for name in named:
                edit = self.param_edits.get(name)
                if edit is None:
                    raise ValueError("参数控件尚未生成，请重新选择查询语句")
                params[name] = edit.text().strip()
            return params
        if positional:
            values = []
            for i in range(1, positional + 1):
                edit = self.param_edits.get(f"_pos_{i}")
                if edit is None:
                    raise ValueError("参数控件尚未生成，请重新选择查询语句")
                values.append(edit.text().strip())
            return tuple(values)
        return None

    def clear_params(self):
        for edit in self.param_edits.values():
            edit.clear()
        if self.param_edits:
            self.set_status("已清空参数", "idle", auto=True)
        self._refresh_enabled_state()

    # ---------- 数据库 CRUD ----------
    def add_database(self):
        used = [db["name"] for db in self.config["databases"]]
        dlg = DatabaseDialog(self, None, used)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        db = dlg.result_data
        self.config["databases"].append(db)
        self.refresh_db_combo(db["name"])
        self.refresh_query_combo(self.query_combo.currentText())
        self.persist()
        self.set_status(f"已新增数据库：{db['name']}", "ok")

    def edit_database(self):
        db = self.current_db()
        if not db:
            QMessageBox.warning(self, "提示", "请先选择要编辑的数据库")
            return
        index = self.config["databases"].index(db)
        used = [d["name"] for d in self.config["databases"] if d is not db]
        dlg = DatabaseDialog(self, db, used)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        new_db = dlg.result_data
        old_name = db["name"]
        self.config["databases"][index] = new_db
        if old_name != new_db["name"]:
            for q in self.config["queries"]:
                if q["db"] == old_name:
                    q["db"] = new_db["name"]
        self.refresh_db_combo(new_db["name"])
        self.on_query_changed()
        self.persist()
        self.set_status(f"已更新数据库：{new_db['name']}", "ok")

    def delete_database(self):
        db = self.current_db()
        if not db:
            QMessageBox.warning(self, "提示", "请先选择要删除的数据库")
            return
        if len(self.config["databases"]) <= 1:
            QMessageBox.warning(self, "提示", "至少需要保留一个数据库连接")
            return
        refs = [q["name"] for q in self.config["queries"] if q["db"] == db["name"]]
        if refs:
            QMessageBox.warning(
                self, "提示",
                f"数据库「{db['name']}」仍被 {len(refs)} 个查询使用：\n"
                + "、".join(refs)
                + "\n请先删除或修改这些查询。"
            )
            return
        answer = QMessageBox.question(
            self, "确认删除",
            f"确定删除数据库连接「{db['name']}」吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.config["databases"].remove(db)
        self.refresh_db_combo(None)
        self.persist()
        self.set_status(f"已删除数据库：{db['name']}", "warn", auto=True)

    def reset_config(self):
        """把配置恢复为内置示例（先备份现有配置文件）"""
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.warning(self, "提示", "当前有查询正在执行，请等待结束后再恢复示例配置")
            return
        answer = QMessageBox.question(
            self, "恢复示例配置",
            "将把数据库连接和查询语句全部恢复为内置示例。\n"
            "当前配置文件会先备份到同一目录，可随时手动还原。\n\n是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        path = config_path()
        backup = ""
        if os.path.exists(path):
            backup = f"{path}.bak-{datetime.now():%Y%m%d%H%M%S}"
            try:
                os.replace(path, backup)
            except OSError as exc:
                QMessageBox.critical(self, "恢复失败", f"无法备份当前配置：{exc}")
                return

        self.config = normalize_config(default_config())
        try:
            save_config(self.config)
        except Exception as exc:
            QMessageBox.critical(self, "恢复失败", f"无法写入示例配置：{exc}")
            return

        self.refresh_db_combo(self.config.get("last_db"))
        self.refresh_query_combo(self.config.get("last_query"))
        self._clear_results()
        self._refresh_enabled_state()

        msg = "已恢复示例配置"
        if backup:
            msg += f"，原配置已备份为 {os.path.basename(backup)}"
        self.set_status(msg, "ok")

    # ---------- 查询 CRUD ----------
    def add_query(self):
        if not self.config["databases"]:
            QMessageBox.warning(self, "提示", "请先新增一个数据库连接")
            return
        used = [q["name"] for q in self.config["queries"]]
        initial = {"db": self.db_combo.currentText(), "sql": ""}
        dlg = QueryDialog(self, initial, self.config["databases"], used)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        q = dlg.result_data
        self.config["queries"].append(q)
        self.refresh_query_combo(q["name"])
        self.persist()
        self.set_status(f"已新增查询：{q['name']}", "ok")

    def edit_query(self):
        q = self.current_query()
        if not q:
            QMessageBox.warning(self, "提示", "请先选择要编辑的查询语句")
            return
        used = [item["name"] for item in self.config["queries"] if item is not q]
        dlg = QueryDialog(self, q, self.config["databases"], used)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_data:
            return
        index = self.config["queries"].index(q)
        new_q = dlg.result_data
        self.config["queries"][index] = new_q
        self.refresh_query_combo(new_q["name"])
        self.persist()
        self.set_status(f"已更新查询：{new_q['name']}", "ok")

    def delete_query(self):
        q = self.current_query()
        if not q:
            QMessageBox.warning(self, "提示", "请先选择要删除的查询语句")
            return
        answer = QMessageBox.question(
            self, "确认删除",
            f"确定删除查询「{q['name']}」吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.config["queries"].remove(q)
        self.refresh_query_combo(None)
        self.persist()
        self.set_status(f"已删除查询：{q['name']}", "warn", auto=True)

    # ---------- 执行 ----------
    def test_current_db(self):
        db = self.current_db()
        if not db:
            QMessageBox.warning(self, "提示", "请先选择数据库")
            return
        self._start_worker(db, "SELECT VERSION() AS version, DATABASE() AS db_name", None, "test")

    def run_query(self):
        if self._busy:
            return
        db = self.current_db()
        q = self.current_query()
        if not db or not q:
            QMessageBox.warning(self, "提示", "请先选择数据库和查询语句")
            return
        sql = q["sql"]
        try:
            params = self.collect_params(sql)
        except ValueError as exc:
            QMessageBox.warning(self, "提示", str(exc))
            return

        self._clear_results()
        self._start_worker(db, sql, params, "query")

    def _start_worker(self, db, sql, params, mode):
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "提示", "上一个查询仍在执行，请稍候…")
            return
        worker = QueryWorker(db, sql, params, self)
        worker.succeeded.connect(self._on_worker_succeeded)
        worker.failed.connect(self._on_worker_failed)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        self._worker_mode = mode
        self._last_params = params
        self._busy = True
        self.set_busy(True, "正在测试连接…" if mode == "test" else "正在查询…")
        worker.start()

    def _on_worker_succeeded(self, columns, rows, total):
        if self._worker_mode == "test":
            version = cell_text(rows[0][0]) if rows and rows[0] else "未知"
            db_name = cell_text(rows[0][1]) if rows and len(rows[0]) > 1 else ""
            extra = f"，当前库：{db_name}" if db_name else ""
            self.set_status(f"连接成功：MySQL {version}{extra}", "ok")
            QMessageBox.information(self, "连接成功", f"MySQL {version}{extra}")
            return
        self.show_results(columns, rows, total)

    def _on_worker_failed(self, message):
        hint = ""
        if self._worker_mode == "query" and self._last_params is not None:
            low = message.lower()
            if "format" in low or "unsupported" in low:
                hint = "\n\n提示：SQL 中的字面量百分号需要写成 %% 。"
        if self._worker_mode == "test":
            self.set_status(f"连接失败：{message}", "error")
            QMessageBox.critical(self, "连接失败", message)
        else:
            self.set_status(f"查询失败：{message}", "error")
            QMessageBox.warning(self, "查询失败", message + hint)

    def _on_worker_finished(self):
        self._busy = False
        self.set_busy(False)
        if self._worker is not None:
            self._worker.deleteLater()
            self._worker = None
        self._worker_mode = None

    def show_results(self, columns, rows, total):
        table = self.result_table
        table.clear()
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels([str(c) for c in columns])
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c in range(len(columns)):
                value = row[c] if c < len(row) else None
                table.setItem(r, c, QTableWidgetItem(cell_text(value)))

        shown = len(rows)
        if total and total > shown:
            summary = f"共 {total} 行，仅显示前 {shown} 行"
        else:
            summary = f"共 {shown} 行"
        self.result_summary.setText(summary)
        self.set_status(f"查询成功：{summary}", "ok")

        self.auto_load_cookie(columns, rows)
        self._refresh_enabled_state()

    def auto_load_cookie(self, columns, rows):
        if not columns or not rows:
            return
        lower = [str(c).lower() for c in columns]
        idx = lower.index("cookie") if "cookie" in lower else None
        if idx is None:
            idx = next((i for i, name in enumerate(lower) if "cookie" in name), None)
        if idx is None:
            return
        for row_index, row in enumerate(rows[:20]):
            if idx >= len(row):
                continue
            try:
                cookies = parse_cookie_value(row[idx])
            except Exception:
                continue
            self.load_cookie(cookies)
            skipped = len(cookies) - import_ready_count(cookies)
            self.set_status(
                f"查询成功：共 {len(rows)} 行，已自动加载第 {row_index + 1} 行的 Cookie"
                f"（{self.cookie_import_note(cookies)}）",
                "warn" if skipped else "ok",
            )
            return

    def _clear_results(self):
        self.result_table.clear()
        self.result_table.setRowCount(0)
        self.result_table.setColumnCount(0)
        self.result_summary.setText("")
        self.clear_cookie()

    def clear_cookie(self):
        self.current_cookie_json = None
        self.cookie_editor_text.clear()
        self.formatted_text.clear()
        self.raw_json_text.clear()
        self._refresh_enabled_state()

    def load_cookie(self, cookies):
        self.current_cookie_json = cookies
        self.cookie_editor_text.setPlainText(
            json.dumps(cookie_to_editor(cookies), indent=4, ensure_ascii=False)
        )
        self.formatted_text.setPlainText(format_cookie_display(cookies))
        self.raw_json_text.setPlainText(
            json.dumps(cookies, indent=2, ensure_ascii=False)
        )
        self._refresh_enabled_state()

    def cookie_import_note(self, cookies):
        """Cookie 可导入情况说明（已过期 / 字段不完整的会被跳过）"""
        total = len(cookies)
        ready = import_ready_count(cookies)
        if ready == total:
            return f"共 {total} 条 Cookie"
        return f"共 {total} 条 Cookie，可导入 {ready} 条（{total - ready} 条已过期/无效，导入时跳过）"

    def on_cell_double_clicked(self, row, column):
        item = self.result_table.item(row, column)
        if item is None:
            return
        try:
            cookies = parse_cookie_value(item.text())
        except Exception as exc:
            self.set_status(f"该单元格不是可解析的 Cookie：{exc}", "warn", auto=True)
            return
        self.load_cookie(cookies)
        skipped = len(cookies) - import_ready_count(cookies)
        self.set_status(
            f"已从第 {row + 1} 行、第 {column + 1} 列加载 Cookie"
            f"（{self.cookie_import_note(cookies)}）",
            "warn" if skipped else "ok",
        )

    # ---------- 复制 / 导出 ----------
    def copy_to_clipboard(self, text, label="内容"):
        if not text:
            QMessageBox.warning(self, "提示", "没有可复制的内容")
            return
        QApplication.clipboard().setText(text)
        self.set_status(f"已复制{label}", "ok", auto=True)

    def _table_matrix(self):
        table = self.result_table
        headers = []
        for c in range(table.columnCount()):
            header = table.horizontalHeaderItem(c)
            headers.append(header.text() if header else f"col{c + 1}")
        rows = []
        for r in range(table.rowCount()):
            row = []
            for c in range(table.columnCount()):
                item = table.item(r, c)
                row.append(item.text() if item else "")
            rows.append(row)
        return headers, rows

    def copy_results(self):
        if self.result_table.rowCount() == 0:
            QMessageBox.warning(self, "提示", "当前没有查询结果")
            return
        headers, rows = self._table_matrix()

        def safe(text):
            return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")

        lines = ["\t".join(safe(h) for h in headers)]
        for row in rows:
            lines.append("\t".join(safe(v) for v in row))
        QApplication.clipboard().setText("\n".join(lines))
        self.set_status(f"已复制 {len(rows)} 行结果", "ok", auto=True)

    def export_csv(self):
        if self.result_table.rowCount() == 0:
            QMessageBox.warning(self, "提示", "当前没有查询结果")
            return
        default_name = f"查询结果_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出查询结果", default_name, "CSV 文件 (*.csv)"
        )
        if not path:
            return
        headers, rows = self._table_matrix()
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                writer.writerows(rows)
        except Exception as exc:
            QMessageBox.critical(self, "错误", f"导出失败：{exc}")
            return
        self.set_status(f"已导出 {len(rows)} 行到 {path}", "ok", auto=True)

    def export_cookie_json(self):
        if not self.current_cookie_json:
            QMessageBox.warning(self, "提示", "没有可导出的 Cookie")
            return
        editor_cookies = cookie_to_editor(self.current_cookie_json)
        if not editor_cookies:
            QMessageBox.warning(self, "提示", "所有 Cookie 均已过期或无效，没有可导出的内容")
            return
        default_name = f"cookie_{datetime.now():%Y%m%d_%H%M%S}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Cookie", default_name, "JSON 文件 (*.json)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(editor_cookies, f, indent=4, ensure_ascii=False)
        except Exception as exc:
            QMessageBox.critical(self, "错误", f"导出失败：{exc}")
            return
        self.set_status(
            f"已导出 {len(editor_cookies)} 条 Cookie-Editor 格式数据到 {path}",
            "ok", auto=True,
        )

    def open_browser(self):
        if not self.current_cookie_json:
            QMessageBox.warning(self, "提示", "没有可用的 Cookie 数据")
            return
        total = len(self.current_cookie_json)
        ready = import_ready_count(self.current_cookie_json)
        if ready == 0:
            QMessageBox.warning(
                self, "提示",
                f"共 {total} 条 Cookie，但全部已过期或缺少必要字段，无法导入。\n"
                "请重新查询一份最新的 Cookie。",
            )
            return
        editor_text = self.cookie_editor_text.toPlainText()
        QApplication.clipboard().setText(editor_text)
        url = self.config.get("browser_url") or DEFAULT_BROWSER_URL
        webbrowser.open(url)
        extra = ""
        if ready < total:
            extra = f"\n注意：{total - ready} 条已过期/无效的 Cookie 已自动跳过。\n"
        QMessageBox.information(
            self, "操作说明",
            f"浏览器已打开，Cookie-Editor 格式已复制到剪贴板（可导入 {ready} 条）。\n"
            f"{extra}\n"
            "请务必按以下顺序操作：\n"
            "1. 等待目标网站加载完成，确认当前标签页就是该网站页面\n"
            "2. 在这个标签页上点击浏览器右上角的 Cookie-Editor 图标\n"
            "3. 点击「Import」按钮\n"
            "4. 按 Ctrl+V 粘贴已复制的 Cookie 数据\n"
            "5. 点击「Save」保存，然后刷新页面\n\n"
            "若提示 Failed to parse or set cookie named \"xxx\"：\n"
            "说明导入时所在的标签页与 Cookie 域名不一致（或该 Cookie 被浏览器策略拒绝），\n"
            "请回到目标网站页面重新打开扩展再导入。\n\n"
            "扩展地址：\n"
            "https://chrome.google.com/webstore/detail/cookie-editor/"
            "hlkenndednhfkekhgcdicdfddnkalmdm"
        )

    # ---------- 窗口事件 ----------
    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.clear_params()
        elif event.key() == Qt.Key.Key_F5:
            if self.run_btn.isEnabled():
                self.run_query()
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "提示", "查询仍在执行，请等待完成后再关闭。")
            event.ignore()
            return
        self.persist()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(QFont("Microsoft YaHei", 9))
    win = CookieQueryApp()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
