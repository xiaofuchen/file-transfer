<p align="center">
  <h1 align="center">📱⟷💻 局域网文件互传</h1>
  <p align="center">
    <img src="https://img.shields.io/badge/Python-3.8+-blue?logo=python" alt="Python">
    <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
    <img src="https://img.shields.io/badge/No%20App%20Required-✓-brightgreen" alt="No App">
  </p>
  <p align="center">
    同一 WiFi 下，电脑运行，手机扫码，<b>上传 / 下载文件</b>。<br>
    无需安装 App，浏览器即用。
  </p>
</p>

---

## 📸 界面

<p align="center">
  <img src="docs/screenshot.png" alt="GUI 截图" width="420">
</p>

## ✨ 特性

- 📲 **扫码即用** —— GUI 窗口直接显示高清二维码，手机一扫就打开
- ⬆️ **手机 → 电脑** —— 选择/拖拽文件上传，支持多选与大文件（流式解析，不吃内存）
- ⬇️ **电脑 → 手机** —— 浏览共享目录，点击文件名即可下载
- 🖥️ **GUI 窗口**（默认）—— Tkinter 原生界面，二维码高清，上传记录实时刷新
- ⌨️ **终端模式**（`--no-gui`）—— 纯 ASCII 二维码 + qrcode.png 文件，适合无桌面环境
- 🔒 **令牌校验** —— 随机 6 位 token 保护，防止同局域网他人误连
- 📁 **防覆盖** —— 同名文件自动加 `(1)` `(2)` 后缀，绝不丢失数据
- 🪶 **轻量依赖** —— 仅 `qrcode` + `pillow`，其余全部 Python 标准库
- 📦 **单文件 exe** —— PyInstaller 打包，双击即用，发给朋友零门槛

## 🚀 快速开始

### 方式一：源码运行

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 启动（GUI 模式）
python app.py
```

### 方式二：打包为 exe

```bash
# 一键构建
build.bat

# 产物在 dist\file-transfer.exe，双击打开
```

> **提示**：首次运行 exe 时 Windows 可能弹出防火墙提示，请勾选「专用网络」并允许。

## 🖥️ 使用步骤

```
电脑连 WiFi → 打开程序 → 窗口显示二维码
              ↓
手机连同一 WiFi → 扫码 → 浏览器打开网页
              ↓
    ┌─────────┴─────────┐
    ↓                    ↓
上传文件到电脑        下载电脑的文件
（窗口实时显示记录）   （浏览共享目录）
```

1. 电脑连接 WiFi，双击 `file-transfer.exe`（或运行 `python app.py`）
2. 窗口显示二维码，状态行显示 **"✅ 服务运行中"**
3. 手机连接**同一个 WiFi**，用相机/浏览器扫码
4. 打开的网页有两个标签：
   - **上传到电脑**：选文件 → 点「开始上传」→ 文件保存到 `uploads/`
   - **电脑上的文件**：浏览 `uploads/` 目录 → 点击文件名下载
5. 电脑窗口的「上传记录」区实时显示收发日志

## 📱 手机端网页功能

| 功能 | 说明 |
|------|------|
| 📤 上传 | 支持多文件、拖拽/点击选择、大文件流式上传 |
| 📥 下载 | 列出共享目录所有文件，点击下载 |
| 📋 文件列表 | 显示文件名、大小、修改时间 |
| 🔄 刷新 | 上传后自动刷新文件列表 |

## ⚙️ 命令行参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--port PORT` | `8000` | 监听端口 |
| `--dir PATH` | `./uploads` | 接收/共享目录 |
| `--no-token` | 关 | 关闭令牌校验（不建议在公共网络使用） |
| `--no-gui` | 关 | 强制终端模式（纯 ASCII 二维码） |

```bash
# GUI 模式（默认）
python app.py

# 终端模式
python app.py --no-gui

# 自定义端口 + 关闭令牌
python app.py --port 9000 --no-token

# 指定共享目录
python app.py --dir D:\Shared

# exe 同样支持
file-transfer.exe --port 8080 --no-token
```

## 🛠️ 技术实现

```
app.py  (单文件，~1000 行)
├── Config             运行时配置（端口、目录、token、回调）
├── StreamBuffer       流式缓冲区（read1 避免 socket 阻塞）
├── parse_multipart    流式 multipart 解析器（大文件友好）
├── Handler            HTTP 请求处理（GET / /files /download, POST /upload）
├── PAGE_HTML          内嵌手机端网页（单页应用）
├── make_qr_image      PIL 生成高清二维码
├── GuiApp             Tkinter GUI（二维码、状态、日志、健康检查）
└── main()             入口：GUI 模式 / 终端模式 分支
```

**关键设计决策**：
- `HTTP/1.0` 协议 —— 响应边界清晰，避免移动端 keep-alive 挂起
- `read1()` 而非 `read()` —— socket BufferedReader 下避免阻塞
- 服务器绑定在主线程，`serve_forever` 在守护线程 —— 启动异常可被 GUI 捕获并提示

## ❓ 常见问题

<details>
<summary><b>📱 手机扫码打不开？</b></summary>

1. 确认手机和电脑连接**同一个 WiFi**（不是访客网络、不是移动数据）
2. 检查 GUI 状态行是否为 **"✅ 服务运行中"**（不是 ⚠️ 或 ❌）
3. Windows 首次运行会弹「防火墙」提示，**必须勾选「专用网络」并允许**
4. 公司/学校网络可能开启了 **AP 隔离（客户端隔离）**，手机开热点让电脑连接测试
5. 尝试在电脑浏览器打开 `http://127.0.0.1:8000/` —— 若能打开说明服务正常，问题在手机上
</details>

<details>
<summary><b>🖥️ GUI 窗口打不开？</b></summary>

```bash
pip install pillow qrcode   # 确认依赖已安装
python -c "import tkinter"  # 确认 Tkinter 可用（Python 自带）
```
极少数精简版 Python 可能缺 tkinter，重装完整版 Python 即可。
</details>

<details>
<summary><b>📦 exe 被 Windows Defender 拦截？</b></summary>

这是 PyInstaller 打包的常见误报。点击「更多信息」→「仍要运行」，或将 `dist/` 目录加入 Defender 排除列表。
</details>

<details>
<summary><b>🔧 如何更换端口？</b></summary>

```bash
python app.py --port 9000
file-transfer.exe --port 9000
```
</details>

## 📂 项目文件

```
├── app.py                主程序（GUI + HTTP 服务器 + 前端页面）
├── requirements.txt      Python 依赖
├── build.bat             PyInstaller 打包脚本
├── .gitignore            Git 忽略规则
├── docs/
│   └── screenshot.png    界面截图
├── dist/
│   └── file-transfer.exe 打包产物（33MB，可独立运行）
└── uploads/              接收/共享目录（自动创建）
```

## 📄 License

MIT — 随意使用、修改、分发。
