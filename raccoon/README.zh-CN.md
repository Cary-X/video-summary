# Raccoon Agent 独立版

在 Windows x64 中双击 **Start Raccoon Agent.cmd** 打开网页，
或双击 **Start Raccoon Drop.cmd** (Raccoon Box) 打开桌面投递窗。
整个文件夹一起备份或迁移，保留隐藏的 .env 文件。

- frontend 和 backend 存放程序。
- runtime 包含 Python、OCR 依赖和 FFmpeg 工具。
- data 包含本地模型、用户数据库、上传文件和证据。
- .env 包含私有 API 配置。分享项目时请保护 .env 和 data。

本地模型支持 CPU。GPU 加速取决于显卡驱动及相关运行库。
网络视频、网页和云端 API 仍需要联网。
通过网页设置输入的摘要 API Key 需要在服务重启后重新输入。

## 从 GitHub 源码运行

安装 64 位 Python 3.12 和 FFmpeg，并确保 FFprobe 在 PATH 中。在 `raccoon` 目录运行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m backend.server
```

打开 http://127.0.0.1:8507/。如需打开 Raccoon Box，保持网页服务运行，
再在另一个终端执行 `.\.venv\Scripts\python.exe desktop_widget.py`。
双击启动器使用独立版自带的运行环境。GitHub 提供源码和图片，运行环境和本地模型需要另行安装。
