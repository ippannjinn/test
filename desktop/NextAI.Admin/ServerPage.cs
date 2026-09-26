using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;
using NextAI.Common;

namespace NextAI.Admin
{
    sealed class ServerPage : AdminPage
    {
        readonly Label svcStatus, health, diagSummary, remoteStatus;
        readonly LinkLabel remoteUrl;
        readonly Button remoteInstall, remoteLogin, remoteOn, remoteOff, remoteCopy;
        int tunnelPort = 8444;
        string publicUrl = "";
        readonly DataGridView diag, backups;
        readonly TextBox info;

        public ServerPage(MainForm main) : base(main, "サーバー")
        {
            svcStatus = Ui.Label("サービス: -", Ui.BoldFont);
            health = Ui.Label("", null, Ui.Muted);
            var svcBar = Ui.Toolbar(svcStatus,
                Ui.Btn("開始", (s, e) => Run(async () => { await WinService.StartAsync(); await WaitHealthy(); })),
                Ui.Btn("停止", (s, e) => { if (Ui.Confirm(this, "AIサーバーを停止します。実行中のジョブは中断されます。")) Run(() => WinService.StopAsync(), false); }),
                Ui.Btn("再起動", (s, e) => Run(async () => { await Api.PostAsync("/api/admin/server/restart"); await Task.Delay(3000); await WaitHealthy(); })),
                Ui.Btn("ヘルスチェック", (s, e) => Run(() => Task.CompletedTask)),
                health);

            diagSummary = Ui.Label("", Ui.BoldFont);
            diag = Ui.Grid(("status", "結果", 60), ("name", "項目", 190), ("value", "値", 260), ("detail", "詳細 / 対処", 0));
            var diagPanel = new Panel { Dock = DockStyle.Fill };
            diagPanel.Controls.Add(diag);
            diagPanel.Controls.Add(Ui.Toolbar(
                Ui.Btn("クイック診断", (s, e) => RunDiagnostics(false), true),
                Ui.Btn("フル診断 (実機ベンチマーク)", (s, e) => RunDiagnostics(true)),
                diagSummary));

            backups = Ui.Grid(("name", "バックアップ", 0), ("size", "サイズ", 90), ("time", "作成日時", 120));
            var bpanel = new Panel { Dock = DockStyle.Fill };
            bpanel.Controls.Add(backups);
            bpanel.Controls.Add(Ui.Toolbar(
                Ui.Btn("バックアップ作成", (s, e) => Backup(true)),
                Ui.Btn("作成 (ユーザーファイル除く)", (s, e) => Backup(false)),
                Ui.Btn("復元…", (s, e) => Restore()),
                Ui.Btn("フォルダを開く", (s, e) => OpenFolder(Path.Combine(Main.Info.DataDir, "backups")))));

            info = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, BackColor = Color.White, Font = Ui.MonoFont, Dock = DockStyle.Fill, TabStop = false };
            var ipanel = new Panel { Dock = DockStyle.Fill };
            ipanel.Controls.Add(info);
            ipanel.Controls.Add(Ui.Toolbar(
                Ui.Btn("ブラウザで開く", (s, e) => Process.Start(new ProcessStartInfo($"https://localhost:{Main.Info.Port}/") { UseShellExecute = true })),
                Ui.Btn("データフォルダ", (s, e) => OpenFolder(Main.Info.DataDir)),
                Ui.Btn("アップデート確認 / 更新", (s, e) => CheckUpdate()),
                Ui.Btn("ディスククリーンアップ", (s, e) => Run(async () => { var r = await Api.PostAsync("/api/admin/cleanup"); MessageBox.Show(this, Json.Serialize(r.Obj("removed")), "クリーンアップ"); }, false))));

            var grid = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, RowCount = 2 };
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 58));
            grid.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 42));
            grid.RowStyles.Add(new RowStyle(SizeType.Percent, 60));
            grid.RowStyles.Add(new RowStyle(SizeType.Percent, 40));
            grid.Controls.Add(Group("システム診断 (PASS / WARN / FAIL)", diagPanel), 0, 0);
            grid.SetRowSpan(grid.GetControlFromPosition(0, 0), 2);
            grid.Controls.Add(Group("サーバー情報", ipanel), 1, 0);
            grid.Controls.Add(Group("バックアップ / 復元", bpanel), 1, 1);
            remoteStatus = Ui.Label("確認中…", Ui.BoldFont);
            remoteUrl = new LinkLabel { AutoSize = true, Margin = new Padding(12, 8, 3, 3), Font = Ui.BoldFont };
            remoteUrl.LinkClicked += (s, e) => { if (remoteUrl.Text.StartsWith("https://")) Process.Start(new ProcessStartInfo(remoteUrl.Text) { UseShellExecute = true }); };
            remoteInstall = Ui.Btn("Tailscale を入手", (s, e) => Process.Start(new ProcessStartInfo(RemoteAccess.DownloadUrl) { UseShellExecute = true }));
            remoteLogin = Ui.Btn("Tailscale にログイン", (s, e) => Run(() => RemoteAccess.LoginAsync(null), false));
            remoteOn = Ui.Btn("外部公開を開始", (s, e) => EnableRemote(), true);
            remoteOff = Ui.Btn("外部公開を停止", (s, e) => DisableRemote());
            remoteCopy = Ui.Btn("URLをコピー", (s, e) => { if (publicUrl != "") { Clipboard.SetText(publicUrl); remoteStatus.Text = "コピーしました"; } });
            var remoteBar = Ui.Toolbar(remoteStatus, remoteInstall, remoteLogin, remoteOn, remoteOff, remoteCopy, remoteUrl);

            Controls.Add(grid);
            Controls.Add(Group("外部公開 (インターネット上の友だちに使ってもらう / Tailscale Funnel)", remoteBar, DockStyle.Top, 76));
            Controls.Add(Group("サービス (Windows起動時に自動起動・異常終了時は自動復旧)", svcBar, DockStyle.Top, 70));
        }

        async Task WaitHealthy()
        {
            for (var i = 0; i < 60; i++)
            {
                if (await Api.HealthAsync()) return;
                await Task.Delay(1000);
            }
            throw new ApiException(0, "timeout", "サーバーが起動しませんでした。ログタブの service.log を確認してください。");
        }

        public override async Task RefreshAsync()
        {
            var st = WinService.Status();
            svcStatus.Text = "サービス: " + MainForm.ServiceText(st);
            svcStatus.ForeColor = st == "Running" ? Ui.Ok : Ui.Bad;
            var ok = await Api.HealthAsync();
            health.Text = (ok ? "ヘルスチェック: OK" : "ヘルスチェック: 応答なし") + $"  ({DateTime.Now:HH:mm:ss})";
            health.ForeColor = ok ? Ui.Ok : Ui.Bad;
            await RefreshRemote();
            if (!ok)
            {
                info.Text = st == "Running"
                    ? "サーバーが応答しません。起動中の場合は少し待ってください。\r\n続く場合は「ログ / 監査」タブの service.log / server.log を確認してください。"
                    : "サービスが停止しています。「開始」を押すと起動します。\r\n(異常終了した場合は「ログ / 監査」タブの service.log を確認してください)";
                if (!diagSummary.Text.StartsWith("(前回)")) diagSummary.Text = "(前回) " + diagSummary.Text;
                return;
            }
            var i = await Api.GetAsync("/api/admin/server/info");
            if (publicUrl != "" && i.Str("public_url").TrimEnd('/') != publicUrl.TrimEnd('/'))
            {
                // Funnel is on (e.g. enabled from the tray app): keep invitations pointing at it.
                await SetPublicUrl(publicUrl);
                i = await Api.GetAsync("/api/admin/server/info");
            }
            var urls = string.Join("\r\n", i.Arr("urls").Objects().Select(u => $"  {u.Str("url")}  ({u.Str("label")})"));
            info.Text = $"バージョン: {i.Str("version")}\r\nPython: {i.Str("python")}\r\nOS: {i.Str("platform")}\r\nデータ: {i.Str("data_dir")}\r\n"
                        + $"稼働時間: {Ui.Duration(i.Num("uptime_seconds"))}\r\nバックエンド: {i.Str("backend_mode")} / GPU: {i.Str("gpu_provider")} / Sandbox: {i.Str("sandbox")}\r\n"
                        + $"CA証明書 SHA-256:\r\n  {i.Str("ca_fingerprint")}\r\n\r\nメンバー用URL:\r\n{urls}";
            var b = await Api.GetAsync("/api/admin/backups");
            Ui.Fill(backups, b.Arr("backups").Objects(), x => new object[] { x.Str("name"), Ui.Bytes(x.Num("size")), Ui.Time(x.Num("created_at")) });
            try
            {
                var latest = await Api.GetAsync("/api/admin/diagnostics/latest");
                ShowReport(latest);
            }
            catch (ApiException) { }
        }

        void ShowReport(JObject report)
        {
            var c = report.Obj("counts");
            diagSummary.Text = $"総合: {report.Str("overall")}   PASS {c.Int("PASS")} / WARN {c.Int("WARN")} / FAIL {c.Int("FAIL")}   ({Ui.Time(report.Num("ts"))}{(report.Bool("full") ? ", フル" : "")})";
            diagSummary.ForeColor = Ui.StatusColor(report.Str("overall"));
            Ui.Fill(diag, report.Arr("checks").Objects(), x => new object[]
            {
                x.Str("status"), x.Str("name"), x.Str("value"), string.Join(" ", new[] { x.Str("detail"), x.Str("advice") }.Where(s => s != "")),
            }, (row, x) => { row.Cells["status"].Style.ForeColor = Ui.StatusColor(x.Str("status")); row.Cells["status"].Style.Font = Ui.BoldFont; });
        }

        async Task RefreshRemote()
        {
            RemoteAccess.State r;
            try { r = await RemoteAccess.StatusAsync(); }
            catch (Exception ex) { remoteStatus.Text = "状態を取得できません: " + ex.Message; return; }
            remoteInstall.Visible = !r.Installed;
            remoteLogin.Visible = r.Installed && !r.LoggedIn;
            remoteOn.Visible = r.LoggedIn && !r.FunnelOn;
            remoteOff.Visible = remoteCopy.Visible = r.FunnelOn;
            if (!r.Installed) { remoteStatus.Text = "Tailscale が未インストールです (無料・このPCにだけ必要)"; remoteStatus.ForeColor = Ui.Muted; }
            else if (!r.LoggedIn) { remoteStatus.Text = $"Tailscale にログインしてください ({r.Backend})"; remoteStatus.ForeColor = Ui.Muted; }
            else if (!r.FunnelOn) { remoteStatus.Text = "準備OK (未公開)"; remoteStatus.ForeColor = Ui.Muted; }
            else { remoteStatus.Text = "● 公開中"; remoteStatus.ForeColor = Ui.Ok; }
            publicUrl = r.FunnelOn ? r.Url : "";
            remoteUrl.Text = publicUrl;
        }

        void EnableRemote()
        {
            if (!Ui.Confirm(this,
                "このPCの NextAI をインターネットに公開します。\n\n" +
                "・発行されるURL (https://〜.ts.net) を知っていれば、誰でもログイン画面を開けます。\n" +
                "・使えるのは、あなたが「メンバー」で作成したアカウントを持つ人だけです。\n" +
                "・管理機能・管理APIはインターネットからは使えません (このPCからのみ)。\n" +
                "・ログイン失敗の繰り返しは自動でロックされます。メンバーには推測されにくいパスワードを使ってもらってください。\n\n" +
                "初回は、ブラウザで Tailscale の「Funnel / HTTPS を有効にする」画面が開きます。許可してください。\n\n公開しますか？", "外部公開")) return;
            Run(async () =>
            {
                remoteStatus.Text = "公開処理中… (ブラウザが開いたら許可してください)";
                var i = await Api.GetAsync("/api/admin/server/info");
                tunnelPort = (int)i.Int("tunnel_port", 8444);
                if (tunnelPort <= 0) throw new ApiException(0, "tunnel", "server.tunnel_port が 0 のため外部公開できません (AI設定 → サーバー)");
                var log = new System.Text.StringBuilder();
                var res = await RemoteAccess.EnableAsync(tunnelPort, l => log.AppendLine(l));
                var st = await RemoteAccess.StatusAsync();
                if (!st.FunnelOn) throw new ApiException(0, "funnel", "外部公開を開始できませんでした。\n\n" + log.ToString().Trim());
                await SetPublicUrl(st.Url);
                Clipboard.SetText(st.Url);
                MessageBox.Show(this, $"公開しました。URL をクリップボードにコピーしました:\n\n{st.Url}\n\n" +
                    "「メンバー」→「メンバー作成」で友だちのアカウントを作り、招待情報 (このURLが含まれます) を渡してください。\n" +
                    "有効な証明書なので、友だちの端末で証明書の設定は不要です。", "外部公開");
            });
        }

        void DisableRemote()
        {
            if (!Ui.Confirm(this, "外部公開を停止します。インターネットからは接続できなくなります (LAN からは引き続き使えます)。", "外部公開")) return;
            Run(async () =>
            {
                await RemoteAccess.DisableAsync(null);
                await SetPublicUrl("");
            });
        }

        async Task SetPublicUrl(string url)
        {
            await Api.PutAsync("/api/admin/settings", new JObject { ["values"] = new JObject { ["server.public_url"] = url.TrimEnd('/') } });
        }

        void RunDiagnostics(bool full)
        {
            if (full && !Ui.Confirm(this, "フル診断では実際にモデルをロードして、ロード時間・VRAM使用量・推論速度・スワップ時間を計測します。\n数分かかり、その間は利用者のリクエストが待たされる場合があります。", "フル診断")) return;
            Run(async () =>
            {
                diagSummary.Text = "診断中…";
                var r = await Api.PostAsync("/api/admin/diagnostics/run", new JObject { ["full"] = full });
                var id = r.Obj("job").Str("id");
                for (var i = 0; i < 900; i++)
                {
                    await Task.Delay(1500);
                    var j = (await Api.GetAsync($"/api/admin/jobs/{id}")).Obj("job");
                    if (j.Str("status") == "done") { ShowReport(j.Obj("result")); return; }
                    if (j.Str("status") != "running" && j.Str("status") != "queued") throw new ApiException(0, "diag", "診断に失敗しました: " + j.Str("error"));
                }
            }, false);
        }

        void Backup(bool withFiles)
        {
            Run(async () =>
            {
                var r = await Api.PostAsync("/api/admin/backup", new JObject { ["include_user_files"] = withFiles });
                MessageBox.Show(this, $"作成しました: {r.Str("name")} ({Ui.Bytes(r.Num("size"))})", "バックアップ");
            });
        }

        void Restore()
        {
            var b = RequireSelection(backups, "バックアップ");
            if (b == null) return;
            var typed = Ui.Prompt(this, "復元", $"{b.Str("name")} から復元します。現在のデータベース・設定・ユーザーデータは置き換えられます\n(直前の状態は restore-rollback フォルダに退避)。続行するには RESTORE と入力:");
            if (typed == null) return;
            Run(async () =>
            {
                await Api.PostAsync("/api/admin/restore", new JObject { ["name"] = b.Str("name"), ["confirm"] = typed });
                await Task.Delay(4000);
                await WaitHealthy();
                MessageBox.Show(this, "復元が完了し、サーバーが再起動しました。", "復元");
            }, false);
        }

        void CheckUpdate()
        {
            Run(async () =>
            {
                var r = await Api.GetAsync("/api/admin/update/check");
                if (!r.Bool("configured")) { MessageBox.Show(this, $"現在のバージョン: {r.Str("current")}\n更新情報URL (server.update_manifest_url) が未設定です。", "アップデート"); return; }
                if (!r.Bool("update_available")) { MessageBox.Show(this, $"最新版です (v{r.Str("current")})", "アップデート"); return; }
                await Updater.InstallAsync(this, r);
            }, false);
        }

        static void OpenFolder(string path)
        {
            try { Process.Start(new ProcessStartInfo("explorer.exe", ProcessRunner.Quote(path)) { UseShellExecute = true }); } catch { }
        }
    }

    sealed class LogsPage : AdminPage
    {
        readonly ComboBox file;
        readonly TextBox text, filter;
        readonly CheckBox follow;
        readonly DataGridView audit;
        public override bool AutoRefresh => follow.Checked;

        public LogsPage(MainForm main) : base(main, "ログ / 監査")
        {
            file = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 240 };
            file.SelectedIndexChanged += async (s, e) => { try { await LoadLog(); } catch (Exception ex) { Ui.Error(this, ex); } };
            follow = new CheckBox { Text = "自動更新", AutoSize = true, Margin = new Padding(8, 6, 3, 3) };
            text = new TextBox { Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Both, WordWrap = false, Font = Ui.MonoFont, BackColor = Color.FromArgb(15, 23, 42), ForeColor = Color.FromArgb(226, 232, 240) };
            var lp = new Panel { Dock = DockStyle.Fill };
            lp.Controls.Add(text);
            lp.Controls.Add(Ui.Toolbar(Ui.Label("ログ:"), file, Ui.Btn("更新", async (s, e) => await LoadLog()), follow));
            filter = new TextBox { Width = 200 };
            audit = Ui.Grid(("time", "日時", 120), ("actor", "実行者", 110), ("action", "操作", 190), ("target", "対象", 170), ("ip", "IP", 110), ("details", "詳細", 0));
            var ap = new Panel { Dock = DockStyle.Fill };
            ap.Controls.Add(audit);
            ap.Controls.Add(Ui.Toolbar(Ui.Label("操作で絞り込み (例: user.*):"), filter, Ui.Btn("検索", (s, e) => Run(LoadAudit, false))));
            var split = new SplitContainer { Dock = DockStyle.Fill, Orientation = Orientation.Horizontal, SplitterDistance = 330 };
            split.Panel1.Controls.Add(Group("サーバーログ", lp));
            split.Panel2.Controls.Add(Group("監査ログ (Audit Log)", ap));
            Controls.Add(split);
        }

        public override async Task RefreshAsync()
        {
            if (file.Items.Count == 0)
            {
                var l = await Api.GetAsync("/api/admin/logs/list");
                foreach (var f in l.Arr("logs").Objects()) file.Items.Add(f.Str("name"));
                if (file.Items.Count > 0) file.SelectedItem = file.Items.Contains("server.log") ? "server.log" : file.Items[0];
                await LoadAudit();
                return;
            }
            await LoadLog();
            if (!follow.Checked) await LoadAudit();
        }

        async Task LoadLog()
        {
            if (file.SelectedItem == null) return;
            var r = await Api.GetAsync($"/api/admin/logs?name={Uri.EscapeDataString(file.SelectedItem.ToString())}&lines=800");
            text.Text = string.Join("\r\n", r.Arr("lines").Cast<object>());
            text.SelectionStart = text.TextLength;
            text.ScrollToCaret();
        }

        async Task LoadAudit()
        {
            var q = filter.Text.Trim() == "" ? "" : "&action=" + Uri.EscapeDataString(filter.Text.Trim());
            var r = await Api.GetAsync("/api/admin/audit?limit=500" + q);
            Ui.Fill(audit, r.Arr("entries").Objects(), x => new object[]
            {
                Ui.Time(x.Num("ts")), x.Str("actor_name", "-"), x.Str("action"), x.Str("target"), x.Str("ip"), x.Obj("details").Count > 0 ? Json.Serialize(x.Obj("details")) : "",
            }, (row, x) => { if (x.Str("action").Contains("failed") || x.Str("action").Contains("reuse")) row.Cells["action"].Style.ForeColor = Ui.Bad; });
        }
    }
}
