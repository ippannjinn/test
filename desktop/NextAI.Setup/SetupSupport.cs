using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net.Http;
using System.Reflection;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.Win32;
using NextAI.Common;

namespace NextAI.Setup
{
    static class Payload
    {
        public const string ResourceName = "NextAI.Setup.payload.zip";

        public static Stream Open() =>
            Assembly.GetExecutingAssembly().GetManifestResourceStream(ResourceName)
            ?? throw new InvalidOperationException("インストーラーにペイロードが含まれていません (ビルド不備)");

        public static string ReadText(string entry)
        {
            using (var s = Open())
            using (var z = new ZipArchive(s, ZipArchiveMode.Read))
            {
                var e = z.GetEntry(entry) ?? throw new FileNotFoundException(entry);
                using (var r = new StreamReader(e.Open())) return r.ReadToEnd();
            }
        }

        public static string Version()
        {
            try { return ReadText("version.txt").Trim(); } catch { return "0.0.0"; }
        }

        /// <summary>Extracts every entry to <paramref name="dest"/>, rejecting path traversal.</summary>
        public static void ExtractTo(string dest, Action<double> progress)
        {
            var root = Path.GetFullPath(dest).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
            using (var s = Open())
            using (var z = new ZipArchive(s, ZipArchiveMode.Read))
            {
                var n = 0;
                foreach (var e in z.Entries)
                {
                    var target = Path.GetFullPath(Path.Combine(dest, e.FullName));
                    if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("不正なペイロード: " + e.FullName);
                    if (e.FullName.EndsWith("/")) { Directory.CreateDirectory(target); continue; }
                    Directory.CreateDirectory(Path.GetDirectoryName(target));
                    e.ExtractToFile(target, true);
                    progress?.Invoke(++n / (double)z.Entries.Count);
                }
            }
        }
    }

    sealed class ModelSet
    {
        public string Id, Name;
        public bool Auto, Eligible;
        public double SizeGb, NeedVram, NeedRam, NeedDisk;
        public List<string> Models = new List<string>();
        public List<string> Runtimes = new List<string>();
        public override string ToString() => $"{Name}  (約{SizeGb:F0}GB){(Eligible ? "" : "  ※要件未達")}";
    }

    static class Catalog
    {
        /// <summary>Mirrors Catalog.select_set in the server: first auto set whose requirements this PC meets.</summary>
        public static List<ModelSet> Load(double vramGb, double ramGb, double diskGb)
        {
            var root = Json.ParseObject(Payload.ReadText("catalog.json"));
            var sizes = root.Arr("models").Objects().ToDictionary(m => m.Str("id"), m => m.Num("size_gb"));
            var rts = root.Obj("runtimes");
            var list = new List<ModelSet>();
            foreach (var s in root.Arr("sets").Objects())
            {
                var req = s.Obj("requires");
                var set = new ModelSet
                {
                    Id = s.Str("id"), Name = s.Str("name"), Auto = s.Bool("auto", true),
                    NeedVram = req.Num("vram_gb"), NeedRam = req.Num("ram_gb"), NeedDisk = req.Num("disk_free_gb"),
                };
                set.Models.AddRange(s.Arr("models").Cast<string>());
                set.Runtimes.AddRange(s.Arr("runtimes").Cast<string>());
                set.SizeGb = set.Models.Sum(m => sizes.TryGetValue(m, out var v) ? v : 0) + set.Runtimes.Sum(r => rts.Obj(r).Num("size_gb"));
                set.Eligible = vramGb >= set.NeedVram && ramGb >= set.NeedRam && diskGb >= set.NeedDisk;
                list.Add(set);
            }
            return list;
        }

        public static ModelSet Recommend(List<ModelSet> sets) => sets.FirstOrDefault(s => s.Auto && s.Eligible) ?? sets.LastOrDefault();
    }

    static class Shell
    {
        public static void CreateShortcut(string lnk, string target, string args, string description, string icon = null)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(lnk));
            var t = Type.GetTypeFromProgID("WScript.Shell");
            dynamic sh = Activator.CreateInstance(t);
            try
            {
                dynamic s = sh.CreateShortcut(lnk);
                s.TargetPath = target;
                s.Arguments = args ?? "";
                s.Description = description;
                s.WorkingDirectory = Path.GetDirectoryName(target);
                if (icon != null) s.IconLocation = icon;
                s.Save();
            }
            finally { System.Runtime.InteropServices.Marshal.FinalReleaseComObject(sh); }
        }

        public static void CreateUrlShortcut(string path, string url, string icon)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.WriteAllText(path, $"[InternetShortcut]\r\nURL={url}\r\nIconFile={icon}\r\nIconIndex=0\r\n");
        }

        public static string StartMenuDir =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonPrograms), "NextAI Platform");

        public static string DesktopLink =>
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonDesktopDirectory), "NextAI 管理コンソール.lnk");

        public static int Run(string exe, params string[] args)
        {
            var psi = new ProcessStartInfo(exe, ProcessRunner.JoinArgs(args)) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
            using (var p = Process.Start(psi))
            {
                p.StandardOutput.ReadToEnd();
                p.StandardError.ReadToEnd();
                p.WaitForExit();
                return p.ExitCode;
            }
        }

        public static (int code, string output) Capture(string exe, params string[] args)
        {
            try
            {
                var psi = new ProcessStartInfo(exe, ProcessRunner.JoinArgs(args))
                {
                    UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                };
                using (var p = Process.Start(psi))
                {
                    var err = p.StandardError.ReadToEndAsync();
                    var output = p.StandardOutput.ReadToEnd() + err.Result;
                    p.WaitForExit();
                    return (p.ExitCode, output.Trim());
                }
            }
            catch (Exception ex) { return (-1, ex.Message); }
        }

        /// <summary>Launches a program as the non-elevated desktop user (via Explorer).</summary>
        public static void LaunchUnelevated(string target)
        {
            try { Process.Start(new ProcessStartInfo("explorer.exe", ProcessRunner.Quote(target)) { UseShellExecute = true }); } catch { }
        }

        public static void WriteRegistry(InstallInfo info, string version, long estimatedKb)
        {
            using (var k = Registry.LocalMachine.CreateSubKey(InstallInfo.RegistryKey))
            {
                k.SetValue("InstallDir", info.InstallDir);
                k.SetValue("DataDir", info.DataDir);
                k.SetValue("Version", version);
                k.SetValue("Port", info.Port, RegistryValueKind.DWord);
                k.SetValue("ModelSet", info.ModelSet ?? "");
            }
            using (var k = Registry.LocalMachine.CreateSubKey(InstallInfo.UninstallKey))
            {
                var setup = Path.Combine(info.InstallDir, "NextAI-Setup.exe");
                k.SetValue("DisplayName", "NextAI Platform");
                k.SetValue("DisplayVersion", version);
                k.SetValue("Publisher", "NextAI Platform");
                k.SetValue("InstallLocation", info.InstallDir);
                k.SetValue("DisplayIcon", Path.Combine(info.InstallDir, "NextAI.Admin.exe"));
                k.SetValue("UninstallString", $"\"{setup}\" /uninstall");
                k.SetValue("ModifyPath", $"\"{setup}\" /repair");
                k.SetValue("EstimatedSize", (int)Math.Min(int.MaxValue, estimatedKb), RegistryValueKind.DWord);
                k.SetValue("NoRepair", 0, RegistryValueKind.DWord);
                k.SetValue("URLInfoAbout", $"https://localhost:{info.Port}/");
            }
        }

        public static InstallInfo ReadExisting(out string version)
        {
            version = null;
            try
            {
                using (var k = Registry.LocalMachine.OpenSubKey(InstallInfo.RegistryKey))
                {
                    if (k == null) return null;
                    version = Convert.ToString(k.GetValue("Version"));
                    var dir = Convert.ToString(k.GetValue("InstallDir"));
                    var json = Path.Combine(dir, "install.json");
                    if (File.Exists(json)) return InstallInfo.Load(json);
                    return new InstallInfo
                    {
                        InstallDir = dir, DataDir = Convert.ToString(k.GetValue("DataDir")),
                        Port = Convert.ToInt32(k.GetValue("Port") ?? 8443), ModelSet = Convert.ToString(k.GetValue("ModelSet")),
                    };
                }
            }
            catch { return null; }
        }

        public static void AdminAutostart(string exe, bool enable)
        {
            using (var k = Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run"))
            {
                if (enable) k.SetValue("NextAIAdmin", $"\"{exe}\"");
                else if (k.GetValue("NextAIAdmin") != null) k.DeleteValue("NextAIAdmin");
            }
        }
    }

    /// <summary>Resumable, verified single-stream download (used for uv before Python exists).</summary>
    static class SimpleDownloader
    {
        public static async Task DownloadAsync(HttpClient http, string url, string dest, string sha256, Action<long, long> progress, CancellationToken ct)
        {
            if (File.Exists(dest) && Sha256(dest) == sha256) return;
            var part = dest + ".part";
            Directory.CreateDirectory(Path.GetDirectoryName(dest));
            for (var attempt = 0; ; attempt++)
            {
                try
                {
                    var have = File.Exists(part) ? new FileInfo(part).Length : 0;
                    using (var req = new HttpRequestMessage(HttpMethod.Get, url))
                    {
                        if (have > 0) req.Headers.Range = new System.Net.Http.Headers.RangeHeaderValue(have, null);
                        using (var resp = await http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct))
                        {
                            if (have > 0 && resp.StatusCode != System.Net.HttpStatusCode.PartialContent) have = 0;
                            resp.EnsureSuccessStatusCode();
                            var total = have + (resp.Content.Headers.ContentLength ?? 0);
                            using (var src = await resp.Content.ReadAsStreamAsync())
                            using (var dst = new FileStream(part, have > 0 ? FileMode.Append : FileMode.Create, FileAccess.Write))
                            {
                                var buf = new byte[1 << 20];
                                int n;
                                while ((n = await src.ReadAsync(buf, 0, buf.Length, ct)) > 0)
                                {
                                    await dst.WriteAsync(buf, 0, n, ct);
                                    have += n;
                                    progress?.Invoke(have, total);
                                }
                            }
                        }
                    }
                    break;
                }
                catch (Exception) when (attempt < 5 && !ct.IsCancellationRequested)
                {
                    await Task.Delay(TimeSpan.FromSeconds(Math.Pow(2, attempt + 1)), ct);
                }
            }
            if (!string.IsNullOrEmpty(sha256) && Sha256(part) != sha256)
            {
                File.Delete(part);
                throw new InvalidDataException("ダウンロードしたファイルのチェックサムが一致しません: " + Path.GetFileName(dest));
            }
            if (File.Exists(dest)) File.Delete(dest);
            File.Move(part, dest);
        }

        public static string Sha256(string path)
        {
            using (var s = File.OpenRead(path))
            using (var h = SHA256.Create())
                return BitConverter.ToString(h.ComputeHash(s)).Replace("-", "").ToLowerInvariant();
        }
    }
}
