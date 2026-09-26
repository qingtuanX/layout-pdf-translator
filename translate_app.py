#!/usr/bin/env python3
"""拖拽式 PDF 翻译器：把 PDF 拖进窗口，自动生成中文译文。

三种入队方式都走同一个队列：拖进窗口、拖到 exe 图标上、命令行传文件名。
翻译引擎在 translate_pdf.py，这里只负责界面、队列和配置。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from translate_pdf import TranslationCancelled, TranslationSettings, translate_file

try:  # 没装 tkinterdnd2 也能跑，只是不能拖拽
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # pragma: no cover
    DND_FILES = "DND_Files"
    TkinterDnD = None

APP_NAME = "PDF 排版翻译器"
LOGGER = logging.getLogger("pdf-translator")
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "layout-pdf-translator"
CONFIG_PATH = CONFIG_DIR / "config.json"
CACHE_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "layout-pdf-translator" / "cache"

DEFAULT_CONFIG: dict[str, object] = {
    "base_url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "api_key": "",
    "output_mode": "same_dir",
    "output_dir": "",
    "overwrite": False,
}
HINT = "把 PDF 拖到这里（可多选，也可以拖文件夹）\n或点这里选择文件"
LOG_LINES = 400
PENDING_STATES = ("等待中", "翻译中")
RETRY_STATES = ("等待中", "已停止", "失败")


def load_config() -> dict[str, object]:
    config = dict(DEFAULT_CONFIG)
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return config
    except (OSError, json.JSONDecodeError) as exc:
        print(f"配置文件读取失败，改用默认值: {exc}", file=sys.stderr)
        return config
    if isinstance(loaded, dict):
        config.update({key: loaded[key] for key in DEFAULT_CONFIG if key in loaded})
    return config


def save_config(config: dict[str, object]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def enable_dpi_awareness() -> None:
    """高分屏下窗口不发虚。"""
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001 - 非 Windows 或老系统直接跳过
        pass


def collect_pdfs(paths: list[Path]) -> list[Path]:
    """展开文件夹，丢掉非 PDF 和已经是译文的文件。"""
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(path.rglob("*.pdf")))
        elif path.is_file() and path.suffix.lower() == ".pdf":
            found.append(path)
    seen: set[Path] = set()
    result: list[Path] = []
    for path in found:
        if path.name.endswith(".zh-CN.pdf") or path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result


def cache_dir_for(source: Path) -> Path:
    """命令行翻过的文件直接复用它的缓存，别重复花钱。"""
    cli_cache = source.parent / "output" / ".cache"
    if (cli_cache / f"{source.stem}.json").is_file():
        return cli_cache
    return CACHE_DIR


class Job:
    def __init__(self, path: Path):
        self.path = path
        self.status = "等待中"
        self.output: Path | None = None
        self.detail = ""


class QueueLogHandler(logging.Handler):
    """把翻译过程的日志丢进队列，交给界面线程追加显示。"""

    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink(self.format(record))
        except Exception:  # noqa: BLE001
            pass


class TranslatorApp:
    def __init__(self, root: tk.Tk, initial: list[Path], config: dict[str, object]):
        self.root = root
        self.config = config
        self.jobs: list[Job] = []
        self.messages: queue.Queue[tuple] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.dnd_ready = False
        self._build_ui()
        self._install_log_handler()
        self.root.after(120, self._drain)
        self._log(f"拖拽{'已就绪' if self.dnd_ready else '不可用（请用“选择文件”，或把 PDF 拖到 exe 图标上）'}")
        if initial:
            # 等窗口画出来再入队，免得缺密钥时设置窗先弹出来。
            self.root.after(300, lambda: self.enqueue(initial, autostart=True))

    # ---------- 界面 ----------

    def _build_ui(self) -> None:
        self.root.title(APP_NAME)
        self.root.geometry("880x620")
        self.root.minsize(700, 480)
        self._build_drop_zone()
        self._build_job_list()
        self._build_log_pane()
        self._build_buttons()

    def _build_drop_zone(self) -> None:
        frame = tk.Frame(self.root, highlightthickness=2, highlightbackground="#9aa0a6")
        frame.pack(fill="x", padx=12, pady=(12, 8))
        label = tk.Label(frame, text=HINT, height=4, justify="center", fg="#3c4043")
        label.pack(fill="both", expand=True)
        frame.bind("<Button-1>", lambda _event: self._choose_files())
        label.bind("<Button-1>", lambda _event: self._choose_files())
        if TkinterDnD is None:
            label.configure(text=HINT + "\n（拖拽不可用：没装 tkinterdnd2，请用点选）")
            return
        for widget in (frame, label):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
                self.dnd_ready = True
            except Exception:  # noqa: BLE001 - 注册失败不影响点选，但要让用户看见
                pass
        if not self.dnd_ready:
            label.configure(text=HINT + "\n（拖拽不可用：tkdnd 没加载成功，请用点选）")

    def _build_job_list(self) -> None:
        columns = ("file", "status", "output")
        self.tree = ttk.Treeview(self.root, columns=columns, show="headings", height=10)
        for name, title, width in (("file", "文件", 300), ("status", "状态", 150), ("output", "输出", 400)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor="w", stretch=True)
        self.tree.pack(fill="both", expand=False, padx=12, pady=(0, 8))
        self.tree.bind("<Double-1>", self._open_selected)

    def _build_log_pane(self) -> None:
        frame = tk.Frame(self.root)
        frame.pack(fill="both", expand=True, padx=12)
        scroll = tk.Scrollbar(frame)
        scroll.pack(side="right", fill="y")
        self.log = tk.Text(frame, height=9, wrap="none", state="disabled", yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.configure(command=self.log.yview)

    def _build_buttons(self) -> None:
        bar = tk.Frame(self.root)
        bar.pack(fill="x", padx=12, pady=10)
        tk.Button(bar, text="设置", width=10, command=self._open_settings).pack(side="left")
        self.start_button = tk.Button(bar, text="开始", width=10, command=self.start)
        self.start_button.pack(side="left", padx=6)
        self.stop_button = tk.Button(bar, text="停止", width=10, command=self.stop, state="disabled")
        self.stop_button.pack(side="left")
        tk.Button(bar, text="清空已完成", width=12, command=self._clear_finished).pack(side="left", padx=6)
        self.status_label = tk.Label(bar, text="等待拖入 PDF", anchor="e")
        self.status_label.pack(side="right")

    # ---------- 入队 ----------

    def _on_drop(self, event) -> None:
        try:
            paths = [Path(item) for item in self.root.tk.splitlist(event.data)]
        except Exception:  # noqa: BLE001
            return
        self.enqueue(paths, autostart=True)

    def _choose_files(self) -> None:
        chosen = filedialog.askopenfilenames(title="选择要翻译的 PDF", filetypes=[("PDF", "*.pdf")])
        if chosen:
            self.enqueue([Path(item) for item in chosen], autostart=True)

    def enqueue(self, paths: list[Path], *, autostart: bool) -> None:
        known = {job.path for job in self.jobs}
        pdfs = [path for path in collect_pdfs(paths) if path not in known]
        if not pdfs:
            self._log("没有新的 PDF 可加入（译文 .zh-CN.pdf 会自动跳过）")
            return
        for path in pdfs:
            job = Job(path)
            self.jobs.append(job)
            self.tree.insert("", "end", iid=self._iid(job), values=(str(path), job.status, ""))
        self._refresh_status()
        if autostart:
            self.start()

    # ---------- 队列线程 ----------

    def start(self) -> None:
        pending = [job for job in self.jobs if job.status in RETRY_STATES]
        if not pending:
            self._log("没有待翻译的文件")
            return
        if self.worker is not None and self.worker.is_alive():
            self._log("正在翻译中，新加入的文件排在后面")
            return
        if not str(self.config.get("api_key", "")).strip():
            self._log("还没配置 API 密钥，先填一下设置")
            self._open_settings()
            return
        self.stop_event.clear()
        self.worker = threading.Thread(target=self._run_queue, args=(pending,), daemon=True)
        self.worker.start()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._set_status(f"翻译中：0/{len(pending)}")

    def stop(self) -> None:
        self.stop_event.set()
        self._set_status("正在停止（当前批次跑完后停）")

    def _run_queue(self, pending: list[Job]) -> None:
        try:
            for position, job in enumerate(pending):
                if self.stop_event.is_set():
                    self._post_job(job, "已停止")
                    continue
                self._post_job(job, "翻译中")
                self._post(("status", f"翻译中：{position + 1}/{len(pending)} · {job.path.name}"))
                try:
                    result = translate_file(
                        job.path,
                        self._output_dir(job),
                        self._settings(job.path),
                        should_stop=self.stop_event.is_set,
                    )
                except TranslationCancelled as exc:
                    self._post_job(job, "已停止", str(exc))
                    for rest in pending[position + 1 :]:
                        self._post_job(rest, "已停止")
                    return
                except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响队列
                    LOGGER.error("处理失败: %s", exc)
                    self._post_job(job, "失败", str(exc)[:200])
                    continue
                job.output = result.get("output") if isinstance(result, dict) else None
                self._post_job(
                    job,
                    {
                        "done": "完成",
                        "skipped": "已跳过（输出已存在）",
                        "empty": "跳过（无可提取文字）",
                        "dry-run": "试运行完成",
                    }.get(str(result.get("status", "")), "完成"),
                )
        finally:
            self._post(("finished", None))

    def _settings(self, source: Path) -> TranslationSettings:
        return TranslationSettings(
            model=str(self.config.get("model", "")),
            base_url=str(self.config.get("base_url", "")),
            api_key=str(self.config.get("api_key", "")),
            overwrite=bool(self.config.get("overwrite", False)),
            cache_dir=cache_dir_for(source),
        )

    def _output_dir(self, job: Job) -> Path:
        custom = str(self.config.get("output_dir", "")).strip()
        if self.config.get("output_mode") == "custom" and custom:
            return Path(custom)
        return job.path.parent

    # ---------- 主线程：消息与刷新 ----------

    def _iid(self, job: Job) -> str:
        return str(id(job))

    def _post(self, message: tuple) -> None:
        self.messages.put(message)

    def _post_job(self, job: Job, status: str, detail: str = "") -> None:
        self._post(("job", job, status, detail))

    def _drain(self) -> None:
        try:
            while True:
                message = self.messages.get_nowait()
                kind = message[0]
                if kind == "log":
                    self._log(message[1])
                elif kind == "status":
                    self._set_status(message[1])
                elif kind == "job":
                    self._apply_job(*message[1:])
                elif kind == "finished":
                    self._finish()
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _apply_job(self, job: Job, status: str, detail: str) -> None:
        job.status = status
        job.detail = detail
        index = self._iid(job)
        if self.tree.exists(index):
            self.tree.item(index, values=(str(job.path), status, str(job.output or "")))
        self._refresh_status()

    def _finish(self) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        done = sum(1 for job in self.jobs if job.status == "完成")
        failed = sum(1 for job in self.jobs if job.status == "失败")
        self._set_status(f"结束：完成 {done}，失败 {failed}，共 {len(self.jobs)} 个文件")

    def _refresh_status(self) -> None:
        waiting = sum(1 for job in self.jobs if job.status in PENDING_STATES)
        if waiting and not self.stop_event.is_set():
            self._set_status(f"队列中还有 {waiting} 个文件")

    def _set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        excess = int(self.log.index("end-1c").split(".")[0]) - LOG_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _install_log_handler(self) -> None:
        LOGGER.setLevel(logging.INFO)
        LOGGER.propagate = False
        handler = QueueLogHandler(lambda text: self.messages.put(("log", text)))
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        LOGGER.addHandler(handler)

    def _clear_finished(self) -> None:
        self.jobs = [job for job in self.jobs if job.status in PENDING_STATES]
        self.tree.delete(*self.tree.get_children())
        for job in self.jobs:
            self.tree.insert("", "end", iid=self._iid(job), values=(str(job.path), job.status, ""))
        self._refresh_status()

    def _open_selected(self, _event) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        job = next((item for item in self.jobs if self._iid(item) == selection[0]), None)
        if job is None:
            return
        target = job.output or job.path.parent / f"{job.path.stem}.zh-CN.pdf"
        if target.is_file():
            os.startfile(target)  # noqa: S606 - Windows 下用默认程序打开译文
        else:
            self._log(f"还没生成译文: {target}")

    # ---------- 设置窗口 ----------

    def _open_settings(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("设置")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        entries: dict[str, tk.Entry] = {}
        for row, (key, title, secret) in enumerate(
            (("base_url", "API 地址", False), ("model", "模型名", False), ("api_key", "API 密钥", True))
        ):
            tk.Label(dialog, text=title, anchor="w").grid(row=row, column=0, sticky="w", padx=10, pady=6)
            entry = tk.Entry(dialog, width=50, show="*" if secret else "")
            entry.insert(0, str(self.config.get(key, "")))
            entry.grid(row=row, column=1, padx=10, pady=6)
            entries[key] = entry
        show_key = tk.BooleanVar(value=False)
        tk.Checkbutton(
            dialog,
            text="显示密钥",
            variable=show_key,
            command=lambda: entries["api_key"].configure(show="" if show_key.get() else "*"),
        ).grid(row=3, column=1, sticky="w", padx=10)

        mode = tk.StringVar(value=str(self.config.get("output_mode", "same_dir")))
        tk.Label(dialog, text="译文位置", anchor="w").grid(row=4, column=0, sticky="w", padx=10, pady=6)
        mode_row = tk.Frame(dialog)
        mode_row.grid(row=4, column=1, sticky="w", padx=10)
        tk.Radiobutton(mode_row, text="原文件旁边", variable=mode, value="same_dir").pack(side="left")
        tk.Radiobutton(mode_row, text="统一放到", variable=mode, value="custom").pack(side="left", padx=6)
        dir_entry = tk.Entry(mode_row, width=26)
        dir_entry.insert(0, str(self.config.get("output_dir", "")))
        dir_entry.pack(side="left")
        tk.Button(
            mode_row,
            text="浏览",
            command=lambda: dir_entry.delete(0, "end") or dir_entry.insert(0, filedialog.askdirectory() or ""),
        ).pack(side="left", padx=4)

        overwrite = tk.BooleanVar(value=bool(self.config.get("overwrite", False)))
        tk.Checkbutton(dialog, text="覆盖已存在的译文", variable=overwrite).grid(
            row=5, column=1, sticky="w", padx=10, pady=4
        )
        tk.Label(
            dialog,
            text=f"配置：{CONFIG_PATH}（密钥明文，只在本机）\n缓存：{CACHE_DIR}",
            justify="left",
            fg="#5f6368",
        ).grid(row=6, column=0, columnspan=2, sticky="w", padx=10, pady=(2, 6))

        def commit() -> None:
            self.config.update(
                {
                    "base_url": entries["base_url"].get().strip(),
                    "model": entries["model"].get().strip(),
                    "api_key": entries["api_key"].get().strip(),
                    "output_mode": mode.get(),
                    "output_dir": dir_entry.get().strip(),
                    "overwrite": overwrite.get(),
                }
            )
            try:
                save_config(self.config)
            except OSError as exc:
                messagebox.showerror(APP_NAME, f"配置保存失败: {exc}")
                return
            self._log(f"设置已保存到 {CONFIG_PATH}")
            dialog.destroy()

        buttons = tk.Frame(dialog)
        buttons.grid(row=7, column=0, columnspan=2, sticky="e", padx=10, pady=(0, 10))
        tk.Button(buttons, text="取消", width=10, command=dialog.destroy).pack(side="right", padx=6)
        tk.Button(buttons, text="保存", width=10, command=commit).pack(side="right")
        dialog.grab_set()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    enable_dpi_awareness()
    root: tk.Tk = TkinterDnD.Tk() if TkinterDnD is not None else tk.Tk()
    initial = [Path(item) for item in arguments if not item.startswith("-")]
    TranslatorApp(root, initial, load_config())
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
