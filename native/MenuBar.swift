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

struct SourceHealth: Decodable {
    let status: String
    let detail: String?
    let checked_at: String?
}

struct MenuSnapshot: Decodable {
    let tasks: [WorkTask]
    let question_count: Int
    let pending_events: Int
    let source_health: [String: SourceHealth]
    let generated_at: String
    let background: BackgroundStatus
    let last_poll: String?
}

struct BackgroundStatus: Decodable {
    let supported: Bool
    let loaded: Bool
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
    private var snapshot: MenuSnapshot?
    private var tasks: [WorkTask] { snapshot?.tasks ?? [] }
    private let snapshotFile: String?
    private var loading = false
    private var error = false
    private var loaded = false
    private var reportedReady = false
    private var timer: Timer?
    private let python: String
    private let project: String
    private let home: String

    init(python: String, project: String, home: String, snapshotFile: String? = nil) {
        self.python = python
        self.project = project
        self.home = home
        self.snapshotFile = snapshotFile
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
        if snapshotFile != nil { return }
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in self?.refresh() }
    }

    func menuWillOpen(_ menu: NSMenu) { refresh() }

    @discardableResult private func line(_ title: String, action: Selector? = nil, in targetMenu: NSMenu? = nil) -> NSMenuItem {
        let row = NSMenuItem(title: title, action: action, keyEquivalent: "")
        row.target = self
        (targetMenu ?? menu).addItem(row)
        return row
    }

    private func heading(_ title: String, in targetMenu: NSMenu? = nil) {
        let row = line(title, in: targetMenu)
        row.isEnabled = false
    }

    private func taskRows(_ records: [WorkTask], in targetMenu: NSMenu) {
        for task in records {
            let abbreviated = task.title.count > 42 ? String(task.title.prefix(42)) + "…" : task.title
            let overdue = date(task.due_at).map { $0 < Date() } ?? false
            let waitingOverdue = task.status == "waiting" && (date(task.waiting_until).map { $0 < Date() } ?? false)
            let suffix = overdue ? "  ·  已逾期" : (waitingOverdue ? "  ·  前置超時" : "")
            let row = line(abbreviated + suffix, action: #selector(showTask(_:)), in: targetMenu)
            row.representedObject = task.id
            row.toolTip = task.title + (task.next_action.isEmpty ? "" : "\n下一步：" + task.next_action)
            if overdue || waitingOverdue {
                row.image = NSImage(systemSymbolName: "clock.badge.exclamationmark", accessibilityDescription: overdue ? "交付已逾期" : "前置工作已超時")
            } else if task.status == "waiting" {
                row.image = NSImage(systemSymbolName: "hourglass", accessibilityDescription: "等待他人")
            }
        }
    }

    private func sourceDetails(_ snapshot: MenuSnapshot) {
        let health = snapshot.source_health
        let issues = health.values.filter { !["ok", "excluded"].contains($0.status) }.count
        let title = health.isEmpty ? "來源尚未檢查" : (issues == 0 ? "已檢查來源正常" : "\(issues) 個來源需處理")
        let row = line(title)
        let details = NSMenu()
        row.submenu = details
        if issues > 0 {
            row.image = NSImage(systemSymbolName: "exclamationmark.triangle", accessibilityDescription: "來源需要處理")
        }
        if health.isEmpty { heading("收集後才會顯示來源狀態", in: details) }
        let labels = ["ok": "正常", "excluded": "已停用", "partial": "資料不完整", "error": "讀取失敗", "unconfigured": "尚未連接"]
        for name in health.keys.sorted() {
            guard let state = health[name] else { continue }
            let source = line(name + "  ·  " + (labels[state.status] ?? state.status), in: details)
            let info = NSMenu()
            source.submenu = info
            heading(state.detail?.isEmpty == false ? state.detail! : "沒有其他說明", in: info)
            heading("最後檢查：" + (timeDescription(state.checked_at) ?? "未知"), in: info)
        }
        details.addItem(.separator())
        heading("尚未分析的活動：\(snapshot.pending_events) 筆", in: details)
        heading("清單讀取：" + (timeDescription(snapshot.generated_at) ?? "未知"), in: details)
    }

    private func rebuild() {
        menu.removeAllItems()
        if !loaded {
            heading(error ? "無法讀取清單，請重新整理" : "正在讀取清單…")
        } else if let snapshot = snapshot {
            let active = tasks.filter { $0.status != "waiting" }
            let waiting = tasks.filter { $0.status == "waiting" }
            let overdue = tasks.filter { date($0.due_at).map { $0 < Date() } ?? false }.count
            let summary = line("輪到你 \(active.count) 項  ·  等待 \(waiting.count) 項")
            summary.isEnabled = false
            summary.attributedTitle = NSAttributedString(string: summary.title, attributes: [
                .font: NSFont.systemFont(ofSize: NSFont.systemFontSize, weight: .semibold),
                .foregroundColor: NSColor.labelColor,
            ])
            if error { heading("更新失敗，顯示上次清單。請重新整理") }
            if !snapshot.background.supported { heading("此平台未提供背景排程") }
            else { heading(snapshot.background.loaded ? "背景排程已啟用" : "背景收集未啟用") }
            heading(timeDescription(snapshot.last_poll).map { "最後收集檢查 " + $0 } ?? "尚未收集資料")
            if overdue > 0 { heading("\(overdue) 項交付已逾期") }
            if snapshot.question_count > 0 {
                line("待你確認 \(snapshot.question_count) 題…", action: #selector(openDaily)).isEnabled = snapshotFile == nil
            }
            sourceDetails(snapshot)
            menu.addItem(.separator())
            heading("輪到你處理")
            if active.isEmpty { heading("目前沒有待辦") }
            taskRows(Array(active.prefix(5)), in: menu)
            if active.count > 5 {
                let more = line("其餘 \(active.count - 5) 項待辦")
                let remaining = NSMenu()
                more.submenu = remaining
                taskRows(Array(active.dropFirst(5)), in: remaining)
            }
            menu.addItem(.separator())
            let waitingRow = line("等待他人（\(waiting.count)）")
            let waitingMenu = NSMenu()
            waitingRow.submenu = waitingMenu
            if waiting.isEmpty { heading("目前沒有等待事項", in: waitingMenu) }
            taskRows(waiting, in: waitingMenu)
        }
        menu.addItem(.separator())
        line("新增待辦…", action: #selector(addTask)).isEnabled = snapshotFile == nil
        line("每日確認與完整清單…", action: #selector(openDaily)).isEnabled = snapshotFile == nil
        line(loading ? "正在更新…" : "重新整理清單", action: #selector(reload)).isEnabled = !loading
        menu.addItem(.separator())
        line("關閉圖示，背景收集繼續", action: #selector(quit))
        let activeCount = tasks.filter { $0.status != "waiting" }.count
        item?.button?.title = loaded ? " \(activeCount)" : ""
        let description = loaded ? "Monitor：輪到你 \(activeCount) 項，等待 \(tasks.count - activeCount) 項" : "Monitor：查看目前待辦"
        item?.button?.toolTip = description + (error ? "，清單更新失敗" : "")
        item?.button?.setAccessibilityLabel(description)
    }

    func inspectSnapshot(_ path: String) throws {
        snapshot = try JSONDecoder().decode(MenuSnapshot.self, from: Data(contentsOf: URL(fileURLWithPath: path)))
        loaded = true
        rebuild()
        let data = try JSONSerialization.data(withJSONObject: menuContents(menu), options: [.prettyPrinted, .sortedKeys])
        FileHandle.standardOutput.write(data + Data([10]))
    }

    private func menuContents(_ source: NSMenu) -> [[String: Any]] {
        source.items.map { row in
            var entry: [String: Any] = ["title": row.title, "enabled": row.isEnabled, "separator": row.isSeparatorItem]
            if let submenu = row.submenu { entry["items"] = menuContents(submenu) }
            return entry
        }
    }

    private func refresh() {
        guard !loading else { return }
        loading = true
        rebuild()
        let python = self.python, project = self.project, home = self.home
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result: CommandResult
            if let path = self?.snapshotFile {
                let data = try? Data(contentsOf: URL(fileURLWithPath: path))
                result = CommandResult(data: data ?? Data(), succeeded: data != nil)
            } else {
                result = command(["menubar", "snapshot"], python: python, project: project, home: home)
            }
            let records = result.succeeded ? try? JSONDecoder().decode(MenuSnapshot.self, from: result.data) : nil
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.loading = false
                self.error = records == nil
                if let records = records {
                    self.snapshot = records
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
let snapshotFile = option("--snapshot")
if snapshotFile == nil && (option("--python") == nil || option("--project") == nil || option("--home") == nil) {
    fputs("Expected --python, --project and --home, or --snapshot FILE\n", stderr)
    exit(2)
}
let controller = MenuController(python: option("--python") ?? "", project: option("--project") ?? "", home: option("--home") ?? "", snapshotFile: snapshotFile)
if arguments.contains("--dump-menu"), let path = snapshotFile {
    do { try controller.inspectSnapshot(path) }
    catch { fputs("Could not read menu snapshot\n", stderr); exit(1) }
    exit(0)
}
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
application.delegate = controller
application.run()
