import AppKit
import CryptoKit
import Darwin
import Foundation

private let requestSchema = "daemon.companion-request.v1"

private enum CompanionError: LocalizedError {
    case message(String)

    var errorDescription: String? {
        switch self {
        case .message(let value): return value
        }
    }
}

private struct PackageFile: Decodable, Equatable {
    let sha256: String
    let size: Int
    let executable: Bool
}

private struct PackageManifest: Decodable {
    let schema: String
    let files: [String: PackageFile]
}

private struct BackendRuntime {
    let python: URL
    let sourceRoot: URL
    let preparationEnvironment: [String: String]
}

private let runtimeLock = NSLock()
private var cachedRuntime: BackendRuntime?

private func automaticHost(_ usable: [String]) -> String? {
    usable.count == 1 ? usable[0] : (usable.contains("hermes") ? "hermes" : usable.first)
}

private func backendEnvironment(_ runtime: BackendRuntime) -> [String: String] {
    var environment = ProcessInfo.processInfo.environment
    environment["PYTHONPATH"] = runtime.sourceRoot.path
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for (key, value) in runtime.preparationEnvironment {
        environment[key] = value
    }
    return environment
}

private func runProcess(
    _ executable: URL,
    arguments: [String],
    environment: [String: String]? = nil,
    timeout: TimeInterval
) throws -> (Int32, String, String) {
    let process = Process()
    let output = Pipe()
    let errors = Pipe()
    process.executableURL = executable
    process.arguments = arguments
    process.environment = environment
    process.standardOutput = output
    process.standardError = errors
    try process.run()
    let completed = DispatchSemaphore(value: 0)
    process.terminationHandler = { _ in completed.signal() }
    if completed.wait(timeout: .now() + timeout) == .timedOut {
        process.terminate()
        throw CompanionError.message("A private runtime operation timed out.")
    }
    return (
        process.terminationStatus,
        String(decoding: output.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self),
        String(decoding: errors.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
    )
}

private func safePackagePath(_ value: String) -> Bool {
    if value.isEmpty || value.hasPrefix("/") || value.contains("\\") || value.contains("\0") {
        return false
    }
    let parts = value.split(separator: "/", omittingEmptySubsequences: false)
    return !parts.contains(where: { $0.isEmpty || $0 == "." || $0 == ".." })
}

private func sha256(_ url: URL) throws -> String {
    let data = try Data(contentsOf: url, options: [.mappedIfSafe])
    return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

private func verifyPackage(at root: URL) throws -> PackageManifest {
    let manager = FileManager.default
    let rootValues = try root.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
    guard rootValues.isDirectory == true, rootValues.isSymbolicLink != true else {
        throw CompanionError.message("The companion package root is unsafe.")
    }
    let manifestURL = root.appendingPathComponent("companion-manifest.json", isDirectory: false)
    let manifestValues = try manifestURL.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
    guard manifestValues.isRegularFile == true, manifestValues.isSymbolicLink != true else {
        throw CompanionError.message("The companion package manifest is missing.")
    }
    let data = try Data(contentsOf: manifestURL)
    guard
        let raw = try JSONSerialization.jsonObject(with: data) as? [String: Any],
        Set(raw.keys) == Set(["schema", "files"]),
        raw["schema"] as? String == "daemon.companion-package.v1",
        let rawFiles = raw["files"] as? [String: Any],
        !rawFiles.isEmpty,
        rawFiles.count <= 5_000
    else {
        throw CompanionError.message("The companion package manifest is invalid.")
    }
    for (path, descriptor) in rawFiles {
        guard
            safePackagePath(path),
            let fields = descriptor as? [String: Any],
            Set(fields.keys) == Set(["sha256", "size", "executable"]),
            let digest = fields["sha256"] as? String,
            digest.count == 64,
            digest.allSatisfy({ $0.isHexDigit && !$0.isUppercase }),
            let size = fields["size"] as? Int,
            size >= 0,
            size <= 128 * 1024 * 1024,
            fields["executable"] is Bool
        else {
            throw CompanionError.message("The companion package manifest contains an invalid entry.")
        }
    }
    let manifest = try JSONDecoder().decode(PackageManifest.self, from: data)
    guard manifest.schema == "daemon.companion-package.v1" else {
        throw CompanionError.message("The companion package manifest is unsupported.")
    }

    let keys: [URLResourceKey] = [.isRegularFileKey, .isDirectoryKey, .isSymbolicLinkKey, .fileSizeKey]
    guard let enumerator = manager.enumerator(
        at: root,
        includingPropertiesForKeys: keys,
        options: [],
        errorHandler: { _, _ in false }
    ) else {
        throw CompanionError.message("The companion package could not be inspected.")
    }
    var actual: [String: PackageFile] = [:]
    for case let url as URL in enumerator {
        let values = try url.resourceValues(forKeys: Set(keys))
        guard values.isSymbolicLink != true else {
            throw CompanionError.message("The companion package contains a symbolic link.")
        }
        if values.isDirectory == true { continue }
        guard values.isRegularFile == true else {
            throw CompanionError.message("The companion package contains an unsupported entry.")
        }
        let prefix = root.path.hasSuffix("/") ? root.path : root.path + "/"
        guard url.path.hasPrefix(prefix) else {
            throw CompanionError.message("The companion package contains an unsafe path.")
        }
        let relative = String(url.path.dropFirst(prefix.count))
        if relative == "companion-manifest.json" { continue }
        guard safePackagePath(relative), actual[relative] == nil else {
            throw CompanionError.message("The companion package contains a duplicate or unsafe path.")
        }
        let attributes = try manager.attributesOfItem(atPath: url.path)
        let permissions = (attributes[.posixPermissions] as? NSNumber)?.intValue ?? 0
        actual[relative] = PackageFile(
            sha256: try sha256(url),
            size: values.fileSize ?? -1,
            executable: permissions & 0o111 != 0
        )
    }
    guard actual == manifest.files else {
        throw CompanionError.message("Companion package integrity verification failed.")
    }
    return manifest
}

private func privateStateRoot() throws -> URL {
    let environment = ProcessInfo.processInfo.environment
    if let override = environment["DAEMON_HARNESS_COMPANION_STATE_ROOT"], !override.isEmpty {
        return URL(fileURLWithPath: override, isDirectory: true)
    }
    let base = try FileManager.default.url(
        for: .applicationSupportDirectory,
        in: .userDomainMask,
        appropriateFor: nil,
        create: true
    )
    return base.appendingPathComponent("DaemonHarnessCompanion/bootstrap", isDirectory: true)
}

private func installHome() throws -> URL {
    let base = try FileManager.default.url(
        for: .applicationSupportDirectory,
        in: .userDomainMask,
        appropriateFor: nil,
        create: true
    )
    return base.appendingPathComponent("DaemonHarness", isDirectory: true)
}

private func validPython(in installRoot: URL) throws -> URL? {
    let manager = FileManager.default
    guard manager.fileExists(atPath: installRoot.path) else { return nil }
    guard let enumerator = manager.enumerator(
        at: installRoot,
        includingPropertiesForKeys: [.isRegularFileKey, .isSymbolicLinkKey, .isExecutableKey],
        options: [.skipsHiddenFiles]
    ) else { return nil }
    var candidates: [URL] = []
    for case let url as URL in enumerator where ["python3.12", "python3", "python"].contains(url.lastPathComponent) {
        let values = try url.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey, .isExecutableKey])
        if values.isRegularFile == true, values.isSymbolicLink != true, values.isExecutable == true {
            candidates.append(url)
        }
    }
    for candidate in candidates.sorted(by: { $0.path < $1.path }) {
        let result = try runProcess(candidate, arguments: ["--version"], timeout: 12)
        let version = (result.1 + result.2).trimmingCharacters(in: .whitespacesAndNewlines)
        if result.0 == 0, version.hasPrefix("Python 3.12.") { return candidate }
    }
    return nil
}

private func ensurePrivatePython(uv: URL) throws -> URL {
    let state = try privateStateRoot()
    let installRoot = state.appendingPathComponent("python", isDirectory: true)
    let cacheRoot = state.appendingPathComponent("cache", isDirectory: true)
    try FileManager.default.createDirectory(at: installRoot, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    try FileManager.default.createDirectory(at: cacheRoot, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    var uvEnvironment = ProcessInfo.processInfo.environment.filter { key, _ in
        !key.hasPrefix("UV_") && !key.hasPrefix("PYTHON") && key != "VIRTUAL_ENV" && key != "CONDA_PREFIX"
    }
    uvEnvironment["UV_CACHE_DIR"] = cacheRoot.path
    uvEnvironment["UV_NO_CONFIG"] = "1"
    let uvResult = try runProcess(uv, arguments: ["--version"], environment: uvEnvironment, timeout: 12)
    let uvVersion = uvResult.1.trimmingCharacters(in: .whitespacesAndNewlines)
    guard uvResult.0 == 0, uvVersion.hasPrefix("uv 0.12.16") else {
        throw CompanionError.message("The verified companion requires uv 0.12.16.")
    }
    if let existing = try validPython(in: installRoot) { return existing }
    let arguments = [
        "python", "install", "3.12", "--install-dir", installRoot.path,
        "--managed-python", "--no-bin", "--no-registry", "--no-config", "--no-progress",
    ]
    let result = try runProcess(uv, arguments: arguments, environment: uvEnvironment, timeout: 180)
    guard result.0 == 0, let installed = try validPython(in: installRoot) else {
        let detail = (result.2 + result.1).trimmingCharacters(in: .whitespacesAndNewlines)
        throw CompanionError.message(detail.isEmpty ? "Private Python 3.12 could not be installed." : detail)
    }
    return installed
}

private func resolveBackendRuntime() throws -> BackendRuntime {
    runtimeLock.lock()
    defer { runtimeLock.unlock() }
    if let cachedRuntime { return cachedRuntime }

    let environment = ProcessInfo.processInfo.environment
    if let resources = Bundle.main.resourceURL {
        let manifest = resources.appendingPathComponent("companion-manifest.json")
        let uv = resources.appendingPathComponent("bin/uv")
        let payload = resources.appendingPathComponent("payload", isDirectory: true)
        let packageDetected = FileManager.default.fileExists(atPath: manifest.path)
            || FileManager.default.fileExists(atPath: uv.path)
            || FileManager.default.fileExists(atPath: payload.path)
        if packageDetected {
            let package = try verifyPackage(at: resources)
            guard let uvDescriptor = package.files["bin/uv"] else {
                throw CompanionError.message("The companion package does not contain verified uv metadata.")
            }
            let runtime = BackendRuntime(
                python: try ensurePrivatePython(uv: uv),
                sourceRoot: payload.appendingPathComponent("src", isDirectory: true),
                preparationEnvironment: [
                    "DAEMON_HARNESS_COMPANION_PACKAGE_ROOT": resources.path,
                    "DAEMON_HARNESS_COMPANION_INSTALL_HOME": try installHome().path,
                    "DAEMON_HARNESS_COMPANION_BUNDLE_ROOT": payload.path,
                    "DAEMON_HARNESS_COMPANION_UV_EXECUTABLE": uv.path,
                    "DAEMON_HARNESS_COMPANION_UV_SHA256": uvDescriptor.sha256,
                    "DAEMON_HARNESS_COMPANION_TARGET_OS": "macos",
                    "DAEMON_HARNESS_COMPANION_OPENCLAW_API_PORT": "8000",
                    "DAEMON_HARNESS_COMPANION_OPENCLAW_GATEWAY_PORT": "18789",
                ]
            )
            cachedRuntime = runtime
            return runtime
        }
    }
    guard
        let python = environment["DAEMON_HARNESS_COMPANION_PYTHON"], !python.isEmpty,
        let root = environment["DAEMON_HARNESS_COMPANION_ROOT"], !root.isEmpty
    else {
        throw CompanionError.message("No verified companion package or explicit development runtime was found.")
    }
    let runtime = BackendRuntime(
        python: URL(fileURLWithPath: python),
        sourceRoot: URL(fileURLWithPath: root).appendingPathComponent("src", isDirectory: true),
        preparationEnvironment: [:]
    )
    cachedRuntime = runtime
    return runtime
}

private func requestBackend(_ payload: [String: Any]) throws -> [String: Any] {
    guard JSONSerialization.isValidJSONObject(payload) else {
        throw CompanionError.message("The request could not be encoded.")
    }
    let runtime = try resolveBackendRuntime()
    let process = Process()
    let input = Pipe()
    let output = Pipe()
    let errors = Pipe()
    let environment = backendEnvironment(runtime)
    process.executableURL = runtime.python
    process.arguments = ["-m", "daemons_companion.desktop.app", "request"]
    process.environment = environment
    process.standardInput = input
    process.standardOutput = output
    process.standardError = errors
    try process.run()
    input.fileHandleForWriting.write(try JSONSerialization.data(withJSONObject: payload))
    try input.fileHandleForWriting.close()

    let completed = DispatchSemaphore(value: 0)
    process.terminationHandler = { _ in completed.signal() }
    if completed.wait(timeout: .now() + 12) == .timedOut {
        process.terminate()
        throw CompanionError.message("The local companion engine timed out.")
    }
    let data = output.fileHandleForReading.readDataToEndOfFile()
    let diagnostic = String(
        decoding: errors.fileHandleForReading.readDataToEndOfFile(),
        as: UTF8.self
    ).trimmingCharacters(in: .whitespacesAndNewlines)
    guard
        let decoded = try JSONSerialization.jsonObject(with: data) as? [String: Any],
        decoded["schema"] as? String == "daemon.companion-response.v1"
    else {
        throw CompanionError.message(
            diagnostic.isEmpty ? "The local companion engine returned an invalid response." : diagnostic
        )
    }
    if decoded["ok"] as? Bool != true {
        let error = decoded["error"] as? [String: Any]
        throw CompanionError.message(error?["message"] as? String ?? "The request was refused.")
    }
    guard process.terminationStatus == 0 else {
        throw CompanionError.message("The local companion engine failed.")
    }
    return decoded
}

private final class CompanionController: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var hostChoice: NSPopUpButton!
    private var nameField: NSTextField!
    private var genderChoice: NSPopUpButton!
    private var providerChoice: NSPopUpButton!
    private var modelField: NSTextField!
    private var baseURLField: NSTextField!
    private var credentialCheck: NSButton!
    private var reviewButton: NSButton!
    private var hostSetupButton: NSButton!
    private var consentCheck: NSButton!
    private var installButton: NSButton!
    private var statusLabel: NSTextField!
    private var planView: NSTextView!
    private var detailsCheck: NSButton!
    private var reviewSummary = ""
    private var reviewDetails = ""
    private var inspection: [String: Any]?

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        inspectHosts()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 780, height: 700),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Daemon Harness Companion"
        window.center()
        guard let content = window.contentView else { return }

        content.addSubview(label("Set up Daemon Harness", x: 28, y: 645, width: 724, height: 36, size: 25, bold: true))
        content.addSubview(label(
            "Use an existing host or review a qualified version to install. The host remains in charge of channels, tools and permissions.",
            x: 30, y: 595, width: 720, height: 46, size: 13
        ))

        addCaption("Host", y: 554, to: content)
        hostChoice = NSPopUpButton(frame: NSRect(x: 190, y: 548, width: 310, height: 30))
        hostChoice.isEnabled = false
        hostChoice.target = self
        hostChoice.action = #selector(hostChanged)
        content.addSubview(hostChoice)
        hostSetupButton = NSButton(title: "Review host setup", target: self, action: #selector(reviewHostSetup))
        hostSetupButton.frame = NSRect(x: 542, y: 548, width: 190, height: 32)
        hostSetupButton.isEnabled = false
        content.addSubview(hostSetupButton)

        addCaption("Daemon name", y: 510, to: content)
        nameField = NSTextField(frame: NSRect(x: 190, y: 504, width: 310, height: 28))
        nameField.placeholderString = "Choose a name"
        content.addSubview(nameField)

        addCaption("Grammar", y: 466, to: content)
        genderChoice = NSPopUpButton(frame: NSRect(x: 190, y: 460, width: 310, height: 30))
        genderChoice.addItems(withTitles: ["Feminine", "Masculine"])
        content.addSubview(genderChoice)

        addCaption("Model access", y: 422, to: content)
        providerChoice = NSPopUpButton(frame: NSRect(x: 190, y: 416, width: 310, height: 30))
        providerChoice.addItems(withTitles: [
            "ChatGPT with Codex CLI (recommended)",
            "OpenAI API key",
            "OpenAI-compatible API",
        ])
        providerChoice.target = self
        providerChoice.action = #selector(providerChanged)
        content.addSubview(providerChoice)

        addCaption("Model", y: 378, to: content)
        modelField = NSTextField(frame: NSRect(x: 190, y: 372, width: 310, height: 28))
        modelField.stringValue = "gpt-5.6-terra"
        content.addSubview(modelField)

        addCaption("Provider URL", y: 334, to: content)
        baseURLField = NSTextField(frame: NSRect(x: 190, y: 328, width: 310, height: 28))
        baseURLField.placeholderString = "https://provider.example/v1"
        baseURLField.isEnabled = false
        content.addSubview(baseURLField)

        credentialCheck = NSButton(
            checkboxWithTitle: "I have the required API key; it will be stored by the system during installation.",
            target: self,
            action: nil
        )
        credentialCheck.frame = NSRect(x: 190, y: 288, width: 530, height: 26)
        credentialCheck.isHidden = true
        content.addSubview(credentialCheck)

        reviewButton = NSButton(title: "Review exact changes", target: self, action: #selector(reviewPlan))
        reviewButton.frame = NSRect(x: 542, y: 328, width: 190, height: 32)
        reviewButton.isEnabled = false
        content.addSubview(reviewButton)

        let scroll = NSScrollView(frame: NSRect(x: 28, y: 105, width: 724, height: 165))
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        planView = NSTextView(frame: scroll.bounds)
        planView.isEditable = false
        planView.font = NSFont.systemFont(ofSize: 13)
        scroll.documentView = planView
        content.addSubview(scroll)

        consentCheck = NSButton(
            checkboxWithTitle: "I have reviewed and accept these exact changes.",
            target: self,
            action: #selector(consentChanged)
        )
        consentCheck.frame = NSRect(x: 28, y: 67, width: 440, height: 26)
        consentCheck.isEnabled = false
        content.addSubview(consentCheck)

        detailsCheck = NSButton(checkboxWithTitle: "Technical details", target: self, action: #selector(detailsChanged))
        detailsCheck.frame = NSRect(x: 478, y: 67, width: 158, height: 26)
        detailsCheck.isEnabled = false
        content.addSubview(detailsCheck)

        installButton = NSButton(title: "Install", target: self, action: #selector(installUnavailable))
        installButton.frame = NSRect(x: 642, y: 58, width: 110, height: 34)
        installButton.isEnabled = false
        content.addSubview(installButton)

        statusLabel = label("Inspecting this Mac…", x: 28, y: 20, width: 600, height: 28, size: 12)
        statusLabel.textColor = .secondaryLabelColor
        content.addSubview(statusLabel)
    }

    private func label(_ text: String, x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat, size: CGFloat, bold: Bool = false) -> NSTextField {
        let value = NSTextField(wrappingLabelWithString: text)
        value.frame = NSRect(x: x, y: y, width: width, height: height)
        value.font = bold ? NSFont.boldSystemFont(ofSize: size) : NSFont.systemFont(ofSize: size)
        return value
    }

    private func addCaption(_ text: String, y: CGFloat, to view: NSView) {
        view.addSubview(label(text, x: 30, y: y, width: 145, height: 24, size: 13, bold: true))
    }

    private func inspectHosts() {
        reviewButton.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let response = try requestBackend(["schema": requestSchema, "action": "inspect"])
                DispatchQueue.main.async { self.applyInspection(response) }
            } catch {
                DispatchQueue.main.async { self.showError(error.localizedDescription) }
            }
        }
    }

    private func applyInspection(_ response: [String: Any]) {
        guard let value = response["inspection"] as? [String: Any],
              let hosts = value["hosts"] as? [[String: Any]],
              let usable = value["usable_hosts"] as? [String] else {
            showError("Host inspection was incomplete.")
            return
        }
        inspection = value
        hostChoice.removeAllItems()
        for host in hosts {
            let code = host["host"] as? String ?? ""
            let version = host["version"] as? String ?? ""
            let title = code == "hermes" ? "Hermes (recommended)" : "OpenClaw"
            let detail = version.isEmpty ? (host["detected"] as? Bool == true ? "Needs review" : "Not installed") : version
            hostChoice.addItem(withTitle: "\(title) — \(detail)")
            hostChoice.lastItem?.representedObject = code
        }
        if let selected = value["recommended_host"] as? String,
           let index = hostChoice.itemArray.firstIndex(where: { $0.representedObject as? String == selected }) {
            hostChoice.selectItem(at: index)
        }
        hostChoice.isEnabled = true
        hostSetupButton.isEnabled = true
        hostChanged()
        if usable.isEmpty {
            statusLabel.stringValue = "Review a qualified host version. This preview cannot install it yet."
        } else if usable.count == 1 {
            statusLabel.stringValue = "The only usable host was selected automatically. No change has been made."
        } else {
            statusLabel.stringValue = "Both hosts are available. Hermes is recommended; you remain free to choose."
        }
        if value["codex_cli_available"] as? Bool != true {
            providerChoice.item(at: 0)?.isEnabled = false
            providerChoice.selectItem(at: 1)
            providerChanged()
        }
    }

    @objc private func providerChanged() {
        let api = providerChoice.indexOfSelectedItem != 0
        let compatible = providerChoice.indexOfSelectedItem == 2
        credentialCheck.isHidden = !api
        baseURLField.isEnabled = compatible
    }

    @objc private func hostChanged() {
        clearReview()
        let selected = hostChoice.selectedItem?.representedObject as? String ?? ""
        let usable = inspection?["usable_hosts"] as? [String] ?? []
        reviewButton.isEnabled = usable.contains(selected)
        statusLabel.stringValue = "Review host setup to see compatibility and installation requirements."
    }

    private func clearReview() {
        reviewSummary = ""
        reviewDetails = ""
        detailsCheck.state = .off
        detailsCheck.isEnabled = false
        consentCheck.state = .off
        consentCheck.isEnabled = false
        installButton.isEnabled = false
        planView.string = ""
        planView.font = NSFont.systemFont(ofSize: 13)
    }

    @objc private func detailsChanged() {
        let detailed = detailsCheck.state == .on
        planView.font = detailed ? NSFont.monospacedSystemFont(ofSize: 12, weight: .regular) : NSFont.systemFont(ofSize: 13)
        planView.string = detailed ? reviewDetails : reviewSummary
    }

    @objc private func reviewHostSetup() {
        guard let host = hostChoice.selectedItem?.representedObject as? String else { return }
        clearReview()
        hostSetupButton.isEnabled = false
        hostChoice.isEnabled = false
        reviewButton.isEnabled = false
        consentCheck.state = .off
        consentCheck.isEnabled = false
        installButton.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let response = try requestBackend(["schema": requestSchema, "action": "host_setup", "host": host])
                guard let setup = response["host_setup"] as? [String: Any],
                      let summary = setup["summary"] as? String else {
                    throw CompanionError.message("Host setup review was incomplete.")
                }
                DispatchQueue.main.async {
                    self.hostChanged()
                    self.planView.string = summary
                    self.hostSetupButton.isEnabled = true
                    self.hostChoice.isEnabled = true
                    self.statusLabel.stringValue = "Host setup reviewed. No download or installation has started."
                }
            } catch {
                DispatchQueue.main.async {
                    self.hostChanged()
                    self.hostSetupButton.isEnabled = true
                    self.hostChoice.isEnabled = true
                    self.showError(error.localizedDescription)
                }
            }
        }
    }

    @objc private func reviewPlan() {
        clearReview()
        guard let host = hostChoice.selectedItem?.representedObject as? String else {
            showError("Choose an installed host.")
            return
        }
        let provider: [String: Any]
        switch providerChoice.indexOfSelectedItem {
        case 0:
            provider = ["kind": "codex_cli", "model": modelField.stringValue,
                        "base_url": "", "credential_available": false]
        case 1:
            provider = ["kind": "openai_api", "model": modelField.stringValue,
                        "base_url": "", "credential_available": credentialCheck.state == .on]
        default:
            provider = ["kind": "openai_compatible_api", "model": modelField.stringValue,
                        "base_url": baseURLField.stringValue,
                        "credential_available": credentialCheck.state == .on]
        }
        let payload: [String: Any] = [
            "schema": requestSchema,
            "action": "prepare",
            "host": host,
            "daemon_name": nameField.stringValue,
            "daemon_gender": genderChoice.indexOfSelectedItem == 0 ? "feminine" : "masculine",
            "provider": provider,
        ]
        reviewButton.isEnabled = false
        statusLabel.stringValue = "Preparing the exact plan…"
        hostChoice.isEnabled = false
        hostSetupButton.isEnabled = false
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let response = try requestBackend(payload)
                guard let summary = response["review_text"] as? String else {
                    throw CompanionError.message("The setup summary was incomplete.")
                }
                let data = try JSONSerialization.data(withJSONObject: response, options: [.prettyPrinted, .sortedKeys])
                let text = String(decoding: data, as: UTF8.self)
                DispatchQueue.main.async {
                    self.reviewSummary = summary
                    self.reviewDetails = text
                    self.detailsCheck.isEnabled = true
                    self.planView.string = summary
                    self.consentCheck.state = .off
                    self.consentCheck.isEnabled = true
                    self.installButton.isEnabled = false
                    self.reviewButton.isEnabled = true
                    self.hostChoice.isEnabled = true
                    self.hostSetupButton.isEnabled = true
                    self.statusLabel.stringValue = "Review ready. This preview cannot install yet."
                }
            } catch {
                DispatchQueue.main.async {
                    self.reviewButton.isEnabled = true
                    self.hostChoice.isEnabled = true
                    self.hostSetupButton.isEnabled = true
                    self.showError(error.localizedDescription)
                }
            }
        }
    }

    @objc private func consentChanged() {
        installButton.isEnabled = false
        if consentCheck.state == .on {
            statusLabel.stringValue = "Consent recorded locally. The install action is not enabled in this preview."
        }
    }

    @objc private func installUnavailable() {
        showError("Installation is not enabled in this qualified review preview.")
    }

    private func showError(_ message: String) {
        statusLabel.stringValue = message
        let alert = NSAlert()
        alert.messageText = "Daemon Harness Companion"
        alert.informativeText = message
        alert.alertStyle = .warning
        alert.runModal()
    }
}

private func runSmokeTest() -> Never {
    let cases = [
        ([], nil),
        (["hermes"], "hermes"),
        (["openclaw"], "openclaw"),
        (["hermes", "openclaw"], "hermes"),
    ]
    guard cases.allSatisfy({ automaticHost($0.0) == $0.1 }) else {
        fputs("COMPANION_MACOS_SMOKE_FAILED\n", stderr)
        exit(1)
    }
    print("COMPANION_MACOS_SMOKE_OK host_detection=automatic consent=required install=locked")
    exit(0)
}

private func runBackendSmokeTest() -> Never {
    do {
        let response = try requestBackend(["schema": requestSchema, "action": "inspect"])
        guard
            let inspection = response["inspection"] as? [String: Any],
            let usable = inspection["usable_hosts"] as? [String]
        else {
            throw CompanionError.message("The packaged backend inspection was incomplete.")
        }
        for host in usable {
            let codexAvailable = inspection["codex_cli_available"] as? Bool == true
            let plan = try requestBackend([
                "schema": requestSchema,
                "action": "prepare",
                "host": host,
                "daemon_name": "Avery",
                "daemon_gender": "feminine",
                "provider": [
                    "kind": codexAvailable ? "codex_cli" : "openai_api",
                    "model": "gpt-5.6-terra",
                    "base_url": "",
                    "credential_available": !codexAvailable,
                ],
            ])
            let expectedSteps = host == "hermes" ? 7 : 8
            guard
                let technical = plan["plan"] as? [String: Any],
                let steps = technical["steps"] as? [[String: Any]],
                steps.count == expectedSteps,
                plan["reviewed_plan"] is [String: Any],
                plan["confirmation_digest"] is String
            else {
                throw CompanionError.message("The packaged backend plan was incomplete.")
            }
        }
        print("COMPANION_MACOS_PACKAGED_BACKEND_OK")
        exit(0)
    } catch {
        fputs("COMPANION_MACOS_PACKAGED_BACKEND_FAILED: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
}

private func runTerminal() -> Never {
    guard isatty(STDIN_FILENO) != 0 && isatty(STDOUT_FILENO) != 0 else {
        fputs("DAEMONS requires an interactive terminal. The graphical preview has been retired.\n", stderr)
        exit(2)
    }
    do {
        print("Preparing DAEMONS. A verified package may download its private Python on first launch.")
        fflush(stdout)
        let runtime = try resolveBackendRuntime()
        let arguments = [runtime.python.path, "-m", "daemons_companion.desktop.terminal"] +
            CommandLine.arguments.filter { ["--plain", "--no-animation", "--logo"].contains($0) }
        var environment = backendEnvironment(runtime)
        environment.removeValue(forKey: "PYTHONHOME")
        environment["PYTHONUTF8"] = "1"
        // Preserve the foreground terminal process group. Foundation Process
        // may create a background group, which is suspended by tcsetattr/read.
        var argv = arguments.map { strdup($0) } + [nil]
        var envp = environment.keys.sorted().map { strdup("\($0)=\(environment[$0]!)") } + [nil]
        defer {
            for pointer in argv { free(pointer) }
            for pointer in envp { free(pointer) }
        }
        execve(runtime.python.path, &argv, &envp)
        throw CompanionError.message("The terminal runtime could not replace the launcher.")
    } catch {
        fputs("DAEMONS could not start. Check the verified package and private runtime.\n", stderr)
        exit(1)
    }
}

if CommandLine.arguments.contains("--terminal") {
    runTerminal()
}
if CommandLine.arguments.contains("--smoke-test") {
    runSmokeTest()
}
if CommandLine.arguments.contains("--backend-smoke-test") {
    runBackendSmokeTest()
}

// Finder/LaunchServices may restart the app without the shell's --terminal flag.
// Never fall back to the retired graphical preview.
runTerminal()
