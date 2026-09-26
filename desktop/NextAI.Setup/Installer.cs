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
        bool reacquiredUv;
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
                    throw new StepFailed($"{plan[i].title} に失敗しました:\n{ex.Message}");
                }
                StepState?.Invoke(i, Warnings.Count > warnBefore ? "warn" : "done");
            }
        }

        Dictionary<string, string> Env() => new Dictionary<string, string>
        {
            ["PYTHONPATH"] = ServerDir,
            ["UV_PYTHON_INSTALL_DIR"] = Path.Combine(Runtime, "python"),
            ["UV_CACHE_DIR"] = Path.Combine(Runtime, "uv-cache"),
            ["UV_NO_PROGRESS"] = "1",
            ["NEXTAI_UV"] = Uv,
            ["NEXTAI_DATA_DIR"] = O.DataDir,
        };

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
            if (!WinService.Exists()) { Info("  既存サービスはありません"); return; }
            Progress(-1, "サービスを停止しています…");
            await WinService.StopAsync();
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
        }, ct);

        async Task GetUv(CancellationToken ct)
        {
            if (File.Exists(Uv) && new FileInfo(Uv).Length > 1_000_000) { Info("  uv は取得済みです"); return; }
            Progress(-1, "PyPI から uv の情報を取得中…");
            var meta = Json.ParseObject(await http.GetStringAsync("https://pypi.org/pypi/uv/json"));
            var wheel = meta.Arr("urls").Objects().FirstOrDefault(u => u.Str("filename").EndsWith("-py3-none-win_amd64.whl"))
                        ?? throw new StepFailed("uv の Windows 版が見つかりません");
            var dl = Path.Combine(Runtime, "downloads", wheel.Str("filename"));
            await SimpleDownloader.DownloadAsync(http, wheel.Str("url"), dl, wheel.Obj("digests").Str("sha256"),
                (done, total) => Progress(total > 0 ? done / (double)total : -1, $"{Ui.Bytes(done)} / {Ui.Bytes(total)}"), ct);
            using (var z = ZipFile.OpenRead(dl))
            {
                var exe = z.Entries.FirstOrDefault(e => e.FullName.EndsWith("/scripts/uv.exe", StringComparison.OrdinalIgnoreCase))
                          ?? throw new StepFailed("uv.exe がアーカイブ内にありません");
                Directory.CreateDirectory(Path.GetDirectoryName(Uv));
                exe.ExtractToFile(Uv, true);
            }
            Info($"  uv {meta.Obj("info").Str("version")} を取得しました (SHA256検証済み)");
        }

        async Task CreateVenv(CancellationToken ct)
        {
            Progress(-1, "Python 3.12 を準備しています (初回は数分かかります)…");
            var uvArgs = new[] { "venv", "--python", "3.12", "--python-preference", "only-managed", "--allow-existing", Venv };
            try
            {
                await Exec(Uv, uvArgs, ct);
            }
            catch (StartFailed) when (!ct.IsCancellationRequested && !reacquiredUv)
            {
                // A damaged / half-quarantined uv.exe from an earlier run: fetch it again once, then retry.
                reacquiredUv = true;
                Info("  uv を取得し直して再試行します…");
                try { if (File.Exists(Uv)) File.Delete(Uv); }
                catch (Exception ex) { throw new StepFailed($"古い uv.exe を削除できません ({Uv}): {ex.Message}\nセキュリティソフトがファイルをロックしている可能性があります。"); }
                await GetUv(ct);
                await Exec(Uv, uvArgs, ct);
            }
        }

        async Task InstallDeps(CancellationToken ct)
        {
            Progress(-1, "依存パッケージをインストールしています…");
            await Exec(Uv, new[] { "pip", "install", "--python", Python, "--require-hashes", "-r", Path.Combine(ServerDir, "requirements.lock") }, ct);
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
                var grants = new List<string> { O.DataDir, "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "/grant:r", "*S-1-5-32-544:(OI)(CI)F" };
                if (account != null) { grants.Add("/grant:r"); grants.Add(account + ":(OI)(CI)M"); }
                grants.Add("/T"); grants.Add("/C"); grants.Add("/Q");
                if (Shell.Run("icacls.exe", grants.ToArray()) != 0) Warnings.Add("データフォルダのアクセス権設定に一部失敗しました");
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
