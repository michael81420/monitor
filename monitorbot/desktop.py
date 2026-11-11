"""Desktop dialogs keep the first daily interface small."""

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .agents import AgentRunner
from .service import refresh_questions


def _osascript(script, args):
    result = subprocess.run(["osascript", "-e", script, "--"] + list(args),
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("native dialog cancelled or unavailable")
    return result.stdout.rstrip("\n")


def _windows_root():
    added = []
    try:
        for name, prefix, script in (("TCL_LIBRARY", "tcl", "init.tcl"),
                                     ("TK_LIBRARY", "tk", "tk.tcl")):
            if name in os.environ:
                continue
            for library in sorted((Path(sys.base_prefix) / "tcl").glob(prefix + "*"), reverse=True):
                if not (library / script).is_file():
                    continue
                # Tcl caches startup paths at import and can misread non-ASCII Windows paths.
                try:
                    path = os.path.relpath(library)
                except ValueError:
                    path = str(library)
                os.environ[name] = path
                added.append(name)
                break
        try:
            import tkinter as tk
        except ImportError as exc:
            raise RuntimeError("Windows dialogs require Python with tkinter") from exc
        try:
            return tk.Tk()
        except tk.TclError as exc:
            message = ("Windows dialogs require a working Python Tcl/Tk installation"
                       if "init.tcl" in str(exc) else "Windows dialogs require an interactive desktop")
            raise RuntimeError(message) from exc
    finally:
        for name in added:
            os.environ.pop(name, None)


def _windows_dialog(title, prompt, choices=None):
    root = _windows_root()
    import tkinter as tk
    from tkinter import ttk
    answer = []
    try:
        root.withdraw()
        root.title(title)
        root.resizable(True, True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        style = ttk.Style(root)
        style.configure("Monitor.TFrame", background="#f5f7fb")
        style.configure("Monitor.TLabel", background="#f5f7fb", foreground="#18263b",
                        font=("Microsoft JhengHei UI", 10))
        style.configure("Monitor.Title.TLabel", background="#f5f7fb", foreground="#18263b",
                        font=("Microsoft JhengHei UI", 16, "bold"))
        style.configure("Monitor.Hint.TLabel", background="#f5f7fb", foreground="#536177",
                        font=("Microsoft JhengHei UI", 9))
        style.configure("Monitor.TButton", font=("Microsoft JhengHei UI", 10), padding=(12, 6))
        root.configure(background="#f5f7fb")
        header = ttk.Frame(root, padding=(24, 20, 24, 12), style="Monitor.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="工作助理", style="Monitor.Hint.TLabel").pack(anchor="w")
        heading = ttk.Label(header, text=title, style="Monitor.Title.TLabel", wraplength=560)
        heading.pack(anchor="w", pady=(4, 0))
        body = ttk.Frame(root, padding=(24, 0, 24, 0), style="Monitor.TFrame")
        body.grid(row=1, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        canvas = tk.Canvas(body, background="#f5f7fb", highlightthickness=0, height=220)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        canvas.configure(yscrollcommand=scrollbar.set)
        content = ttk.Frame(canvas, style="Monitor.TFrame")
        content.columnconfigure(0, weight=1)
        window = canvas.create_window(0, 0, window=content, anchor="nw")
        prompt_label = ttk.Label(content, text=prompt, wraplength=540, justify="left",
                                 style="Monitor.TLabel")
        prompt_label.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        selected = tk.StringVar(value=choices[0] if choices else "")
        wrapped = [prompt_label]
        focus = None
        if choices is None:
            focus = ttk.Entry(content, textvariable=selected, font=("Microsoft JhengHei UI", 11))
            focus.grid(row=1, column=0, sticky="ew", pady=(0, 8), ipady=5)
        else:
            for index, choice in enumerate(choices):
                option = tk.Radiobutton(content, text=choice, value=choice, variable=selected,
                                        wraplength=520, justify="left", anchor="w",
                                        background="#ffffff", activebackground="#e8effb",
                                        foreground="#18263b", selectcolor="#ffffff",
                                        font=("Microsoft JhengHei UI", 10), padx=12, pady=10,
                                        relief="flat", borderwidth=0, highlightthickness=1,
                                        highlightbackground="#dce2ec", highlightcolor="#315fbc")
                option.grid(row=index + 1, column=0, sticky="ew", pady=(0, 8))
                wrapped.append(option)
                if focus is None:
                    focus = option
        def reveal(widget):
            root.update_idletasks()
            top = widget.winfo_y()
            bottom = top + widget.winfo_height()
            visible_top = canvas.canvasy(0)
            visible_bottom = visible_top + canvas.winfo_height()
            if top < visible_top:
                canvas.yview_moveto(top / max(1, content.winfo_height()))
            elif bottom > visible_bottom:
                canvas.yview_moveto((bottom - canvas.winfo_height()) / max(1, content.winfo_height()))
        for widget in ([focus] if choices is None else wrapped[1:]):
            widget.bind("<FocusIn>", lambda event: reveal(event.widget))
        def resize(event):
            canvas.itemconfigure(window, width=event.width)
            heading.configure(wraplength=max(80, event.width))
            for widget in wrapped:
                widget.configure(wraplength=max(80, event.width - 44))
        canvas.bind("<Configure>", resize)
        content.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        root.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        def confirm():
            answer.append(selected.get())
            root.destroy()
        footer = ttk.Frame(root, padding=(24, 16, 24, 20), style="Monitor.TFrame")
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="Enter 確認  ·  Esc 稍後", style="Monitor.Hint.TLabel").grid(
            row=0, column=0, sticky="w")
        cancel = ttk.Button(footer, text="取消" if choices is None else "稍後",
                            style="Monitor.TButton", command=root.destroy)
        cancel.grid(row=0, column=1, padx=(12, 8))
        confirm_button = ttk.Button(footer, text="儲存" if choices is None else "確認",
                                    style="Monitor.TButton", command=confirm)
        confirm_button.grid(row=0, column=2)
        root.bind("<Return>", lambda event: root.destroy() if event.widget == cancel else confirm())
        root.bind("<Escape>", lambda event: root.destroy())
        root.update_idletasks()
        width = min(680, root.winfo_screenwidth() - 64)
        height = min(max(360, content.winfo_reqheight() + 190), root.winfo_screenheight() - 100)
        root.geometry("{}x{}+{}+{}".format(width, height,
                      (root.winfo_screenwidth() - width) // 2,
                      (root.winfo_screenheight() - height) // 2))
        root.minsize(min(440, width), min(320, height))
        root.deiconify()
        if focus is not None:
            reveal(focus)
            focus.focus_set()
        root.mainloop()
        return answer[0] if answer else ""
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def choose(title, prompt, choices):
    if sys.platform == "win32":
        return _windows_dialog(title, prompt, choices)
    script = '''on run argv
        set options to items 3 thru -1 of argv
        set picked to choose from list options with title (item 1 of argv) with prompt (item 2 of argv) OK button name "確認" cancel button name "稍後"
        if picked is false then return ""
        return item 1 of picked
    end run'''
    return _osascript(script, [title, prompt] + choices)


def _text(title, prompt):
    if sys.platform == "win32":
        return _windows_dialog(title, prompt)
    script = '''on run argv
        set picked to display dialog (item 2 of argv) with title (item 1 of argv) default answer "" buttons {"取消", "儲存"} default button "儲存"
        return text returned of picked
    end run'''
    return _osascript(script, [title, prompt])


def todo_markdown(store, config):
    now = datetime.now(ZoneInfo(config["timezone"]))
    lines = ["# 每日待處理清單", "", "整理時間：" + now.isoformat(), ""]
    tasks = store.list_tasks()
    for title, waiting in (("輪到你處理", False), ("等待他人或前置條件", True)):
        lines += ["## " + title, ""]
        group = [task for task in tasks if (task["status"] == "waiting") == waiting]
        if not group:
            lines += ["目前沒有項目。", ""]
        for task in group:
            lines += ["- " + task["title"] + " (`" + task["id"] + "`)",
                      "  - 下一步：" + (task.get("next_action") or "尚未確定"),
                      "  - 負責人：" + ((task.get("waiting_for") if waiting else task.get("ownership")) or "未知")]
            if task.get("due_at"):
                expired = datetime.fromisoformat(task["due_at"]) < now
                lines.append("  - 交付期限：" + task["due_at"] + ("，已逾期" if expired else ""))
            if waiting and task.get("waiting_until"):
                expired = datetime.fromisoformat(task["waiting_until"]) < now
                lines.append("  - 前置工作預估完成：" + task["waiting_until"] +
                             ("，前置工作已超過預估" if expired else ""))
            lines += ["  - 最近實質進展：" + task["last_activity_at"], ""]
    lines += ["## 資料涵蓋", ""]
    for source, health in store.health().items():
        lines.append("- {}：{}，最後檢查 {}".format(source, health["status"], health.get("checked_at", "未知")))
    lines += ["", "尚未分析的活動：{} 筆。".format(store.count_events(unprocessed=True, excluded=False))]
    return "\n".join(lines) + "\n"


def daily_review(store, config):
    root = Path(config["data_dir"])
    path = root / "daily-todo.md"
    path.write_text(todo_markdown(store, config), encoding="utf-8")
    path.chmod(0o600)
    if sys.platform not in ("darwin", "win32"):
        return {"path": str(path), "popup": False, "reason": "desktop dialogs require macOS or Windows"}
    runner = AgentRunner(config, root)
    questions = store.list_questions()
    if questions and not refresh_questions(store, config, runner):
        store.log("daily_questions_deferred", {"reason": "latest review did not complete"})
    reviewed = set(store.get_meta("daily_reviewed_questions", []))
    questions = [question for question in store.list_questions() if question["id"] in reviewed]
    today = datetime.now(ZoneInfo(config["timezone"])).date().isoformat()
    backlog = any(datetime.fromisoformat(question["created_at"]).astimezone(
        ZoneInfo(config["timezone"])).date().isoformat() < today for question in questions)
    cap = config["schedule"]["backlog_limit" if backlog else "question_limit"]
    answered = 0
    try:
        for question in questions[:cap]:
            latest = next((item for item in store.list_questions() if item["id"] == question["id"]), None)
            if not latest:
                continue
            labels = ["{}. {}".format(index + 1, choice) for index, choice in enumerate(latest["choices"])]
            suggestion = latest.get("suggested_index", 0)
            prompt = latest["text"]
            if 0 <= suggestion < len(latest["choices"]):
                prompt += "\n建議：" + latest["choices"][suggestion]
            picked = choose("工作確認 {}/{}".format(answered + 1, min(cap, len(questions))),
                            prompt, labels + ["其他，自行填寫"])
            if not picked:
                break
            answer = (_text("其他回應", latest["text"]) if picked == "其他，自行填寫"
                      else latest["choices"][labels.index(picked)])
            if not answer.strip():
                continue
            apply = False
            if latest.get("rule_key"):
                apply = choose("這項選擇的適用範圍", "是否將答案套用到往後同類情況？",
                               ["僅這次", "以後同類都如此處理"]) == "以後同類都如此處理"
            store.answer_question(latest["id"], answer, apply_to_similar=apply, scope=latest.get("scope"))
            answered += 1
        summary = "\n".join(task["title"] for task in store.list_tasks()[:10]) or "目前沒有待處理事項。"
        action = choose("每日待處理清單", summary, ["開啟完整清單", "關閉"])
        if action == "開啟完整清單":
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                subprocess.Popen(["open", "-e", str(path)])
    except RuntimeError:
        pass
    return {"path": str(path), "answered": answered}
