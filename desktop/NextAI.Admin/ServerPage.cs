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
        readonly Label svcStatus, health, diagSummary;
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
                Ui.Btn("アップデート確認", (s, e) => CheckUpdate()),
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
            Controls.Add(grid);
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
            svcStatus.Text = "サービス: " + (st == "Running" ? "実行中" : st == "Stopped" ? "停止中" : st);
            svcStatus.ForeColor = st == "Running" ? Ui.Ok : Ui.Bad;
            var ok = await Api.HealthAsync();
            health.Text = ok ? "ヘルスチェック: OK" : "ヘルスチェック: 応答なし";
            health.ForeColor = ok ? Ui.Ok : Ui.Bad;
            if (!ok) return;
            var i = await Api.GetAsync("/api/admin/server/info");
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
                if (!r.Bool("configured"))
                {
                    MessageBox.Show(this, $"現在のバージョン: {r.Str("current")}\n更新情報URL (server.update_manifest_url) が未設定です。\n新しい NextAI-Platform-Setup.exe を実行すると、データを保持したまま更新されます。", "アップデート");
                    return;
                }
                if (!r.Bool("update_available")) { MessageBox.Show(this, $"最新版です ({r.Str("current")})", "アップデート"); return; }
                if (Ui.Confirm(this, $"新しいバージョン {r.Str("latest")} があります。\n{r.Str("notes")}\n\nダウンロードページを開きますか？ (セットアップを実行するとデータを保持したまま更新されます)", "アップデート"))
                    Process.Start(new ProcessStartInfo(r.Str("download_url")) { UseShellExecute = true });
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
