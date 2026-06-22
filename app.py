#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手机 <-> 电脑 局域网文件互传小工具

电脑运行本程序 -> 窗口显示二维码 -> 手机扫码打开网页
-> 手机上传文件到电脑 / 下载电脑共享的文件

默认 GUI 模式（Tkinter 窗口）。
--no-gui 切换为终端模式。

依赖：qrcode、pillow（其余均为 Python 标准库）
"""

import argparse
import json
import os
import re
import socket
import sys
import threading
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import urlopen, Request

try:
    import qrcode
except ImportError:
    sys.exit("缺少依赖，请先执行： pip install -r requirements.txt")

# Tkinter 是标准库，但某些精简 Python 可能缺失
GUI_AVAILABLE = False
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext
    from PIL import Image, ImageTk
    GUI_AVAILABLE = True
except ImportError:
    pass


# ======================== 配置 / 全局 ========================

class Config:
    """运行时配置（main 中填充）"""
    host_port = 8000
    share_dir = os.path.join(os.getcwd(), "uploads")
    token = None            # None 表示关闭令牌校验
    upload_root = None      # = share_dir
    notify_callback = None  # GUI 模式: callable(filename, size_hint) 当收到文件时


# ======================== 工具函数 ========================

def get_lan_ip():
    """通过 UDP 探测拿到本机在局域网中的 IP（不会真正发包）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def safe_filename(name):
    """清洗文件名：去路径、去非法字符、限制长度"""
    name = os.path.basename(name)
    # 去掉 Windows / macOS / Linux 均不允许或危险的字符
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    name = name.rstrip(". ") or "unnamed"
    if len(name) > 200:
        base, ext = os.path.splitext(name)
        name = base[: 200 - len(ext)] + ext
    return name


def unique_path(directory, filename):
    """如果目标已存在，自动加 (1)、(2) 后缀，避免覆盖"""
    target = os.path.join(directory, filename)
    if not os.path.exists(target):
        return target
    base, ext = os.path.splitext(filename)
    i = 1
    while True:
        candidate = os.path.join(directory, f"{base} ({i}){ext}")
        if not os.path.exists(candidate):
            return candidate
        i += 1


def human_size(num):
    """把字节数转成人类可读字符串"""
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(num) < 1024.0:
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


# ======================== 流式 multipart 解析 ========================
#
# 设计：一个滑动缓冲区 buf，所有原始数据都先进 buf。
# boundary 永远出现在 buf 内部时才切分，且每次写出时
# 保留尾部 (len(boundary)-1) 字节不写，避免 boundary 跨块时被截断。
# boundary 之后的内容（下一个 part 的 boundary 行 + headers）留在 buf 里，
# 交给下一阶段的 readline 继续解析，不丢任何字节。

class StreamBuffer:
    """rfile 之上的缓冲读取器：支持按块填充 + 按行读取"""

    def __init__(self, rfile):
        self.rfile = rfile
        self.buf = b""

    def _fill(self, size=65536):
        # 关键：必须用 read1 而非 read。
        # socket 的 BufferedReader.read(n) 会阻塞直到凑满 n 字节或遇到 EOF，
        # 而 keep-alive 连接不会 EOF，导致不足 n 字节时永远卡住。
        # read1 只做一次底层读取，有多少数据立即返回，符合流式解析语义。
        chunk = self.rfile.read1(size) if hasattr(self.rfile, "read1") else self.rfile.read(size)
        if chunk:
            self.buf += chunk
        return len(chunk)

    def readline(self):
        """读一行（含换行符）；必要时自动填充缓冲区"""
        while b"\n" not in self.buf:
            if self._fill() == 0:
                # 流结束，返回缓冲区剩余
                line, self.buf = self.buf, b""
                return line
        idx = self.buf.index(b"\n") + 1
        line, self.buf = self.buf[:idx], self.buf[idx:]
        return line


def parse_multipart_stream(rfile, boundary, save_dir, on_progress=None):
    """
    流式解析 multipart/form-data，把每个文件字段写到 save_dir。
    返回保存成功的文件名列表。不会把整个文件读进内存。
    """
    saved = []
    boundary_bytes = ("--" + boundary).encode("latin-1")
    end_marker = boundary_bytes + b"--"
    reader = StreamBuffer(rfile)

    # 跳过 preamble，定位到第一个 boundary 行
    while True:
        line = reader.readline()
        if not line:
            return saved
        if line.strip().startswith(boundary_bytes):
            break

    while True:
        # 此时上一个 boundary 行已消费。先判断是否结束标记。
        # end_marker 可能紧跟 boundary（即 --boundary--），也可能是普通 boundary。
        # 直接进入 header 解析；若第一行就是 --boundary-- 表示结束。
        first_after = reader.readline()
        if not first_after or first_after.strip().startswith(end_marker):
            break

        # 解析 part headers（first_after 是第一个 header 行）
        headers = {}
        hline = first_after
        while hline not in (b"\r\n", b"\n", b""):
            try:
                # 先用 UTF-8 解码（浏览器通常直接塞中文），失败回退 latin-1
                line_str = hline.decode("utf-8").strip()
            except UnicodeDecodeError:
                line_str = hline.decode("latin-1").strip()
            k, _, v = line_str.partition(":")
            try:
                if k:
                    headers[k.lower()] = v
            except Exception:
                pass
            hline = reader.readline()

        disp = headers.get("content-disposition", "")
        fname_match = re.search(r'filename="([^"]*)"', disp)
        if not fname_match:
            # 无 filename 的普通表单字段：读到下一个 boundary 为止并丢弃
            _drain_until_boundary(reader, boundary_bytes)
            # 读取 boundary 行后的换行，准备下一轮
            if not _consume_boundary_line(reader, end_marker):
                break
            continue

        raw_name = fname_match.group(1)
        try:
            raw_name = unquote(raw_name)
        except Exception:
            pass
        filename = safe_filename(raw_name)
        target_path = unique_path(save_dir, filename)
        ok = _stream_one_file_body(reader, boundary_bytes, target_path, on_progress)
        if ok:
            saved.append(os.path.basename(target_path))
            if on_progress:
                on_progress(0)

        # 消费 boundary 行及其换行；若为 end_marker 则结束
        if not _consume_boundary_line(reader, end_marker):
            break

    return saved


def _stream_one_file_body(reader, boundary_bytes, target_path, on_progress):
    """
    从 reader 读取 part body 直到 boundary（不含 boundary），
    流式写入 target_path。返回 True 表示正常结束。
    """
    out = open(target_path, "wb")
    finished = False
    try:
        while True:
            # 缓冲区不足时填充
            if reader.buf.find(boundary_bytes) == -1 and len(reader.buf) < len(boundary_bytes):
                n = reader._fill()
                if n == 0:
                    # 连接中断，未找到 boundary
                    break
            idx = reader.buf.find(boundary_bytes)
            if idx != -1:
                # boundary 之前是 body（末尾会带 \r\n）
                body = reader.buf[:idx]
                if body.endswith(b"\r\n"):
                    body = body[:-2]
                out.write(body)
                # 从缓冲区移除已写出部分（boundary 本身留给上层处理）
                reader.buf = reader.buf[idx:]
                finished = True
                break
            else:
                # buf 内还没出现 boundary：写出「肯定安全」的前段，
                # 保留尾部 (len(boundary)-1) 字节防止跨块 boundary 被截断
                safe = len(reader.buf) - (len(boundary_bytes) - 1)
                if safe > 0:
                    out.write(reader.buf[:safe])
                    reader.buf = reader.buf[safe:]
                if on_progress:
                    on_progress(len(reader.buf))
    finally:
        out.close()
        if not finished:
            # 中断的半成品清理
            try:
                os.remove(target_path)
            except OSError:
                pass
    return finished


def _drain_until_boundary(reader, boundary_bytes):
    """丢弃内容直到缓冲区里出现 boundary（保留 boundary 在缓冲区）"""
    while reader.buf.find(boundary_bytes) == -1:
        if reader._fill() == 0:
            return


def _consume_boundary_line(reader, end_marker):
    """
    缓冲区当前位置应位于一个 boundary 起始处。
    消费掉该 boundary 行（含其后的 \r\n）。
    返回 True 表示后面还有 part（普通 boundary），False 表示已到 end_marker。
    """
    # 确保读到换行
    while b"\n" not in reader.buf:
        if reader._fill() == 0:
            return False
    line = reader.readline()
    if line.strip().startswith(end_marker):
        return False
    return True


# ======================== HTTP Handler ========================

class Handler(BaseHTTPRequestHandler):
    server_version = "FileTransfer/1.0"
    # 使用 HTTP/1.0：每个请求处理完即关闭连接，响应边界清晰，
    # 客户端（含手机浏览器）能立即识别响应结束，避免 keep-alive 挂起。
    protocol_version = "HTTP/1.0"

    # ---- 通用 ----
    def log_message(self, fmt, *args):
        # --windowed exe 下 sys.stderr 可能为 None，写入会导致请求崩溃
        if sys.stderr is None:
            return
        ts = datetime.now().strftime("%H:%M:%S")
        sys.stderr.write(f"[{ts}] {self.address_string()} - {fmt % args}\n")

    def _check_token(self):
        if Config.token is None:
            return True
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        t = qs.get("t", [None])[0]
        if t == Config.token:
            return True
        self.send_json({"error": "unauthorized"}, status=403)
        return False

    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- GET ----
    def do_GET(self):
        if not self._check_token():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/" or path == "/index.html":
            self.send_html(PAGE_HTML)
            return

        if path == "/files":
            self.handle_list_files()
            return

        if path == "/download":
            self.handle_download(parsed.query)
            return

        self.send_json({"error": "not found"}, status=404)

    # ---- POST ----
    def do_POST(self):
        if not self._check_token():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            self.handle_upload()
            return
        self.send_json({"error": "not found"}, status=404)

    # ---- 业务 ----
    def handle_list_files(self):
        items = []
        try:
            for name in os.listdir(Config.share_dir):
                full = os.path.join(Config.share_dir, name)
                if not os.path.isfile(full):
                    continue
                st = os.stat(full)
                items.append({
                    "name": name,
                    "size": st.st_size,
                    "size_text": human_size(st.st_size),
                    "mtime": int(st.st_mtime),
                    "time_text": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
        except Exception as e:
            self.send_json({"error": str(e)}, status=500)
            return
        items.sort(key=lambda x: x["mtime"], reverse=True)
        self.send_json({"files": items})

    def handle_download(self, query):
        qs = parse_qs(query)
        name = qs.get("name", [None])[0]
        if not name:
            self.send_json({"error": "missing name"}, status=400)
            return
        name = safe_filename(name)
        full = os.path.join(Config.share_dir, name)
        if not os.path.isfile(full):
            self.send_json({"error": "file not found"}, status=404)
            return
        try:
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{name}"; filename*=UTF-8\'\'{name}',
            )
            self.end_headers()
            with open(full, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except ConnectionError:
            pass

    def handle_upload(self):
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self.send_json({"error": "bad content-type"}, status=400)
            return
        m = re.search(r"boundary=(.+)", ctype)
        if not m:
            self.send_json({"error": "bad boundary"}, status=400)
            return
        boundary = m.group(1).strip().strip('"')
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            self.send_json({"error": "empty body"}, status=411)
            return

        received = [0]

        def on_progress(n):
            received[0] += n

        try:
            saved = parse_multipart_stream(
                self.rfile, boundary, Config.share_dir, on_progress
            )
        except Exception as e:
            self.send_json({"error": f"upload failed: {e}"}, status=500)
            return

        if not saved:
            self.send_json({"error": "no file saved"}, status=400)
            return
        self.send_json({"saved": saved, "count": len(saved)})
        # 通知 GUI 或打印到终端
        for fn in saved:
            if Config.notify_callback:
                Config.notify_callback(fn, None)
            else:
                print(f"  \u2713 收到文件: {fn}")


# ======================== 前端页面 ========================

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>局域网文件互传</title>
<style>
  :root{
    --bg:#f4f6fb; --card:#ffffff; --primary:#4f7cff; --primary-d:#3a5fd0;
    --text:#1f2430; --muted:#8a93a6; --border:#e6e9f2; --ok:#22a95b; --err:#e5544b;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
       background:var(--bg);color:var(--text);padding-bottom:40px}
  .wrap{max-width:560px;margin:0 auto;padding:16px}
  header{text-align:center;padding:18px 0 8px}
  header h1{font-size:20px;margin:0 0 4px}
  header p{margin:0;color:var(--muted);font-size:13px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:14px;
        padding:18px;margin:14px 0;box-shadow:0 2px 10px rgba(20,30,60,.04)}
  .card h2{font-size:15px;margin:0 0 12px;display:flex;align-items:center;gap:6px}
  .drop{border:2px dashed var(--border);border-radius:12px;padding:24px 12px;text-align:center;
        color:var(--muted);transition:.2s;cursor:pointer;background:#fbfcff}
  .drop.on{border-color:var(--primary);background:#eef3ff;color:var(--primary)}
  .drop svg{width:38px;height:38px;display:block;margin:0 auto 8px}
  .btn{display:inline-block;width:100%;padding:13px;border:none;border-radius:10px;
       background:var(--primary);color:#fff;font-size:15px;font-weight:600;cursor:pointer;margin-top:12px}
  .btn:active{background:var(--primary-d)}
  .btn:disabled{background:#b9c2d6;cursor:not-allowed}
  .file-list{list-style:none;margin:0;padding:0}
  .file-list li{display:flex;align-items:center;gap:10px;padding:10px;border-radius:10px;border:1px solid var(--border);
        margin-bottom:8px;background:#fff;text-decoration:none;color:var(--text)}
  .file-list li:active{background:#f0f3ff}
  .ficon{flex:0 0 30px;height:30px;border-radius:8px;background:#eef3ff;display:flex;align-items:center;justify-content:center;color:var(--primary)}
  .finfo{flex:1;min-width:0}
  .fname{font-size:14px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .fmeta{font-size:12px;color:var(--muted);margin-top:2px}
  .empty{text-align:center;color:var(--muted);font-size:13px;padding:18px 0}
  .progress{height:6px;background:var(--border);border-radius:6px;overflow:hidden;margin-top:10px;display:none}
  .progress span{display:block;height:100%;width:0;background:var(--primary);transition:width .2s}
  #toast{position:fixed;left:50%;bottom:30px;transform:translateX(-50%);
         background:rgba(20,25,40,.92);color:#fff;padding:10px 16px;border-radius:8px;
         font-size:13px;opacity:0;pointer-events:none;transition:.25s;z-index:99}
  #toast.show{opacity:1}
  .ok{color:var(--ok)} .err{color:var(--err)}
  .tabs{display:flex;gap:8px;margin:6px 0 0}
  .tab{flex:1;text-align:center;padding:10px;border-radius:10px;background:#fff;border:1px solid var(--border);
       font-size:14px;font-weight:600;cursor:pointer;color:var(--muted)}
  .tab.active{background:var(--primary);color:#fff;border-color:var(--primary)}
  .hidden{display:none}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>📱 ⟷ 💻 局域网文件互传</h1>
    <p>同一 WiFi 下，上传到电脑 / 下载电脑文件</p>
  </header>

  <div class="tabs">
    <div class="tab active" data-tab="up">⬆️ 上传到电脑</div>
    <div class="tab" data-tab="down">⬇️ 电脑上的文件</div>
  </div>

  <!-- 上传 -->
  <section id="tab-up" class="card">
    <div class="drop" id="drop">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
        <path d="M12 16V4M12 4l-4 4M12 4l4 4" stroke-linecap="round" stroke-linejoin="round"/>
        <path d="M4 16v2a2 2 0 002 2h12a2 2 0 002-2v-2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <div>点击选择文件，或拖拽到这里</div>
      <div style="font-size:12px;margin-top:4px">支持多选 / 大文件</div>
      <input type="file" id="fileInput" multiple style="display:none">
    </div>
    <div class="progress" id="progress"><span id="progressBar"></span></div>
    <button class="btn" id="uploadBtn" disabled>开始上传</button>
  </section>

  <!-- 下载列表 -->
  <section id="tab-down" class="card hidden">
    <h2>📁 电脑共享的文件</h2>
    <ul class="file-list" id="fileList"><li class="empty">加载中...</li></ul>
    <button class="btn" id="refreshBtn" style="background:#fff;color:var(--primary);border:1px solid var(--primary)">刷新列表</button>
  </section>
</div>
<div id="toast"></div>

<script>
const TOKEN = new URLSearchParams(location.search).get('t') || '';
const qs = TOKEN ? ('?t=' + TOKEN) : '';
function apiUrl(p){ return p + qs; }
function toast(msg, isErr){
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'show' + (isErr ? ' err' : '');
  setTimeout(()=>{ t.className=''; }, 2500);
}
// tab 切换
document.querySelectorAll('.tab').forEach(el=>{
  el.addEventListener('click', ()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    el.classList.add('active');
    const tab = el.dataset.tab;
    document.getElementById('tab-up').classList.toggle('hidden', tab!=='up');
    document.getElementById('tab-down').classList.toggle('hidden', tab!=='down');
    if(tab==='down') loadFiles();
  });
});
// 文件选择
let selected = [];
const drop = document.getElementById('drop');
const fileInput = document.getElementById('fileInput');
const uploadBtn = document.getElementById('uploadBtn');
drop.addEventListener('click', ()=> fileInput.click());
fileInput.addEventListener('change', ()=>{
  selected = Array.from(fileInput.files);
  updateDropText();
});
['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev, e=>{e.preventDefault();drop.classList.add('on');}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev, e=>{e.preventDefault();drop.classList.remove('on');}));
drop.addEventListener('drop', e=>{
  selected = Array.from(e.dataTransfer.files);
  updateDropText();
});
function updateDropText(){
  if(selected.length===0){ uploadBtn.disabled=true; return; }
  uploadBtn.disabled = false;
  const total = selected.reduce((s,f)=>s+f.size,0);
  drop.querySelector('div').textContent = `已选 ${selected.length} 个文件`;
  drop.querySelectorAll('div')[1].textContent = formatSize(total);
}
function formatSize(n){
  const u=['B','KB','MB','GB'];
  for(const x of u){ if(n<1024) return n.toFixed(1)+' '+x; n/=1024; }
  return n.toFixed(1)+' TB';
}
// 上传
uploadBtn.addEventListener('click', async ()=>{
  if(selected.length===0) return;
  uploadBtn.disabled = true;
  const prog = document.getElementById('progress');
  const bar = document.getElementById('progressBar');
  prog.style.display = 'block'; bar.style.width = '0%';
  const fd = new FormData();
  for(const f of selected) fd.append('files', f);
  const xhr = new XMLHttpRequest();
  xhr.open('POST', apiUrl('/upload'));
  xhr.upload.onprogress = e=>{
    if(e.lengthComputable) bar.style.width = (e.loaded/e.total*100).toFixed(1)+'%';
  };
  xhr.onload = ()=>{
    prog.style.display='none';
    uploadBtn.disabled = false;
    if(xhr.status===200){
      let cnt = selected.length;
      try{ cnt = JSON.parse(xhr.responseText).count || cnt; }catch(_){}
      toast('✓ 上传成功 ' + cnt + ' 个文件');
      selected = []; fileInput.value=''; updateDropText();
    } else {
      toast('上传失败: ' + xhr.status, true);
    }
  };
  xhr.onerror = ()=>{ prog.style.display='none'; uploadBtn.disabled=false; toast('网络错误', true); };
  xhr.send(fd);
});
// 文件列表
async function loadFiles(){
  const list = document.getElementById('fileList');
  list.innerHTML = '<li class="empty">加载中...</li>';
  try{
    const r = await fetch(apiUrl('/files'));
    const data = await r.json();
    if(!data.files || data.files.length===0){
      list.innerHTML = '<li class="empty">电脑端暂无共享文件</li>';
      return;
    }
    list.innerHTML = '';
    data.files.forEach(f=>{
      const li = document.createElement('a');
      li.className = 'file-list li-as-anchor';
      li.href = apiUrl('/download') + '&name=' + encodeURIComponent(f.name);
      li.innerHTML = `<span class="ficon">📄</span>
        <span class="finfo">
          <div class="fname"></div>
          <div class="fmeta">${f.size_text} · ${f.time_text}</div>
        </span>`;
      li.querySelector('.fname').textContent = f.name;
      li.style.display='flex';
      li.style.alignItems='center';
      li.style.gap='10px';
      li.style.padding='10px';
      li.style.borderRadius='10px';
      li.style.border='1px solid var(--border)';
      li.style.marginBottom='8px';
      li.style.background='#fff';
      li.style.textDecoration='none';
      li.style.color='var(--text)';
      list.appendChild(li);
    });
  }catch(e){
    list.innerHTML = '<li class="empty err">加载失败</li>';
  }
}
document.getElementById('refreshBtn').addEventListener('click', loadFiles);
</script>
</body>
</html>
"""


# ======================== 二维码生成（公用） ========================

def make_qr_image(url, size=320):
    """生成二维码 PIL 图片，返回 Image 对象"""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    return qr.make_image().resize((size, size), Image.NEAREST)


def terminal_show_qr(url):
    """终端模式：纯 ASCII 二维码 + 保存 PNG"""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)

    matrix = qr.get_matrix()
    modules = len(matrix)
    dark = "##"
    light = "  "

    print("\n" + "=" * 56)
    print("  扫描下面的二维码，用手机打开：")
    print("  (如果终端显示不佳，请打开同目录下的 qrcode.png)")
    print("=" * 56 + "\n")

    for _ in range(2):
        print("    " + light * (modules + 4))
    for row in matrix:
        line = "    " + light * 2
        for cell in row:
            line += dark if cell else light
        line += light * 2
        print(line)
        print(line)
    for _ in range(2):
        print("    " + light * (modules + 4))

    try:
        img = qr.make_image()
        img.save(os.path.join(os.getcwd(), "qrcode.png"))
        print(f"\n（二维码图片已保存到：qrcode.png）")
    except Exception:
        pass


# ======================== GUI 窗口（Tkinter）========================

class GuiApp:
    """Tkinter 图形界面窗口"""

    def __init__(self, url, port, share_dir, token):
        self.url = url
        self.port = port
        self.share_dir = share_dir
        self.token = token
        self.server = None
        self.server_ok = False

        self.root = tk.Tk()
        self.root.title("局域网文件互传")
        self.root.resizable(False, False)

        # 居中
        w, h = 420, 600
        ws = self.root.winfo_screenwidth()
        hs = self.root.winfo_screenheight()
        x = (ws - w) // 2
        y = (hs - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # 配色
        self.bg = "#f0f2f5"
        self.card = "#ffffff"
        self.primary = "#4f7cff"
        self.text = "#1a1c24"
        self.muted = "#8a93a6"
        self.ok = "#22a95b"
        self.err = "#d93a4b"

        self.root.configure(bg=self.bg)
        self._build_ui()

        # 注册通知回调（Handler 发文件时调用）
        Config.notify_callback = self.on_file_received

        # 处理窗口关闭
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 启动后台服务器（先绑定端口，再开守护线程跑 serve_forever）
        self._start_server()
        if self.server_ok:
            self._health_check()

    # ----- UI 构建 -----
    def _build_ui(self):
        # 标题栏
        title_frame = tk.Frame(self.root, bg=self.bg)
        title_frame.pack(fill=tk.X, pady=(14, 0))
        tk.Label(
            title_frame, text="\U0001F4F1 \u27f7 \U0001F4BB  局域网文件互传",
            font=("Microsoft YaHei", 13, "bold"), bg=self.bg, fg=self.text,
        ).pack()

        tk.Label(
            title_frame,
            text="同一 WiFi 下，手机扫码即可上传 / 下载",
            font=("Microsoft YaHei", 9), bg=self.bg, fg=self.muted,
        ).pack(pady=(2, 0))

        # 二维码卡片
        card = tk.Frame(self.root, bg=self.card, bd=0, highlightthickness=1,
                        highlightbackground="#e6e9f2", highlightcolor="#e6e9f2")
        card.pack(pady=(12, 8), padx=20, fill=tk.X)

        qr_img = make_qr_image(self.url, size=260)
        self.qr_tk = ImageTk.PhotoImage(qr_img)
        tk.Label(card, image=self.qr_tk, bg=self.card).pack(pady=(14, 0))

        # 访问地址（可复制）
        url_frame = tk.Frame(card, bg=self.card)
        url_frame.pack(pady=(8, 4))
        tk.Label(
            url_frame, text="访问地址：", font=("Microsoft YaHei", 9),
            bg=self.card, fg=self.muted,
        ).pack(side=tk.LEFT)
        self.url_var = tk.StringVar(value=self.url)
        url_entry = tk.Entry(
            url_frame, textvariable=self.url_var, font=("Consolas", 9),
            bg="#f8f9fc", fg=self.text, relief=tk.FLAT, width=38,
            readonlybackground="#f8f9fc",
        )
        url_entry.pack(side=tk.LEFT, padx=(4, 0))
        url_entry.configure(state="readonly")

        # 按钮行
        btn_frame = tk.Frame(card, bg=self.card)
        btn_frame.pack(pady=(6, 12))
        self._btn(btn_frame, "\U0001F4CB 复制地址",
                  lambda: self._copy_url(), tk.LEFT)
        self._btn(btn_frame, "\U0001F310 打开浏览器",
                  lambda: webbrowser.open(self.url), tk.LEFT)

        # 状态信息
        info_card = tk.Frame(self.root, bg=self.card, bd=0, highlightthickness=1,
                             highlightbackground="#e6e9f2", highlightcolor="#e6e9f2")
        info_card.pack(pady=(0, 8), padx=20, fill=tk.X)

        tk.Label(
            info_card, text=f"\U0001F4C1 共享目录：{self.share_dir}",
            font=("Microsoft YaHei", 9), bg=self.card, fg=self.muted, anchor="w",
        ).pack(fill=tk.X, padx=14, pady=(10, 0))

        tk.Label(
            info_card,
            text=f"\U0001F310 本机 IP：{get_lan_ip()}   端口：{self.port}",
            font=("Microsoft YaHei", 9), bg=self.card, fg=self.muted, anchor="w",
        ).pack(fill=tk.X, padx=14, pady=(4, 0))

        tk.Label(
            info_card,
            text=f"\U0001F512 令牌：{self.token if self.token else '(已关闭)'}",
            font=("Microsoft YaHei", 9), bg=self.card, fg=self.muted, anchor="w",
        ).pack(fill=tk.X, padx=14, pady=(4, 0))

        # 服务状态
        self.status_var = tk.StringVar(value="\u23f3 正在启动服务...")
        tk.Label(
            info_card,
            textvariable=self.status_var,
            font=("Microsoft YaHei", 9, "bold"), bg=self.card, fg=self.muted, anchor="w",
        ).pack(fill=tk.X, padx=14, pady=(4, 10))

        # 上传日志
        log_card = tk.Frame(self.root, bg=self.card, bd=0, highlightthickness=1,
                            highlightbackground="#e6e9f2", highlightcolor="#e6e9f2")
        log_card.pack(pady=(0, 12), padx=20, fill=tk.BOTH, expand=True)

        tk.Label(
            log_card, text="\U0001F4E5 上传记录",
            font=("Microsoft YaHei", 10, "bold"), bg=self.card, fg=self.text,
            anchor="w",
        ).pack(fill=tk.X, padx=14, pady=(10, 4))

        self.log_text = scrolledtext.ScrolledText(
            log_card, height=6, font=("Consolas", 9), bg="#f8f9fc",
            fg=self.text, relief=tk.FLAT, state=tk.DISABLED, wrap=tk.WORD,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        self._log("等待手机连接...", self.muted)

    def _btn(self, parent, text, cmd, side):
        b = tk.Button(
            parent, text=text, font=("Microsoft YaHei", 9),
            bg=self.card, fg=self.primary, bd=0, cursor="hand2",
            activebackground="#eef3ff", activeforeground=self.primary,
            command=cmd,
        )
        b.pack(side=side, padx=(0, 12))
        return b

    def _copy_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url)
        self._log("\u2714 地址已复制到剪贴板", self.ok)

    def _log(self, msg, color=None):
        self.log_text.configure(state=tk.NORMAL)
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.insert(tk.END, f"[{ts}] {msg}\n")
        if color:
            # 给最新行加颜色
            line_start = self.log_text.index("end-2l")
            line_end = self.log_text.index("end-1l")
            self.log_text.tag_add(color, line_start, line_end)
            self.log_text.tag_config(color, foreground=color)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ----- 服务器 -----
    def _start_server(self):
        """在主线程创建服务器（捕获绑定错误），serve_forever 放入守护线程"""
        try:
            self.server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        except OSError as e:
            self.server_ok = False
            self.status_var.set(f"\u274c 服务启动失败：端口 {self.port} 被占用")
            self.root.after(0, lambda: messagebox.showerror(
                "服务启动失败",
                f"无法绑定端口 {self.port}。\n\n"
                f"错误信息：{e}\n\n"
                "请检查：\n"
                "  1) 是否有其他程序占用了该端口\n"
                "  2) 可尝试使用 --port 参数更换端口"
            ))
            self._log(f"\u274c 服务启动失败：{e}", self.err)
            return

        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.server_ok = True

    def _health_check(self):
        """启动后快速自检：GET 本机确认服务可达"""
        def _check():
            try:
                req = Request(
                    f"http://127.0.0.1:{self.port}/",
                    headers={"Host": "127.0.0.1"}
                )
                urlopen(req, timeout=5)
                self.root.after(0, lambda: self.status_var.set(
                    "\u2705 服务运行中 — 请手机扫码访问"
                ))
                self._log("\u2705 服务自检通过，等待连接...", self.ok)
            except Exception as e:
                # 服务可能启动但被防火墙阻止，或偶发延迟
                self.root.after(0, lambda: self.status_var.set(
                    "\u26a0\ufe0f 服务可能被防火墙阻止，请检查"
                ))
                self._log(
                    f"\u26a0\ufe0f 本地自检失败（{e}），"
                    "如其它设备无法访问，请放行 Windows 防火墙", self.err
                )
        threading.Thread(target=_check, daemon=True).start()

    def on_file_received(self, filename, _unused=None):
        """Handler 回调（在 HTTP 线程里），通过 root.after 回 UI 线程"""
        self.root.after(0, lambda: self._log(f"\u2b07 收到：{filename}", self.ok))

    def _on_close(self):
        if self.server and self.server_ok:
            self.server.shutdown()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ======================== 主入口 ========================

def main():
    parser = argparse.ArgumentParser(description="局域网文件互传小工具")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000）")
    parser.add_argument("--dir", default=None, help="接收/共享目录（默认 ./uploads）")
    parser.add_argument("--no-token", action="store_true", help="关闭令牌校验")
    parser.add_argument("--no-gui", action="store_true", help="强制使用终端模式")
    args = parser.parse_args()

    Config.host_port = args.port
    Config.share_dir = os.path.abspath(
        args.dir or os.path.join(os.getcwd(), "uploads")
    )
    os.makedirs(Config.share_dir, exist_ok=True)
    Config.token = None if args.no_token else uuid.uuid4().hex[:6]

    lan_ip = get_lan_ip()
    if Config.token:
        url = f"http://{lan_ip}:{args.port}/?t={Config.token}"
    else:
        url = f"http://{lan_ip}:{args.port}/"

    # ----- GUI 模式 -----
    if GUI_AVAILABLE and not args.no_gui:
        # safe_print: --windowed exe 下 sys.stdout 可能为 None
        def _p(*a, **kw):
            try:
                print(*a, **kw)
            except Exception:
                pass
        _p(f"共享目录 : {Config.share_dir}")
        _p(f"本机 IP  : {lan_ip}")
        _p(f"端口     : {args.port}")
        _p(f"令牌     : {Config.token if Config.token else '(已关闭)'}")
        _p(f"访问地址 : {url}")
        _p("\nGUI 窗口已打开，关闭窗口或 Ctrl+C 退出。")
        app = GuiApp(url, args.port, Config.share_dir, Config.token)
        try:
            app.run()
        except KeyboardInterrupt:
            _p("\n已退出。")
        return

    # ----- 终端模式 -----
    print("=" * 52)
    print("        局域网文件互传工具（终端模式）")
    print("=" * 52)
    print(f"  共享目录 : {Config.share_dir}")
    print(f"  本机 IP  : {lan_ip}")
    print(f"  端口     : {args.port}")
    print(f"  令牌     : {Config.token if Config.token else '(已关闭)'}")
    print(f"  访问地址 : {url}")
    print("-" * 52)

    terminal_show_qr(url)

    print("\n手机和电脑需连接同一 WiFi。按 Ctrl+C 退出。\n")

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
        server.shutdown()


if __name__ == "__main__":
    main()
