using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

// Thin terminal bootstrap. All installation decisions remain in the shared
// Python engine. No PowerShell, admin rights, system Python or global PATH edits.
internal static class Program
{
    // No ANSI requirement: the bootstrap runs before Python configures the console.
    internal sealed class Activity : IDisposable
    {
        readonly CancellationTokenSource stopped = new();
        readonly Task worker;
        public Activity(string label, TextWriter output, bool animated)
        {
            output.WriteLine(label);
            worker = animated ? Task.Run(async () =>
            {
                var elapsed = Stopwatch.StartNew();
                int frame = 0;
                try
                {
                    while (!stopped.IsCancellationRequested)
                    {
                        output.Write($"\r  {"|/-\\"[frame++ % 4]} {elapsed.Elapsed:mm\\:ss}  Working...");
                        output.Flush();
                        await Task.Delay(120, stopped.Token);
                    }
                }
                catch (OperationCanceledException) { }
                catch (IOException) { } // A display failure must not change setup.
                finally { output.Write("\r                              \r"); output.Flush(); }
            }) : Task.CompletedTask;
        }
        public void Dispose()
        {
            stopped.Cancel();
            worker.GetAwaiter().GetResult();
        }
    }
    static void Direct(string path)
    {
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new InvalidDataException("A package or bootstrap path is a link.");
    }
    static string Hash(string path)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexStringLower(SHA256.HashData(stream));
    }
    static string Verify(string root)
    {
        Direct(root);
        var manifest = Path.Combine(root, "companion-manifest.json");
        Direct(manifest);
        if (new FileInfo(manifest).Length > 2 * 1024 * 1024)
            throw new InvalidDataException("Package manifest is too large.");
        using var doc = JsonDocument.Parse(File.ReadAllText(manifest));
        if (doc.RootElement.GetProperty("schema").GetString() != "daemon.companion-package.v1")
            throw new InvalidDataException("Unsupported package manifest.");
        var files = doc.RootElement.GetProperty("files");
        var expected = new Dictionary<string, JsonElement>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in files.EnumerateObject())
        {
            if (entry.Name.Contains('\\') || entry.Name.Contains(':') || entry.Name.Contains('\0') ||
                entry.Name.Split('/').Any(p => p is "" or "." or "..") ||
                !expected.TryAdd(entry.Name, entry.Value) || expected.Count > 5000)
                throw new InvalidDataException("Unsafe package path.");
        }
        // Walk one level at a time: reject links before descending into them.
        var queue = new Queue<string>(); queue.Enqueue(root);
        int count = 0;
        while (queue.TryDequeue(out var directory))
        {
            foreach (var path in Directory.EnumerateFileSystemEntries(directory))
            {
                Direct(path);
                if (Directory.Exists(path)) { queue.Enqueue(path); continue; }
                var relative = Path.GetRelativePath(root, path).Replace('\\', '/');
                if (relative == "companion-manifest.json") continue;
                if (!expected.TryGetValue(relative, out var value) ||
                    new FileInfo(path).Length != value.GetProperty("size").GetInt64() ||
                    Hash(path) != value.GetProperty("sha256").GetString() ||
                    Path.GetExtension(path).Equals(".exe", StringComparison.OrdinalIgnoreCase) != value.GetProperty("executable").GetBoolean())
                    throw new InvalidDataException("Package integrity verification failed. Extract a fresh complete ZIP.");
                count++;
            }
        }
        if (count != expected.Count || !expected.TryGetValue("bin/uv.exe", out var uv))
            throw new InvalidDataException("The package is incomplete.");
        return uv.GetProperty("sha256").GetString()!;
    }
    static ProcessStartInfo Command(string executable, IEnumerable<string> args)
    {
        var info = new ProcessStartInfo(executable) { UseShellExecute = false };
        foreach (var arg in args) info.ArgumentList.Add(arg);
        foreach (var key in info.Environment.Keys.ToArray())
            if (key.StartsWith("UV_", StringComparison.OrdinalIgnoreCase) ||
                key.StartsWith("PYTHON", StringComparison.OrdinalIgnoreCase) ||
                key is "VIRTUAL_ENV" or "CONDA_PREFIX") info.Environment.Remove(key);
        return info;
    }
    static string Run(ProcessStartInfo info, int seconds)
    {
        info.RedirectStandardOutput = info.RedirectStandardError = true;
        using var process = Process.Start(info) ?? throw new IOException("Private runtime could not start.");
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        if (!process.WaitForExit(seconds * 1000))
        {
            process.Kill(entireProcessTree: true); process.WaitForExit();
            throw new TimeoutException("Private runtime download timed out. Check Internet access and retry.");
        }
        Task.WaitAll(stdout, stderr);
        if (process.ExitCode != 0) throw new IOException("Private runtime failed: " + stderr.Result.Trim());
        return stdout.Result.Trim();
    }
    static string FindPrivatePython(string uv, string pythonRoot, string cache)
    {
        // Ask the runtime manager: Lib/venv/scripts/nt/python.exe is only a
        // redirector template and cannot run without a virtual environment.
        var find = Command(uv, new[] { "python", "find", "3.12", "--managed-python",
            "--system", "--no-project", "--no-config", "--no-python-downloads", "--resolve-links" });
        find.Environment["UV_PYTHON_INSTALL_DIR"] = pythonRoot;
        find.Environment["UV_CACHE_DIR"] = cache;
        var python = Run(find, 30);
        if (!Path.IsPathFullyQualified(python)) throw new InvalidDataException("Private Python path is not absolute.");
        var relative = Path.GetRelativePath(pythonRoot, python);
        if (Path.IsPathRooted(relative) || relative.Split(Path.DirectorySeparatorChar).Any(p => p == ".."))
            throw new InvalidDataException("Python is outside the private runtime directory.");
        for (string? item = python; item != null; item = Path.GetDirectoryName(item))
        {
            Direct(item);
            if (Path.GetRelativePath(pythonRoot, item) == ".") break;
        }
        if (!Run(Command(python, new[] { "--version" }), 15).StartsWith("Python 3.12."))
            throw new InvalidDataException("Expected private Python 3.12.");
        return python;
    }
    static void CreateDirect(string path)
    {
        var ancestors = new Stack<string>();
        for (string? item = Path.GetFullPath(path); item != null; item = Path.GetDirectoryName(item))
            ancestors.Push(item);
        foreach (var item in ancestors)
        {
            if (!Directory.Exists(item)) Directory.CreateDirectory(item);
            Direct(item);
        }
    }
    static int Main(string[] args)
    {
        try
        {
            if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException("This launcher requires Windows x64.");
            Console.OutputEncoding = new UTF8Encoding(false);
            using var activity = new Activity("Preparing DAEMONS. First launch may download private Python.", Console.Out, !Console.IsOutputRedirected && !args.Contains("--plain"));
            var root = AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
            var digest = Verify(root);
            var home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "DaemonHarness");
            var bootstrap = Path.Combine(home, "Companion", "bootstrap");
            var pythonRoot = Path.Combine(bootstrap, "python");
            var cache = Path.Combine(bootstrap, "cache");
            CreateDirect(pythonRoot); CreateDirect(cache);
            var uv = Path.Combine(root, "bin", "uv.exe");
            ProcessStartInfo Uv(params string[] options)
            {
                var info = Command(uv, options);
                info.Environment["UV_CACHE_DIR"] = cache;
                info.Environment["UV_NO_CONFIG"] = "1";
                return info;
            }
            if (!Run(Uv("--version"), 15).StartsWith("uv 0.12.16 "))
                throw new InvalidDataException("Expected verified uv 0.12.16.");
            Run(Uv("python", "install", "3.12", "--install-dir", pythonRoot,
                "--managed-python", "--no-bin", "--no-registry", "--no-config", "--no-progress"), 300);
            var python = FindPrivatePython(uv, pythonRoot, cache);
            var options = new List<string> { "-B", "-m", "daemons_companion.desktop.terminal" };
            foreach (var arg in args)
            {
                if (arg is not ("--plain" or "--no-animation" or "--logo")) throw new ArgumentException("Unknown launcher option.");
                options.Add(arg);
            }
            var start = Command(python, options);
            start.WorkingDirectory = Path.Combine(root, "payload");
            var env = start.Environment;
            env["PYTHONUTF8"] = "1";
            env["PYTHONDONTWRITEBYTECODE"] = "1";
            env["PYTHONPATH"] = Path.Combine(root, "payload", "src");
            env["DAEMON_HARNESS_COMPANION_PACKAGE_ROOT"] = root;
            env["DAEMON_HARNESS_COMPANION_INSTALL_HOME"] = home;
            env["DAEMON_HARNESS_COMPANION_BUNDLE_ROOT"] = Path.Combine(root, "payload");
            env["DAEMON_HARNESS_COMPANION_UV_EXECUTABLE"] = uv;
            env["DAEMON_HARNESS_COMPANION_UV_SHA256"] = digest;
            env["DAEMON_HARNESS_COMPANION_TARGET_OS"] = "windows";
            env["DAEMON_HARNESS_COMPANION_OPENCLAW_API_PORT"] = "8000";
            env["DAEMON_HARNESS_COMPANION_OPENCLAW_GATEWAY_PORT"] = "18789";
            activity.Dispose(); // Stop and join before the foreground wizard owns output/input.
            using var wizard = Process.Start(start) ?? throw new IOException("DAEMONS could not start.");
            // Let the foreground Python process perform its transactional Ctrl+C cleanup.
            Console.CancelKeyPress += (_, e) => e.Cancel = true;
            wizard.WaitForExit();
            return wizard.ExitCode;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("DAEMONS: " + error.Message);
            if (!Console.IsInputRedirected) { Console.WriteLine("Press Enter to close."); Console.ReadLine(); }
            return 1;
        }
    }
}
