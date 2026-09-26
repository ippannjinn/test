using System;
using System.Drawing;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;
using NextAI.Common;

namespace NextAI.Admin
{
    sealed class DashboardPage : AdminPage
    {
        readonly MetricTile cpu, ram, vram, gpu, temp, disk, queue, model, swaps, workers, jobs, errors, status, uptime;
        readonly Sparkline cpuLine, ramLine, vramLine, tempLine;
        readonly ListBox swapList, errorList, urlList;
        readonly Label reasons;
        public override bool AutoRefresh => true;

        public DashboardPage(MainForm main) : base(main, "ダッシュボード")
        {
            MetricTile T(string title) => new MetricTile { Title = title };
            cpu = T("CPU"); ram = T("RAM"); vram = T("VRAM"); gpu = T("GPU 使用率"); temp = T("GPU 温度"); disk = T("ストレージ");
            queue = T("キュー"); model = T("ロード中のモデル"); swaps = T("モデルスワップ"); workers = T("稼働ワーカー");
            jobs = T("実行中ジョブ"); errors = T("エラー (直近)"); status = T("システム状態"); uptime = T("稼働時間 / 利用者");
            var tiles = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink, WrapContents = true };
            tiles.Controls.AddRange(new Control[] { status, cpu, ram, vram, gpu, temp, disk, queue, model, swaps, workers, jobs, errors, uptime });

            cpuLine = new Sparkline { Title = "CPU", Unit = "%" };
            ramLine = new Sparkline { Title = "RAM 使用", Unit = "%", LineColor = Color.FromArgb(14, 165, 233) };
            vramLine = new Sparkline { Title = "VRAM 使用", Unit = "MB", LineColor = Color.FromArgb(168, 85, 247) };
            tempLine = new Sparkline { Title = "GPU 温度", Unit = "°C", LineColor = Color.FromArgb(234, 88, 12) };
            var charts = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink };
            charts.Controls.AddRange(new Control[] { cpuLine, ramLine, vramLine, tempLine });

            reasons = new Label { Dock = DockStyle.Top, AutoSize = false, Height = 26, ForeColor = Ui.Warn, Padding = new Padding(8, 4, 4, 4) };
            swapList = new ListBox { IntegralHeight = false, BorderStyle = BorderStyle.None };
            errorList = new ListBox { IntegralHeight = false, BorderStyle = BorderStyle.None, HorizontalScrollbar = true };
            urlList = new ListBox { IntegralHeight = false, BorderStyle = BorderStyle.None };
            urlList.DoubleClick += (s, e) => { if (urlList.SelectedItem is string u) { Clipboard.SetText(u.Split(' ')[0]); Main.SetConnected(true, null); } };
            var bottom = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, RowCount = 1 };
            bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 34));
            bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 38));
            bottom.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 28));
            bottom.Controls.Add(Group("モデルスワップ履歴", swapList), 0, 0);
            bottom.Controls.Add(Group("警告・エラー", errorList), 1, 0);
            bottom.Controls.Add(Group("メンバー用接続URL (ダブルクリックでコピー)", urlList), 2, 0);
            Controls.Add(bottom);
            Controls.Add(reasons);
            Controls.Add(charts);
            Controls.Add(tiles);
        }

        public override async Task RefreshAsync()
        {
            var d = await Api.GetAsync("/api/admin/dashboard");
            var m = await Api.GetAsync("/api/admin/metrics?n=150");
            var r = d.Obj("resources");
            var gpus = r.Arr("gpus").Objects().ToList();
            var g = gpus.FirstOrDefault();
            var gov = d.Obj("governor");
            var q = d.Obj("queue");
            var srv = d.Obj("server");

            var level = gov.Str("level", "NORMAL");
            var levelText = level == "NORMAL" ? "正常" : level == "ELEVATED" ? "注意" : level == "HIGH" ? "高負荷" : "危険";
            status.Set(levelText, $"バックエンド: {srv.Str("backend_mode")} / Sandbox: {srv.Str("sandbox")}", null, Ui.StatusColor(level));
            status.ForeColor = Ui.StatusColor(level);
            cpu.Set($"{r.Num("cpu_percent"):F0}%", $"{r.Int("cpu_count")} スレッド", r.Num("cpu_percent") / 100);
            double ramTotal = r.Num("ram_total_mb"), ramAvail = r.Num("ram_available_mb");
            ram.Set($"{(ramTotal - ramAvail) / 1024:F1} / {ramTotal / 1024:F1} GB", $"空き {ramAvail / 1024:F1} GB / NextAI {r.Num("own_ram_mb") / 1024:F1}GB / 他 {Math.Max(0, ramTotal - ramAvail - r.Num("own_ram_mb")) / 1024:F1}GB", ramTotal > 0 ? (ramTotal - ramAvail) / ramTotal : 0);
            if (g != null)
            {
                double vt = g.Num("vram_total_mb"), vu = g.Num("vram_used_mb");
                vram.Set($"{vu / 1024:F1} / {vt / 1024:F1} GB", $"NextAI {r.Num("own_vram_mb") / 1024:F1}GB / 他 {r.Num("external_vram_mb") / 1024:F1}GB / 予算 {gov.Num("vram_budget_mb") / 1024:F1}GB", vt > 0 ? vu / vt : 0);
                gpu.Set($"{g.Num("util_percent"):F0}%", g.Str("name"), g.Num("util_percent") / 100);
                var t = g.Num("temp_c", -1);
                temp.Set(t < 0 ? "-" : $"{t:F0}°C", $"電力 {g.Num("power_w"):F0}W / Driver {g.Str("driver")}", t < 0 ? (double?)null : t / 95, t >= 88 ? Ui.Bad : t >= 83 ? Ui.Warn : Ui.Ok);
            }
            else
            {
                vram.Set("-", "GPU未検出"); gpu.Set("-", r.Str("gpu_provider")); temp.Set("-", "");
            }
            double dt = r.Num("disk_total_gb"), df = r.Num("disk_free_gb");
            disk.Set($"空き {df:F0} GB", $"全体 {dt:F0} GB (状態: {gov.Str("disk")})", dt > 0 ? (dt - df) / dt : 0, gov.Str("disk") == "ok" ? (Color?)null : Ui.Bad);
            queue.Set($"{q.Int("pending")} 待機 / {q.Int("running")} 実行", $"推定待ち {Ui.Duration(q.Num("est_wait_seconds"))} / 混雑度 {q.Num("congestion"):P0}", q.Num("congestion"));
            var loaded = d.Arr("models_loaded").Objects().ToList();
            model.Set(loaded.Count == 0 ? "なし" : loaded[0].Str("name"), loaded.Count > 1 ? $"他 {loaded.Count - 1} モデル" : loaded.Count == 1 ? string.Join(" ", loaded[0].Arr("notes").Cast<object>()) : "");
            var swapLog = d.Arr("swaps").Objects().ToList();
            swaps.Set($"{swapLog.Count} 回", q.Bool("thrashing") ? "スラッシング検出: プリフェッチ抑制中" : swapLog.Count > 0 ? $"直近 {swapLog[0].Num("seconds"):F1}s" : "", null, q.Bool("thrashing") ? Ui.Warn : (Color?)null);
            workers.Set($"{loaded.Count} モデル", $"使用中スロット {loaded.Sum(x => x.Int("in_use"))}");
            var active = d.Arr("active_jobs").Objects().ToList();
            jobs.Set($"{active.Count} 件", string.Join(", ", active.Take(3).Select(x => $"{x.Str("username")}:{x.Str("kind")}")));
            var errs = d.Arr("errors").Objects().ToList();
            errors.Set($"{errs.Count(e => e.Str("level") == "ERROR")} / {errs.Count}", errs.FirstOrDefault()?.Str("message") ?? "なし", null, errs.Any(e => e.Str("level") == "ERROR") ? Ui.Bad : (Color?)null);
            var users = d.Obj("users");
            uptime.Set(Ui.Duration(srv.Num("uptime_seconds")), $"メンバー {users.Int("total")} / オンライン {users.Int("online_15min")}");

            var reasonText = string.Join(" / ", gov.Arr("reasons").Cast<object>());
            reasons.Text = reasonText == "" ? "" : "⚠ " + reasonText;

            swapList.BeginUpdate();
            swapList.Items.Clear();
            foreach (var s in swapLog)
                swapList.Items.Add($"{Ui.Time(s.Num("ts"))}  {s.Str("loaded")}  {s.Num("seconds"):F1}s  退避: {string.Join(",", s.Arr("evicted").Cast<object>())}");
            swapList.EndUpdate();
            errorList.BeginUpdate();
            errorList.Items.Clear();
            foreach (var e in errs) errorList.Items.Add($"{Ui.Time(e.Num("ts"))} [{e.Str("level")}] {e.Str("message")}");
            errorList.EndUpdate();
            var urls = srv.Arr("urls").Objects().Select(u => $"{u.Str("url")}  ({u.Str("label")})").ToList();
            if (!urls.SequenceEqual(urlList.Items.Cast<string>()))
            {
                urlList.Items.Clear();
                foreach (var u in urls) urlList.Items.Add(u);
            }

            var samples = m.Arr("samples").Objects().ToList();
            cpuLine.SetValues(samples.Select(x => x.Num("cpu")), 100);
            ramLine.SetValues(samples.Select(x => x.Num("ram_total_mb") > 0 ? 100 * (1 - x.Num("ram_available_mb") / x.Num("ram_total_mb")) : 0), 100);
            var vmax = samples.Select(x => x.Num("vram_total_mb")).DefaultIfEmpty(1).Max();
            vramLine.SetValues(samples.Select(x => x.Num("vram_used_mb")), vmax);
            tempLine.SetValues(samples.Select(x => x.Num("gpu_temp")), 100);
        }
    }
}
