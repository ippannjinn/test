using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using NextAI.Common;

namespace NextAI.Setup
{
    sealed class InstallOptions
    {
        public string InstallDir = InstallInfo.DefaultInstallDir;
        public string DataDir = InstallInfo.DefaultDataDir;
        public int Port = 8443;
        public bool Lan = true;
        public string ServerName = "NextAI Platform";
        public ModelSet Set;
        public bool SkipModels;
        public string AdminUser = "";
        public string AdminPassword = "";
        public bool DesktopShortcut = true;
        public bool AutostartAdmin;
        public bool Upgrade;
        public string PreviousVersion;
    }

    class StepFailed : Exception
    {
        public StepFailed(string message) : base(message) { }
    }

    /// <summary>The executable could not be started at all (as opposed to running and failing).</summary>
    sealed class StartFailed : StepFailed
    {
        public StartFailed(string message) : base(message) { }
    }

    /// <summary>Every step is idempotent, so re-running setup (or pressing "retry") resumes where it stopped.</summary>
    sealed class Installer
    {
        public readonly InstallOptions O;
        public readonly List<string> Steps = new List<string>();
        public event Action<int, string> StepState;          // index, "run"|"done"|"warn"|"fail"
        public event Action<double, string> StepProgress;     // 0..1 (or <0 = indeterminate), detail
        public event Action<string> Log;
        public List<string> Urls = new List<string>();
        public List<string> Warnings = new List<string>();
        readonly string version;
        readonly HttpClient http = new HttpClient { Timeout = TimeSpan.FromMinutes(30) };
        readonly List<(string title, Func<CancellationToken, Task> run)> plan = new List<(string, Func<CancellationToken, Task>)>();

        string Runtime => Path.Combine(O.DataDir, "runtime");
        bool uvOk = true;
        string Uv => Path.Combine(Runtime, "uv", "uv.exe");
        string Venv => Path.Combine(Runtime, "venv");
        string Python => Path.Combine(Venv, "Scripts", "python.exe");
        string ServerDir => Path.Combine(O.InstallDir, "app", "server");

        public Installer(InstallOptions o)
        {
            O = o;
            version = Payload.Version();
            http.DefaultRequestHeaders.UserAgent.ParseAdd("NextAI-Setup/" + version);
            Add("既存サービスの停止", StopService);
            Add("アプリケーションの展開", ExtractApp);
            Add("データフォルダの準備", PrepareData);
            Add("パッケージマネージャ (uv) の取得", GetUv);
            Add("Python 実行環境の構築", CreateVenv);
            Add("サーバーの依存パッケージのインストール", InstallDeps);
            Add("サーバー初期設定 (設定・DB・HTTPS証明書)", InitServer);
            if (!o.Upgrade) Add("管理者アカウントの作成", CreateAdmin);
            Add("推論ランタイムの取得 (llama.cpp / sd.cpp / サンドボックス)", InstallRuntimes);
            if (!o.SkipModels && o.Set != null) Add("AIモデルのダウンロード", InstallModels);
            Add("Windows サービスの登録と自動起動設定", RegisterService);
            Add("ファイアウォール設定", Firewall);
            Add("ショートカットと登録情報", Shortcuts);
            Add("AIサーバーの起動とヘルスチェック", StartAndCheck);
        }

        void Add(string title, Func<CancellationToken, Task> run)
        {
            Steps.Add(title);
            plan.Add((title, run));
        }

        void Info(string s) => Log?.Invoke(s);
        void Progress(double v, string detail = "") => StepProgress?.Invoke(v, detail);

        public async Task RunAsync(CancellationToken ct)
        {
            for (var i = 0; i < plan.Count; i++)
            {
                ct.ThrowIfCancellationRequested();
                StepState?.Invoke(i, "run");
                Info($"▶ {plan[i].title}");
                var warnBefore = Warnings.Count;
                try { await plan[i].run(ct); }
                catch (OperationCanceledException) { StepState?.Invoke(i, "fail"); throw; }
                catch (Exception ex)
                {
                    StepState?.Invoke(i, "fail");
                    Info("✗ " + ex.Message);
                    if (!(ex is StepFailed) || ex.Message.Contains("python.exe") || ex.Message.Contains("アクセス")) Diagnose();
                    throw new StepFailed($"{plan[i].title} に失敗しました:\n{ex.Message}");
                }
                StepState?.Invoke(i, Warnings.Count > warnBefore ? "warn" : "done");
            }
        }

        Dictionary<string, string> Env()
        {
            var env = new Dictionary<string, string>
            {
                ["PYTHONPATH"] = ServerDir,
                ["UV_PYTHON_INSTALL_DIR"] = Path.Combine(Runtime, "python"),
                ["UV_CACHE_DIR"] = Path.Combine(Runtime, "uv-cache"),
                ["UV_NO_PROGRESS"] = "1",
                ["NEXTAI_DATA_DIR"] = O.DataDir,
            };
            if (uvOk) env["NEXTAI_UV"] = Uv;
            return env;
        }

        static readonly int[] StartRetryDelays = { 2, 4, 8, 15, 30 };

        /// <summary>Starts a process, waiting out antivirus scans of freshly downloaded executables
        /// (CreateProcess returns "access denied" while Defender / other AV inspects a new file).</summary>
        async Task<ProcessResult> StartWithRetry(string exe, IEnumerable<string> args, CancellationToken ct, Action<string> onLine, string stdin)
        {
            for (var attempt = 0; ; attempt++)
            {
                try
                {
                    return await ProcessRunner.RunAsync(exe, args, onLine ?? (l => Info("  " + l)), Env(), stdin, O.DataDir, ct);
                }
                catch (ProcessStartException ex) when (ex.Transient && attempt < StartRetryDelays.Length && File.Exists(exe))
                {
                    var wait = StartRetryDelays[attempt];
                    Info($"  {Path.GetFileName(exe)} を起動できません (Win32 エラー {ex.Code})。セキュリティソフトの検査待ちの可能性があるため {wait} 秒後に再試行します…");
                    Progress(-1, $"{Path.GetFileName(exe)} の起動を待っています (セキュリティソフトの検査中の可能性)…");
                    await Task.Delay(TimeSpan.FromSeconds(wait), ct);
                }
                catch (ProcessStartException ex)
                {
                    Info($"  ✗ {ex.Message}");
                    var gone = !File.Exists(exe) ? "ファイルが見つかりません (セキュリティソフトに隔離された可能性があります)。" : ex.Hint;
                    throw new StartFailed($"{Path.GetFileName(exe)} を起動できませんでした (Win32 エラー {ex.Code}: {ex.InnerException?.Message})\n" +
                                         $"場所: {exe}\n{gone}\n" +
                                         "Windows セキュリティ →「ウイルスと脅威の防止」→「保護の履歴」でブロックされていないか確認し、" +
                                         "許可してから「再試行」してください。");
                }
            }
        }

        async Task<ProcessResult> Exec(string exe, IEnumerable<string> args, CancellationToken ct, Action<string> onLine = null, string stdin = null, bool check = true)
        {
            var res = await StartWithRetry(exe, args, ct, onLine, stdin);
            if (check && res.ExitCode != 0)
            {
                var tail = string.Join("\n", res.Output.Split('\n').Reverse().Take(12).Reverse());
                throw new StepFailed($"{Path.GetFileName(exe)} が終了コード {res.ExitCode} で失敗しました\n{tail}");
            }
            return res;
        }

        Task<ProcessResult> Nextai(CancellationToken ct, Action<string> onLine, string stdin, bool check, params string[] args)
        {
            var all = new List<string> { "-m", "nextai", "--data-dir", O.DataDir };
            all.AddRange(args);
            return Exec(Python, all, ct, onLine, stdin, check);
        }

        // ------------------------------------------------------------------ steps
        async Task StopService(CancellationToken ct)
        {
            if (WinService.Exists())
            {
                Progress(-1, "サービスを停止しています…");
                await WinService.StopAsync();
            }
            else Info("  既存サービスはありません");
            // A crashed service host / earlier setup can leave python.exe or llama-server.exe running and holding files.
            var killed = await Task.Run(() => LeftoverProcesses(kill: true).ToList(), ct);
            foreach (var k in killed) Info("  残っていたプロセスを終了しました: " + k);
        }

        Task ExtractApp(CancellationToken ct) => Task.Run(() =>
        {
            Directory.CreateDirectory(O.InstallDir);
            var oldServer = Path.Combine(O.InstallDir, "app", "server", "nextai");
            if (Directory.Exists(oldServer)) Directory.Delete(oldServer, true);
            Payload.ExtractTo(O.InstallDir, v => Progress(v, $"{v:P0}"));
            var self = System.Reflection.Assembly.GetExecutingAssembly().Location;
            var copy = Path.Combine(O.InstallDir, "NextAI-Setup.exe");
            if (!string.Equals(Path.GetFullPath(self), Path.GetFullPath(copy), StringComparison.OrdinalIgnoreCase)) File.Copy(self, copy, true);
            Info($"  {O.InstallDir} に展開しました (v{version})");
        }, ct);

        Task PrepareData(CancellationToken ct) => Task.Run(() =>
        {
            foreach (var d in new[] { "", "runtime", "models", "logs", "users", "certs", "backups", "tmp", "run", "diagnostics" })
                Directory.CreateDirectory(Path.Combine(O.DataDir, d));
            var drive = new DriveInfo(Path.GetPathRoot(Path.GetFullPath(O.DataDir)));
            Info($"  データフォルダ: {O.DataDir} (空き {drive.AvailableFreeSpace / 1073741824.0:F1} GB)");
            RepairDataAccess();
        }, ct);

        /// <summary>Files left by an earlier install (created by the service account, restricted ACLs, odd owners)
        /// must stay readable by setup: re-grant SYSTEM / Administrators full control on the whole data folder.
        /// The service's own grant is re-applied at service registration.</summary>
        void RepairDataAccess()
        {
            // Root: make sure SYSTEM / Administrators have full control (inheritable).
            var root = Shell.Capture("icacls.exe", O.DataDir, "/grant", "*S-1-5-18:(OI)(CI)F", "/grant", "*S-1-5-32-544:(OI)(CI)F", "/Q");
            // Children: drop whatever explicit entries an earlier install / the service left (including deny entries)
            // and inherit from the root. The service account's grant is re-applied at service registration.
            string[] Reset() => new[] { Path.Combine(O.DataDir, "*"), "/reset", "/T", "/C", "/Q" };
            var r = Shell.Capture("icacls.exe", Reset());
            if (root.code != 0 || r.code != 0)
            {
                Info("  データフォルダのアクセス権を修復しています (所有者を Administrators に変更)…");
                Shell.Capture("takeown.exe", "/F", O.DataDir, "/R", "/A");
                Shell.Capture("icacls.exe", O.DataDir, "/grant", "*S-1-5-18:(OI)(CI)F", "/grant", "*S-1-5-32-544:(OI)(CI)F", "/Q");
                r = Shell.Capture("icacls.exe", Reset());
                if (r.code != 0) Info("  icacls: " + Tail(r.output, 6));
            }
            var bad = new List<string>();
            foreach (var name in new[] { "config.toml", "nextai.db", "nextai.db-wal", "nextai.db-shm" })
            {
                var p = Path.Combine(O.DataDir, name);
                if (!File.Exists(p)) continue;
                try { File.SetAttributes(p, FileAttributes.Normal); } catch { }
                try { using (File.Open(p, FileMode.Open, FileAccess.ReadWrite, FileShare.ReadWrite | FileShare.Delete)) { } }
                catch (Exception ex) { bad.Add(p); Info($"  ⚠ {p} を開けません: {ex.Message}"); }
            }
            try
            {
                var probe = Path.Combine(O.DataDir, "tmp", "setup-probe.tmp");
                File.WriteAllText(probe, "ok");
                File.Delete(probe);
            }
            catch (Exception ex) { bad.Add(O.DataDir); Info($"  ⚠ データフォルダに書き込めません: {ex.Message}"); }
            if (bad.Count > 0) Diagnose(bad[0]);
        }

        static string Tail(string s, int lines) => string.Join("\n", (s ?? "").Split('\n').Reverse().Take(lines).Reverse()).Trim();

        /// <summary>Writes what we can learn about an access problem to the log (ACL, owner, holders, security software).</summary>
        public void Diagnose(string path = null)
        {
            try
            {
                Info("---- 診断情報 ----");
                var id = System.Security.Principal.WindowsIdentity.GetCurrent();
                var admin = new System.Security.Principal.WindowsPrincipal(id).IsInRole(System.Security.Principal.WindowsBuiltInRole.Administrator);
                Info($"  実行ユーザー: {id.Name} / 管理者として実行: {admin}");
                foreach (var p in new[] { O.DataDir, path ?? Path.Combine(O.DataDir, "config.toml") }.Distinct())
                    if (File.Exists(p) || Directory.Exists(p))
                    {
                        Info("  icacls " + p);
                        Info("    " + Tail(Shell.Capture("icacls.exe", p).output, 8).Replace("\n", "\n    "));
                        try { Info($"    属性: {File.GetAttributes(p)}"); } catch (Exception ex) { Info("    属性: " + ex.Message); }
                    }
                var av = Shell.Capture("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct | ForEach-Object { $_.displayName }");
                Info("  セキュリティソフト: " + (string.IsNullOrWhiteSpace(av.output) ? "(取得できません)" : av.output.Replace("\r\n", ", ").Replace("\n", ", ")));
                var cfa = Shell.Capture("powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                    "(Get-MpPreference).EnableControlledFolderAccess");
                Info("  コントロールされたフォルダーアクセス: " + (cfa.output.Trim() == "1" ? "有効" : cfa.output.Trim() == "0" ? "無効" : cfa.output.Trim()));
                foreach (var name in LeftoverProcesses()) Info("  実行中の関連プロセス: " + name);
                Info("------------------");
            }
            catch (Exception ex) { Info("  診断情報の取得に失敗: " + ex.Message); }
        }

        /// <summary>Processes started from our install / data folders (an old server, llama-server, ...) other than setup itself.</summary>
        IEnumerable<string> LeftoverProcesses(bool kill = false)
        {
            var roots = new[] { Path.GetFullPath(O.InstallDir), Path.GetFullPath(O.DataDir) };
            var self = System.Diagnostics.Process.GetCurrentProcess().Id;
            var found = new List<string>();
            foreach (var p in System.Diagnostics.Process.GetProcesses())
            {
                try
                {
                    if (p.Id == self) continue;
                    var exe = p.MainModule?.FileName;
                    if (exe == null || !roots.Any(r => exe.StartsWith(r + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))) continue;
                    if (Path.GetFileName(exe).StartsWith("NextAI-Setup", StringComparison.OrdinalIgnoreCase)) continue;
                    found.Add($"{Path.GetFileName(exe)} (PID {p.Id})");
                    if (kill) { p.Kill(); p.WaitForExit(10000); }
                }
                catch { }
                finally { p.Dispose(); }
            }
            return found;
        }

        // Official CPython for Windows from nuget.org (published by the Python team, Authenticode-signed).
        // Used when security software blocks uv.exe. Pinned + SHA-256 verified.
        const string NugetPythonVersion = "3.12.10";
        const string NugetPythonSha256 = "0eb85c2dfccccf1b17352de4c397f69194035b7d37149eacc16f1147d93de3b8";
        string NugetPython => Path.Combine(Runtime, "python-nuget", "tools", "python.exe");

        async Task GetUv(CancellationToken ct)
        {
            try
            {
                if (!(File.Exists(Uv) && new FileInfo(Uv).Length > 1_000_000))
                {
                    Progress(-1, "PyPI から uv の情報を取得中…");
                    var meta = Json.ParseObject(await http.GetStringAsync("https://pypi.org/pypi/uv/json"));
                    var wheel = meta.Arr("urls").Objects().FirstOrDefault(u => u.Str("filename").EndsWith("-py3-none-win_amd64.whl"))
                                ?? throw new StepFailed("uv の Windows 版が見つかりません");
                    var dl = await DownloadVerified(wheel.Str("url"), wheel.Str("filename"), wheel.Obj("digests").Str("sha256"), ct);
                    using (var z = ZipFile.OpenRead(dl))
                    {
                        var exe = z.Entries.FirstOrDefault(e => e.FullName.EndsWith("/scripts/uv.exe", StringComparison.OrdinalIgnoreCase))
                                  ?? throw new StepFailed("uv.exe がアーカイブ内にありません");
                        Directory.CreateDirectory(Path.GetDirectoryName(Uv));
                        exe.ExtractToFile(Uv, true);
                    }
                    Info($"  uv {meta.Obj("info").Str("version")} を取得しました (SHA256検証済み)");
                }
                else Info("  uv は取得済みです");
                await StartWithRetry(Uv, new[] { "--version" }, ct, null, null);
            }
            catch (Exception ex) when (!(ex is OperationCanceledException))
            {
                UseUv(false, ex.Message);
            }
        }

        void UseUv(bool ok, string why = null)
        {
            if (!ok && uvOk)
            {
                Info("  uv を利用できません: " + why);
                Info("  → Python 公式パッケージ (nuget.org) と pip で環境を構築します");
                Warnings.Add("uv がブロックされたため、Python 公式パッケージ + pip で構築しました (セキュリティソフトの影響の可能性)");
            }
            uvOk = ok;
        }

        /// <summary>Download to runtime\downloads; if that file is locked / access-denied (security software),
        /// use a fresh file name instead of failing.</summary>
        async Task<string> DownloadVerified(string url, string name, string sha256, CancellationToken ct)
        {
            Action<long, long> prog = (done, total) => Progress(total > 0 ? done / (double)total : -1, $"{Ui.Bytes(done)} / {Ui.Bytes(total)}");
            var dl = Path.Combine(Runtime, "downloads", name);
            try
            {
                await SimpleDownloader.DownloadAsync(http, url, dl, sha256, prog, ct);
                return dl;
            }
            catch (Exception ex) when (ex is UnauthorizedAccessException || ex is IOException)
            {
                Info($"  {name} にアクセスできないため別名で取得します ({ex.Message})");
                var alt = Path.Combine(Runtime, "downloads", Guid.NewGuid().ToString("N").Substring(0, 8) + "-" + name);
                await SimpleDownloader.DownloadAsync(http, url, alt, sha256, prog, ct);
                return alt;
            }
        }

        async Task CreateVenv(CancellationToken ct)
        {
            Progress(-1, "Python 3.12 を準備しています (初回は数分かかります)…");
            if (uvOk)
            {
                try
                {
                    await Exec(Uv, new[] { "venv", "--python", "3.12", "--python-preference", "only-managed", "--allow-existing", Venv }, ct);
                    return;
                }
                catch (StepFailed ex) when (!ct.IsCancellationRequested)
                {
                    UseUv(false, ex.Message);
                }
            }
            if (await Works(Python, ct)) { Info("  既存の Python 環境を利用します"); return; }
            if (!await Works(NugetPython, ct))
            {
                Progress(-1, $"Python {NugetPythonVersion} (公式パッケージ) を取得しています…");
                var pkg = await DownloadVerified($"https://api.nuget.org/v3-flatcontainer/python/{NugetPythonVersion}/python.{NugetPythonVersion}.nupkg",
                                                 $"python.{NugetPythonVersion}.nupkg", NugetPythonSha256, ct);
                var root = Path.Combine(Runtime, "python-nuget");
                await Task.Run(() =>
                {
                    if (Directory.Exists(root)) Directory.Delete(root, true);
                    using (var z = ZipFile.OpenRead(pkg))
                        foreach (var e in z.Entries.Where(e => e.FullName.StartsWith("tools/", StringComparison.Ordinal) && e.Name.Length > 0))
                        {
                            var path = Path.GetFullPath(Path.Combine(root, e.FullName.Replace('/', Path.DirectorySeparatorChar)));
                            if (!path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) continue;
                            Directory.CreateDirectory(Path.GetDirectoryName(path));
                            e.ExtractToFile(path, true);
                        }
                }, ct);
                Info($"  Python {NugetPythonVersion} を展開しました (SHA256検証済み)");
            }
            if (Directory.Exists(Venv)) await Task.Run(() => Directory.Delete(Venv, true), ct);
            await Exec(NugetPython, new[] { "-m", "venv", Venv }, ct);
        }

        async Task<bool> Works(string python, CancellationToken ct)
        {
            if (!File.Exists(python)) return false;
            try { return (await ProcessRunner.RunAsync(python, new[] { "-c", "import sys; assert sys.version_info[:2] == (3, 12)" }, null, Env(), null, O.DataDir, ct)).ExitCode == 0; }
            catch (ProcessStartException) { return false; }
        }

        async Task InstallDeps(CancellationToken ct)
        {
            Progress(-1, "依存パッケージをインストールしています…");
            var lockFile = Path.Combine(ServerDir, "requirements.lock");
            if (uvOk)
            {
                try
                {
                    await Exec(Uv, new[] { "pip", "install", "--python", Python, "--require-hashes", "-r", lockFile }, ct);
                    return;
                }
                catch (StartFailed ex) when (!ct.IsCancellationRequested)
                {
                    UseUv(false, ex.Message);
                }
            }
            var hasPip = (await Exec(Python, new[] { "-m", "pip", "--version" }, ct, l => { }, null, false)).ExitCode == 0;
            if (!hasPip) await Exec(Python, new[] { "-m", "ensurepip", "--default-pip" }, ct);
            await Exec(Python, new[] { "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--only-binary=:all:",
                                       "--require-hashes", "-r", lockFile }, ct);
        }

        async Task InitServer(CancellationToken ct)
        {
            await Nextai(ct, null, null, true, "init", "--port", O.Port.ToString(), O.Lan ? "--lan" : "--no-lan", "--name", O.ServerName, "--json");
            await Nextai(ct, null, null, true, "migrate");
        }

        async Task CreateAdmin(CancellationToken ct)
        {
            var r = await Nextai(ct, null, O.AdminPassword, false, "create-admin", "--username", O.AdminUser, "--display-name", "管理者", "--password-stdin");
            if (r.ExitCode == 2) { Info("  管理者は既に存在します (既存データを保持)"); return; }
            if (r.ExitCode != 0) throw new StepFailed("管理者の作成に失敗しました: " + r.Output.Trim());
        }

        void JsonProgress(string line, Func<JObject, bool> handler)
        {
            var t = line.Trim();
            if (!t.StartsWith("{")) { if (t != "") Info("  " + t); return; }
            try
            {
                var ev = Json.ParseObject(t);
                if (!handler(ev)) Info("  " + t);
            }
            catch (FormatException) { Info("  " + t); }
        }

        async Task InstallRuntimes(CancellationToken ct)
        {
            var comps = (O.Set?.Runtimes ?? new List<string> { "llama.cpp", "python-wasm" }).ToList();
            if (!comps.Contains("llama.cpp")) comps.Insert(0, "llama.cpp");
            var args = new List<string> { "runtime", "install", "--json-progress", "--components" };
            args.AddRange(comps);
            var r = await Nextai(ct, line => JsonProgress(line, ev =>
            {
                switch (ev.Str("event"))
                {
                    case "progress":
                        var size = ev.Num("size");
                        Progress(size > 0 ? ev.Num("done") / size : -1, $"{ev.Str("component")}: {ev.Str("file")}  {Ui.Bytes(ev.Num("done"))} / {Ui.Bytes(size)}");
                        return true;
                    case "component": Info($"  {ev.Str("component")} {ev.Str("version")} ({ev.Str("accel")})"); return true;
                    case "component_skip": Info($"  {ev.Str("component")} は導入済み ({ev.Str("version")})"); return true;
                    case "component_error":
                        Warnings.Add($"{ev.Str("component")}: {ev.Str("error")}");
                        Info($"  ⚠ {ev.Str("component")}: {ev.Str("error")}");
                        return true;
                    case "notice": Info("  " + ev.Str("message")); return true;
                    default: return false;
                }
            }), null, false, args.ToArray());
            if (r.ExitCode != 0) throw new StepFailed("推論ランタイム (llama.cpp) を取得できませんでした。ネットワークを確認して再試行してください。");
        }

        async Task InstallModels(CancellationToken ct)
        {
            double total = 0;
            var failed = new List<string>();
            var ok = new List<string>();
            var r = await Nextai(ct, line => JsonProgress(line, ev =>
            {
                switch (ev.Str("event"))
                {
                    case "resolve": Progress(-1, $"{ev.Str("model")} の取得元を確認中…"); return true;
                    case "plan":
                        total = ev.Num("total_bytes");
                        Info($"  必要容量 {Ui.Bytes(total)} (残り {Ui.Bytes(ev.Num("remaining_bytes"))}) / 空き容量 {Ui.Bytes(ev.Num("free_bytes"))}");
                        return true;
                    case "progress":
                        var all = ev.Num("overall_total");
                        Progress(all > 0 ? ev.Num("overall_done") / all : -1,
                            $"{ev.Str("model")} / {ev.Str("file")}   取得済み {Ui.Bytes(ev.Num("overall_done"))} / {Ui.Bytes(all)}");
                        return true;
                    case "model_done": ok.Add(ev.Str("model")); Info($"  ✓ {ev.Str("model")} ({Ui.Bytes(ev.Num("bytes"))}, 検証済み)"); return true;
                    case "model_error": failed.Add(ev.Str("model")); Info($"  ⚠ {ev.Str("model")}: {ev.Str("error")}"); return true;
                    case "fatal": Info("  ✗ " + ev.Str("error")); failed.Add("*"); return true;
                    case "summary": return true;
                    default: return false;
                }
            }), null, false, "models", "install", "--set", O.Set.Id, "--json-progress");
            if (ok.Count == 0 && (r.ExitCode != 0 || failed.Count > 0))
                throw new StepFailed("モデルをダウンロードできませんでした (容量・ネットワークを確認してください)。再試行すると続きから再開します。");
            if (failed.Count > 0) Warnings.Add("一部のモデルを取得できませんでした: " + string.Join(", ", failed) + " (管理コンソールのモデル画面から再取得できます)");
        }

        async Task RegisterService(CancellationToken ct)
        {
            var host = Path.Combine(O.InstallDir, "NextAI.ServiceHost.exe");
            var info = new InstallInfo { Version = version, InstallDir = O.InstallDir, DataDir = O.DataDir, Python = Python, ServerDir = ServerDir, Port = O.Port, ModelSet = O.Set?.Id ?? "" };
            info.Save();
            await Task.Run(() =>
            {
                var bin = $"\"{host}\"";
                if (!WinService.Exists())
                    Check(Shell.Run("sc.exe", "create", InstallInfo.ServiceName, "binPath=", bin, "start=", "delayed-auto", "DisplayName=", "NextAI Platform Server"), "sc create");
                else
                    Shell.Run("sc.exe", "config", InstallInfo.ServiceName, "binPath=", bin, "start=", "delayed-auto");
                Shell.Run("sc.exe", "description", InstallInfo.ServiceName, "NextAI Platform AI server (multi-user local AI). Managed by NextAI 管理コンソール.");
                Shell.Run("sc.exe", "failure", InstallInfo.ServiceName, "reset=", "86400", "actions=", "restart/5000/restart/15000/restart/60000");
                Shell.Run("sc.exe", "sidtype", InstallInfo.ServiceName, "unrestricted");
                var account = $"NT SERVICE\\{InstallInfo.ServiceName}";
                if (Shell.Run("sc.exe", "config", InstallInfo.ServiceName, "obj=", account) != 0)
                {
                    Warnings.Add("仮想サービスアカウントを設定できなかったため LocalSystem で実行します");
                    account = null;
                }
                // Data dir: SYSTEM + Administrators full, service account modify, no access for other users.
                // Set the protected ACL on the root only, then make every child inherit it. (Applying
                // "/inheritance:r /grant:r ...(OI)(CI)" with /T to files leaves them with an EMPTY DACL:
                // files can't take (OI)(CI) grants, so nobody - not even the service or setup - could open them.)
                var grants = new List<string> { O.DataDir, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "/grant:r", "*S-1-5-32-544:(OI)(CI)F" };
                if (account != null) { grants.Add("/grant:r"); grants.Add(account + ":(OI)(CI)M"); }
                grants.Add("/Q");
                var ok = Shell.Run("icacls.exe", grants.ToArray()) == 0;
                ok &= Shell.Run("icacls.exe", Path.Combine(O.DataDir, "*"), "/reset", "/T", "/C", "/Q") == 0;
                if (!ok) Warnings.Add("データフォルダのアクセス権設定に一部失敗しました");
                Info($"  サービス {InstallInfo.ServiceName} を登録しました (自動起動 / 異常終了時は自動再起動 / 実行アカウント: {account ?? "LocalSystem"})");
            }, ct);
        }

        static void Check(int code, string what)
        {
            if (code != 0) throw new StepFailed($"{what} が失敗しました (コード {code})");
        }

        Task Firewall(CancellationToken ct) => Task.Run(() =>
        {
            Shell.Run("netsh.exe", "advfirewall", "firewall", "delete", "rule", "name=NextAI Platform");
            if (!O.Lan) { Info("  LAN公開なし: ファイアウォール規則は作成しません"); return; }
            var code = Shell.Run("netsh.exe", "advfirewall", "firewall", "add", "rule", "name=NextAI Platform", "dir=in", "action=allow",
                "protocol=TCP", $"localport={O.Port}", "profile=private,domain", "description=NextAI Platform (HTTPS)");
            if (code != 0) Warnings.Add("ファイアウォール規則を追加できませんでした");
            else Info($"  TCP {O.Port} をプライベート/ドメインネットワークに許可しました (パブリックは遮断)");
        }, ct);

        Task Shortcuts(CancellationToken ct) => Task.Run(() =>
        {
            var admin = Path.Combine(O.InstallDir, "NextAI.Admin.exe");
            var ca = Path.Combine(O.DataDir, "certs", "ca.crt");
            if (File.Exists(ca)) File.Copy(ca, Path.Combine(O.InstallDir, "ca.crt"), true);
            var menu = Shell.StartMenuDir;
            Shell.CreateShortcut(Path.Combine(menu, "NextAI 管理コンソール.lnk"), admin, "", "NextAI Platform サーバー管理", admin + ",0");
            Shell.CreateShortcut(Path.Combine(menu, "NextAI 診断.lnk"), admin, "--diagnostics", "NextAI Platform システム診断", admin + ",0");
            Shell.CreateUrlShortcut(Path.Combine(menu, "NextAI (ブラウザで開く).url"), $"https://localhost:{O.Port}/", admin);
            if (O.DesktopShortcut) Shell.CreateShortcut(Shell.DesktopLink, admin, "", "NextAI Platform サーバー管理", admin + ",0");
            Shell.AdminAutostart(admin, O.AutostartAdmin);
            long kb = 0;
            try { kb = Directory.EnumerateFiles(O.InstallDir, "*", SearchOption.AllDirectories).Sum(f => new FileInfo(f).Length) / 1024; } catch { }
            Shell.WriteRegistry(new InstallInfo { InstallDir = O.InstallDir, DataDir = O.DataDir, Port = O.Port, ModelSet = O.Set?.Id ?? "" }, version, kb);
            Info("  スタートメニュー / アンインストール情報を登録しました");
        }, ct);

        async Task StartAndCheck(CancellationToken ct)
        {
            Progress(-1, "サービスを起動しています…");
            await WinService.StartAsync();
            var info = InstallInfo.Load(Path.Combine(O.InstallDir, "install.json"));
            using (var api = new ApiClient(info.BaseUrl, Path.Combine(O.InstallDir, "ca.crt")))
            {
                for (var i = 0; i < 180; i++)
                {
                    ct.ThrowIfCancellationRequested();
                    if (await api.HealthAsync()) break;
                    if (i == 179) throw new StepFailed("サーバーが応答しません。管理コンソールのログ (service.log) を確認してください。");
                    Progress(i / 180.0, $"ヘルスチェック待機中… ({i}s)");
                    await Task.Delay(1000, ct);
                }
            }
            Info("  ✓ ヘルスチェック OK");
            var r = await Nextai(ct, l => { }, null, false, "urls");
            try
            {
                var o = Json.ParseObject(r.Output.Trim().Split('\n').Last());
                Urls = o.Arr("urls").Objects().Where(u => u.Str("kind") != "local").Select(u => $"{u.Str("url")}  ({u.Str("label")})").ToList();
            }
            catch (FormatException) { }
        }
    }
}
