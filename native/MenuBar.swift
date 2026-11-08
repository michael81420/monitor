import Cocoa

struct WorkTask: Decodable {
    let id: String
    let title: String
    let status: String
    let project: String
    let next_action: String
    let waiting_for: String
    let due_at: String?
    let waiting_until: String?
}

struct CommandResult {
    let data: Data
    let succeeded: Bool
}

func command(_ arguments: [String], python: String, project: String, home: String) -> CommandResult {
    let process = Process()
    let output = Pipe()
    process.executableURL = URL(fileURLWithPath: python)
    process.currentDirectoryURL = URL(fileURLWithPath: project)
    process.arguments = ["-m", "monitorbot", "--home", home] + arguments
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    do {
        try process.run()
        DispatchQueue.global().asyncAfter(deadline: .now() + 15) {
            if process.isRunning { process.terminate() }
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return CommandResult(data: data, succeeded: process.terminationStatus == 0)
    } catch {
        return CommandResult(data: Data(), succeeded: false)
    }
}

final class MenuController: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var item: NSStatusItem!
    private let menu = NSMenu()
    private var tasks: [WorkTask] = []
    private var loading = false
    private var error = false
    private var loaded = false
    private var reportedReady = false
    private var timer: Timer?
    private let python: String
    private let project: String
    private let home: String

    init(python: String, project: String, home: String) {
        self.python = python
        self.project = project
        self.home = home
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            let image = NSImage(systemSymbolName: "checklist", accessibilityDescription: "Monitor 待辦清單")
            image?.isTemplate = true
            button.image = image
            button.toolTip = "Monitor：查看目前待辦"
            button.setAccessibilityLabel("Monitor 待辦清單")
        }
        menu.delegate = self
        item.menu = menu
        rebuild()
        refresh()
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func menuWillOpen(_ menu: NSMenu) { refresh() }

    @discardableResult private func line(_ title: String, action: Selector? = nil) -> NSMenuItem {
        let row = NSMenuItem(title: title, action: action, keyEquivalent: "")
        row.target = self
        menu.addItem(row)
        return row
    }

    private func heading(_ title: String) {
        let row = line(title)
        row.isEnabled = false
    }

    private func section(_ title: String, records: [WorkTask]) {
        heading(title + "（\(records.count)）")
        if records.isEmpty {
            heading(title == "輪到你處理" ? "目前沒有待辦" : "目前沒有等待事項")
        }
        for task in records {
            let abbreviated = task.title.count > 52 ? String(task.title.prefix(52)) + "…" : task.title
            let overdue = date(task.due_at).map { $0 < Date() } ?? false
            let waitingOverdue = task.status == "waiting" && (date(task.waiting_until).map { $0 < Date() } ?? false)
            let suffix = overdue ? "（交付已逾期）" : (waitingOverdue ? "（前置已超時）" : "")
            let row = line(abbreviated + suffix, action: #selector(showTask(_:)))
            row.representedObject = task.id
            row.toolTip = task.title + (task.next_action.isEmpty ? "" : "\n下一步：" + task.next_action)
            if overdue {
                row.image = NSImage(systemSymbolName: "clock.badge.exclamationmark", accessibilityDescription: "交付已逾期")
            } else if task.status == "waiting" {
                row.image = NSImage(systemSymbolName: "hourglass", accessibilityDescription: "等待他人")
            }
        }
    }

    private func rebuild() {
        menu.removeAllItems()
        if !loaded {
            heading(error ? "無法讀取待辦，請重新整理" : "正在讀取待辦…")
        } else {
            if error { heading("更新失敗，顯示上次清單") }
            section("輪到你處理", records: tasks.filter { $0.status != "waiting" })
            menu.addItem(.separator())
            section("等待他人", records: tasks.filter { $0.status == "waiting" })
        }
        menu.addItem(.separator())
        line("新增待辦…", action: #selector(addTask))
        line("每日確認與完整清單…", action: #selector(openDaily))
        line(loading ? "正在更新…" : "重新整理清單", action: #selector(reload)).isEnabled = !loading
        menu.addItem(.separator())
        line("關閉選單列圖示", action: #selector(quit))
        item?.button?.title = loaded && !tasks.isEmpty ? " \(tasks.count)" : ""
        item?.button?.toolTip = loaded ? "Monitor：\(tasks.count) 項待辦" : "Monitor：查看目前待辦"
    }

    private func refresh() {
        guard !loading else { return }
        loading = true
        let python = self.python, project = self.project, home = self.home
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = command(["task", "list"], python: python, project: project, home: home)
            let records = result.succeeded ? try? JSONDecoder().decode([WorkTask].self, from: result.data) : nil
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.loading = false
                self.error = records == nil
                if let records = records {
                    self.tasks = records
                    self.loaded = true
                }
                self.rebuild()
                if !self.reportedReady {
                    let state: [String: Any] = ["event": "menu_bar_ready", "tasks": self.tasks.count]
                    if let data = try? JSONSerialization.data(withJSONObject: state) {
                        FileHandle.standardError.write(data + Data([10]))
                    }
                    self.reportedReady = true
                }
            }
        }
    }

    private func date(_ value: String?) -> Date? {
        guard let value = value else { return nil }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let parsed = parser.date(from: value) { return parsed }
        parser.formatOptions = [.withInternetDateTime]
        return parser.date(from: value)
    }

    private func timeDescription(_ value: String?) -> String? {
        guard let parsed = date(value) else { return nil }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_TW")
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        return formatter.string(from: parsed)
    }

    @objc private func showTask(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, let task = tasks.first(where: { $0.id == id }) else { return }
        let alert = NSAlert()
        alert.messageText = task.title
        var details = [task.status == "waiting" ? "目前等待他人或前置條件" : "目前輪到你處理"]
        if !task.project.isEmpty { details.append("專案：" + task.project) }
        details.append("下一步：" + (task.next_action.isEmpty ? "尚未確定" : task.next_action))
        if !task.waiting_for.isEmpty { details.append("等待：" + task.waiting_for) }
        if let due = timeDescription(task.due_at) { details.append("交付期限：" + due) }
        if let waiting = timeDescription(task.waiting_until) { details.append("前置工作預估完成：" + waiting) }
        alert.informativeText = details.joined(separator: "\n\n")
        alert.addButton(withTitle: "關閉")
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }

    @objc private func addTask() {
        let alert = NSAlert()
        alert.messageText = "新增待辦"
        alert.informativeText = "輸入一句話，先保存，後續由追蹤 agent 整理。"
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 340, height: 26))
        field.placeholderString = "例如：有空研究 HDR"
        alert.accessoryView = field
        alert.addButton(withTitle: "儲存")
        alert.addButton(withTitle: "取消")
        alert.window.initialFirstResponder = field
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let text = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let python = self.python, project = self.project, home = self.home
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = command(["add", text], python: python, project: project, home: home)
            DispatchQueue.main.async {
                if result.succeeded { self?.refresh() }
                else {
                    let failure = NSAlert()
                    failure.messageText = "尚未確認儲存結果"
                    failure.informativeText = "這次沒有確認儲存成功，請稍後再試。"
                    failure.runModal()
                }
            }
        }
    }

    @objc private func reload() { refresh() }

    @objc private func openDaily() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.currentDirectoryURL = URL(fileURLWithPath: project)
        process.arguments = ["-m", "monitorbot", "--home", home, "review"]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try? process.run()
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

let arguments = CommandLine.arguments
func option(_ name: String) -> String? {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else { return nil }
    return arguments[index + 1]
}
guard let python = option("--python"), let project = option("--project"), let home = option("--home") else {
    fputs("Expected --python, --project and --home\n", stderr)
    exit(2)
}
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
let controller = MenuController(python: python, project: project, home: home)
application.delegate = controller
application.run()
