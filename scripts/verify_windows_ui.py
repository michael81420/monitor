"""Drive real Windows Tk dialogs with synthetic input, without user interaction."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitorbot import desktop


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def drive(action, choices=("第一項", "第二項，等待同事回覆"), prompt="請確認下一步。"):
    create = tk.Tk
    errors = []
    completed = []

    def factory(*args, **kwargs):
        root = create(*args, **kwargs)

        def interact():
            try:
                action(root, list(descendants(root)))
                completed.append(True)
            except Exception as exc:
                errors.append(exc)
                root.destroy()

        def timeout():
            errors.append(AssertionError("dialog did not close after synthetic input"))
            root.destroy()

        mainloop = root.mainloop
        def drive_loop(*loop_args, **loop_kwargs):
            root.after(150, interact)
            root.after(15000, timeout)
            return mainloop(*loop_args, **loop_kwargs)
        root.mainloop = drive_loop
        return root

    with patch.object(tk, "Tk", side_effect=factory):
        result = desktop._windows_dialog("工作確認 1/2", prompt, choices)
    if errors:
        raise errors[0]
    assert completed, "dialog callback never ran"
    return result


def button(widgets, label):
    return next(widget for widget in widgets if isinstance(widget, ttk.Button)
                and widget.cget("text") == label)


def main():
    global tk, ttk
    if sys.platform != "win32":
        raise SystemExit("This check requires an interactive Windows desktop.")
    root = desktop._windows_root()
    root.update_idletasks()
    root.destroy()
    import tkinter as tk
    from tkinter import ttk
    passed = []

    def select_second(root, widgets):
        options = [widget for widget in widgets if isinstance(widget, tk.Radiobutton)]
        options[1].invoke()
        button(widgets, "確認").invoke()

    assert drive(select_second) == "第二項，等待同事回覆"
    passed.append("nonfirst Unicode choice confirmed through real widgets")
    assert drive(lambda root, widgets: button(widgets, "稍後").invoke()) == ""
    passed.append("defer returns an empty response")

    def escape(root, widgets):
        root.focus_force()
        root.event_generate("<Escape>")

    assert drive(escape) == ""
    passed.append("Escape cancels")

    def cancel_enter(root, widgets):
        cancel = button(widgets, "稍後")
        root.focus_force()
        cancel.focus_force()
        cancel.event_generate("<Return>")

    assert drive(cancel_enter) == ""
    passed.append("Enter on defer does not submit the selected choice")

    def save_text(root, widgets):
        entry = next(widget for widget in widgets if isinstance(widget, ttk.Entry))
        entry.insert(0, "明天下午確認，保留中文與 emoji ✅")
        root.focus_force()
        entry.focus_force()
        entry.event_generate("<Return>")

    assert drive(save_text, choices=None) == "明天下午確認，保留中文與 emoji ✅"
    passed.append("free text preserves Unicode and Enter saves")
    assert drive(lambda root, widgets: button(widgets, "取消").invoke(), choices=None) == ""
    passed.append("free-text cancel returns an empty response")

    def long_text(root, widgets):
        canvas = next(widget for widget in widgets if isinstance(widget, tk.Canvas))
        entry = next(widget for widget in widgets if isinstance(widget, ttk.Entry))
        top = canvas.canvasy(0)
        assert top <= entry.winfo_y(), "text input is above the visible area"
        assert entry.winfo_y() + entry.winfo_height() <= top + canvas.winfo_height(), "text input is below the visible area"
        entry.insert(0, "長問題仍可填寫")
        button(widgets, "儲存").invoke()

    assert drive(long_text, choices=None, prompt="需要完整保留的工作背景說明。" * 500) == "長問題仍可填寫"
    passed.append("long free-text prompt reveals the input before interaction")

    def refocus_text(root, widgets):
        canvas = next(widget for widget in widgets if isinstance(widget, tk.Canvas))
        entry = next(widget for widget in widgets if isinstance(widget, ttk.Entry))
        canvas.yview_moveto(0)
        root.focus_force()
        entry.focus_force()
        root.update()
        assert canvas.canvasy(0) <= entry.winfo_y() < canvas.canvasy(0) + canvas.winfo_height()
        entry.insert(0, "長說明後的回應")
        button(widgets, "儲存").invoke()

    assert drive(refocus_text, None, "很長的工作背景說明。" * 120) == "長說明後的回應"
    passed.append("free-text input scrolls into view after a long prompt")

    def overflow(root, widgets):
        canvas = next(widget for widget in widgets if isinstance(widget, tk.Canvas))
        assert root.winfo_height() < root.winfo_screenheight(), "dialog exceeds display height"
        assert canvas.yview()[1] < 1.0, "long content is not scrollable"
        canvas.yview_moveto(1)
        root.update_idletasks()
        assert canvas.yview()[1] > 0.99, "cannot scroll to final choice"
        options = [widget for widget in widgets if isinstance(widget, tk.Radiobutton)]
        canvas.yview_moveto(0)
        root.focus_force()
        options[-1].focus_force()
        root.update()
        assert canvas.canvasy(0) <= options[-1].winfo_y() < canvas.canvasy(0) + canvas.winfo_height()
        options[-1].invoke()
        confirm = button(widgets, "確認")
        assert confirm.winfo_rooty() + confirm.winfo_height() <= root.winfo_rooty() + root.winfo_height()
        confirm.invoke()

    choices = ["{}. {}".format(index, "很長的選項文字，應保持完整顯示並可捲動。" * 4) for index in range(20)]
    assert drive(overflow, choices, "很長的工作背景說明。" * 60) == choices[-1]
    passed.append("long prompt and choices scroll while confirmation stays visible")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path, help="save a synthetic dialog preview as PNG")
    args = parser.parse_args()
    if args.screenshot:
        def capture(root, widgets):
            root.lift()
            root.focus_force()
            root.update()
            path = args.screenshot.resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            script = """$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap({width}, {height})
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {{
    $graphics.CopyFromScreen({x}, {y}, 0, 0, $bitmap.Size)
    $bitmap.Save($env:MONITOR_UI_SCREENSHOT, [System.Drawing.Imaging.ImageFormat]::Png)
}} finally {{ $graphics.Dispose(); $bitmap.Dispose() }}
""".format(width=root.winfo_width(), height=root.winfo_height(),
           x=root.winfo_rootx(), y=root.winfo_rooty())
            result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                                    env=dict(os.environ, MONITOR_UI_SCREENSHOT=str(path)),
                                    capture_output=True, timeout=15)
            assert result.returncode == 0 and path.is_file(), "dialog screenshot failed: " + repr(result.stderr)
            button(widgets, "稍後").invoke()
        drive(capture, choices=["今天完成初稿並交付審閱", "等待同事補充資料後，再繼續整理", "其他，自行填寫"],
              prompt="這項工作目前進行到哪裡？\n建議：今天完成初稿並交付審閱")
        passed.append("synthetic dialog screenshot saved")
    print(json.dumps({"platform": sys.platform, "passed": passed}, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
