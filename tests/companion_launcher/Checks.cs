using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Diagnostics;

internal static class LauncherChecks
{
    static readonly MethodInfo Verify = typeof(Program).GetMethod("Verify", BindingFlags.NonPublic | BindingFlags.Static)!;
    static void Main(string[] args)
    {
        var root = Path.Combine(Path.GetTempPath(), "daemons-launcher-checks-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(root, "bin"));
        try
        {
            var uv = Path.Combine(root, "bin", "uv.exe");
            File.WriteAllText(uv, "MZsynthetic");
            var digest = Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(uv)));
            var files = new Dictionary<string, object> { ["bin/uv.exe"] = new { sha256 = digest, size = new FileInfo(uv).Length, executable = true } };
            void Manifest() => File.WriteAllText(Path.Combine(root, "companion-manifest.json"), JsonSerializer.Serialize(new { schema = "daemon.companion-package.v1", files }));
            void Reject()
            {
                try { Verify.Invoke(null, new object[] { root }); }
                catch (TargetInvocationException e) when (e.InnerException is InvalidDataException) { return; }
                throw new Exception("Unsafe package accepted");
            }
            Manifest();
            if ((string)Verify.Invoke(null, new object[] { root })! != digest) throw new Exception("Valid package rejected");
            File.AppendAllText(uv, "changed"); Reject(); File.WriteAllText(uv, "MZsynthetic");
            var extra = Path.Combine(root, "unexpected.txt"); File.WriteAllText(extra, "extra"); Reject(); File.Delete(extra);
            files["../escape"] = files["bin/uv.exe"]; Manifest(); Reject(); files.Remove("../escape");
            files["BIN/UV.EXE"] = files["bin/uv.exe"]; Manifest(); Reject(); files.Remove("BIN/UV.EXE");
            files["bin/uv.exe"] = new { sha256 = digest, size = new FileInfo(uv).Length, executable = false }; Manifest(); Reject();
            files["bin/uv.exe"] = new { sha256 = digest, size = new FileInfo(uv).Length, executable = true }; Manifest();
            File.Delete(uv); Reject(); File.WriteAllText(uv, "MZsynthetic");
            if (!OperatingSystem.IsWindows())
            {
                var link = Path.Combine(root, "linked-dir"); Directory.CreateSymbolicLink(link, Path.GetTempPath()); Reject(); Directory.Delete(link);
            }
            var command = typeof(Program).GetMethod("Command", BindingFlags.NonPublic | BindingFlags.Static)!;
            Environment.SetEnvironmentVariable("UV_PYTHON", "untrusted");
            Environment.SetEnvironmentVariable("PYTHONHOME", "untrusted");
            var start = (ProcessStartInfo)command.Invoke(null, new object[] { "synthetic", new[] { "path with spaces" } })!;
            if (start.Environment.ContainsKey("UV_PYTHON") || start.Environment.ContainsKey("PYTHONHOME") || start.ArgumentList[0] != "path with spaces") throw new Exception("Unsafe child environment/arguments");
            if (!OperatingSystem.IsWindows())
            {
                var runtime = Path.Combine(root, "private python");
                var distribution = Path.Combine(runtime, "cpython-3.12-windows-x86_64-none");
                var templates = Path.Combine(distribution, "Lib", "venv", "scripts", "nt");
                Directory.CreateDirectory(templates);
                void Executable(string path, string text)
                {
                    File.WriteAllText(path, "#!/bin/sh\n" + text);
                    if (!OperatingSystem.IsWindows())
                        File.SetUnixFileMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
                }
                var real = Path.Combine(distribution, "python.exe");
                var decoy = Path.Combine(templates, "python.exe");
                Executable(real, "echo 'Python 3.12.13'\n");
                Executable(decoy, "echo 'No pyvenv.cfg file' >&2; exit 1\n");
                // The former recursive selection chooses this template first.
                var first = Directory.EnumerateFiles(runtime, "python.exe", SearchOption.AllDirectories).Order().First();
                if (first != decoy) throw new Exception("Regression fixture did not reproduce the old ordering");
                var run = typeof(Program).GetMethod("Run", BindingFlags.NonPublic | BindingFlags.Static)!;
                var broken = (ProcessStartInfo)command.Invoke(null, new object[] { first, new[] { "--version" } })!;
                try { run.Invoke(null, new object[] { broken, 15 }); throw new Exception("Old selection unexpectedly worked"); }
                catch (TargetInvocationException e) when (e.InnerException is IOException && e.InnerException.Message.Contains("No pyvenv.cfg file")) { }
                var locator = Path.Combine(root, "locator");
                var resultFile = Path.Combine(root, "located");
                Executable(locator, "test \"$*\" = 'python find 3.12 --managed-python --system --no-project --no-config --no-python-downloads --resolve-links' || exit 2\n"
                    + "test -n \"$UV_PYTHON_INSTALL_DIR\" || exit 3\ncat '" + resultFile + "'\n");
                var find = typeof(Program).GetMethod("FindPrivatePython", BindingFlags.NonPublic | BindingFlags.Static)!;
                string Locate() => (string)find.Invoke(null, new object[] { locator, runtime, root })!;
                File.WriteAllText(resultFile, real);
                if (Locate() != real) throw new Exception("Managed interpreter not selected");
                File.WriteAllText(resultFile, locator);
                try { Locate(); throw new Exception("External interpreter accepted"); }
                catch (TargetInvocationException e) when (e.InnerException is InvalidDataException) { }
                File.Delete(real); File.CreateSymbolicLink(real, decoy); File.WriteAllText(resultFile, real);
                try { Locate(); throw new Exception("Linked interpreter accepted"); }
                catch (TargetInvocationException e) when (e.InnerException is InvalidDataException) { }
            }
            var display = new StringWriter();
            using (var activity = new Program.Activity("Preparing isolated fixture", display, true))
            {
                Thread.Sleep(450);
                activity.Dispose();
                var finished = display.ToString();
                Thread.Sleep(180);
                if (display.ToString() != finished || !finished.Contains("| 00:00") || !finished.Contains("/ 00:00"))
                    throw new Exception("Bootstrap indicator did not animate or stop");
            }
            var plain = new StringWriter();
            using (var activity = new Program.Activity("Preparing isolated fixture", plain, false)) { }
            if (plain.ToString().Contains("\r")) throw new Exception("Redirected output was animated");
            if (args.Length == 3)
            {
                var find = typeof(Program).GetMethod("FindPrivatePython", BindingFlags.NonPublic | BindingFlags.Static)!;
                find.Invoke(null, new object[] { args[0], args[1], args[2] });
                Console.WriteLine("MANAGED_UV_RESOLUTION_OK");
            }
            Console.WriteLine("LAUNCHER_CHECKS_OK integrity=8 environment=1 python_resolution=4 activity=2 native_windows=false");
        }
        finally { Directory.Delete(root, recursive: true); }
    }
}
