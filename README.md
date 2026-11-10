# tools-pyqt

一组基于 PyQt6 的桌面小工具：脚本批量管理、MySQL 查询与 Cookie 转换、PyInstaller 打包辅助。

## 环境要求

- Python 3.9+
- 依赖安装：

```bash
python -m pip install -r requirements.txt
```

## 工具说明

### py脚本管理控制台.py

<img width="1065" height="752" alt="image" src="https://github.com/user-attachments/assets/dee31d75-ae64-49a9-970e-26e1834e1a89" />


批量管理并运行同一目录下的 `.py` 脚本。

- 只列出真实存在的 `*.py` 文件，支持新增、编辑、删除、运行、停止
- 支持多脚本并行运行，输出统一显示在日志区
- 自动记住上次使用的目录，支持手动刷新（按钮 / F5）和目录变化自动刷新
- 单实例运行：再次打开时复用已启动的窗口

启动：

```bash
python py脚本管理控制台.py
```

目录记忆保存在 `%APPDATA%\MultiScriptManager\config.json`，可用环境变量 `SCRIPT_MANAGER_CONFIG` 指定其它路径。

### 小云雀查询Cookie.py
<img width="1007" height="748" alt="image" src="https://github.com/user-attachments/assets/2c168b00-6be2-4732-8122-e99fd338181f" />


MySQL 查询工具，可将查询结果中的 Cookie 转换为 Cookie-Editor 扩展可导入的格式。

- 数据库连接和查询语句均可在界面中新增、编辑、删除
- 查询参数按 SQL 自动推导：`%(name)s` 生成命名参数，`%s` 生成位置参数
- 结果表格展示，支持导出 CSV
- 结果含 Cookie 时可一键复制 / 导出 Cookie-Editor JSON，并跳转浏览器导入

启动：

```bash
python 小云雀查询Cookie.py
```

配置保存在 `%APPDATA%\小云雀查询\config.json`，可用环境变量 `XYQ_CONFIG` 指定其它路径。首次运行使用示例数据库；界面中的「恢复示例」会重置为示例配置并自动备份原文件。

### py打包exe.py
<img width="1035" height="739" alt="image" src="https://github.com/user-attachments/assets/e33df525-311b-433c-b4d6-8d0e2ccdd9b5" />


PyInstaller 图形化打包辅助工具，支持选择入口脚本、图标、打包模式并查看实时日志。

启动：

```bash
python py打包exe.py
```

## 说明

- 仓库内不包含任何真实数据库账号或密码。
- 本地配置文件（`config.json`）及备份文件不会被提交，请勿手动把它们加入仓库。
- 打包产物（`build/`、`dist/`、`*.exe`、`*.spec`）已加入 `.gitignore`。
