using System;
using System.Linq;
using System.Drawing;
using System.Threading.Tasks;
using System.Windows.Forms;
using NextAI.Common;

namespace NextAI.Admin
{
    sealed class MainForm : Form
    {
        public readonly InstallInfo Info;
        public readonly ApiClient Api;
        readonly TabControl tabs;
        readonly ToolStripStatusLabel connLabel, svcLabel, userLabel, verLabel, updateLabel;
        JObject pendingUpdate;
        readonly Timer timer;
        int ticks;
        bool refreshing;

        public MainForm(InstallInfo info, ApiClient api, string startPage)
        {
            Info = info;
            Api = api;
            Text = "NextAI Platform 管理コンソール";
            Icon = Ui.AppIcon();
            Font = Ui.BaseFont;
            AutoScaleMode = AutoScaleMode.Dpi;
            StartPosition = FormStartPosition.CenterScreen;
            Size = new Size(1320, 860);
            MinimumSize = new Size(980, 640);
            BackColor = Ui.PanelBg;

            tabs = new TabControl { Dock = DockStyle.Fill, Padding = new Point(14, 6) };
            AdminPage[] pages =
            {
                new DashboardPage(this), new MembersPage(this), new ModelsPage(this), new WorkersPage(this),
                new SettingsPage(this), new ServerPage(this), new LogsPage(this),
            };
            tabs.TabPages.AddRange(pages);
            tabs.SelectedIndexChanged += async (s, e) => await RefreshCurrent();

            var strip = new StatusStrip();
            connLabel = new ToolStripStatusLabel("接続中");
            svcLabel = new ToolStripStatusLabel("サービス: -");
            userLabel = new ToolStripStatusLabel($"管理者: {api.User?.Str("display_name")} ({api.User?.Str("username")})");
            verLabel = new ToolStripStatusLabel(info.Version == "" ? "" : "v" + info.Version) { Spring = true, TextAlign = ContentAlignment.MiddleRight };
            updateLabel = new ToolStripStatusLabel("") { IsLink = true, Visible = false, ForeColor = Ui.Accent };
            updateLabel.Click += async (s, e) =>
            {
                if (pendingUpdate == null) return;
                try { await Updater.InstallAsync(this, pendingUpdate); } catch (Exception ex) { Ui.Error(this, ex); }
            };
            crashLabel = new ToolStripStatusLabel("") { IsLink = true, Visible = false, ForeColor = Ui.Bad };
            crashLabel.Click += (s, e) =>
            {
                crashLabel.Visible = false;
                var logs = tabs.TabPages.OfType<LogsPage>().FirstOrDefault();
                if (logs != null) { tabs.SelectedTab = logs; logs.ShowLog("service.log"); }
            };
            strip.Items.AddRange(new ToolStripItem[] { connLabel, new ToolStripSeparator(), svcLabel, new ToolStripSeparator(), userLabel, crashLabel, updateLabel, verLabel });

            Controls.Add(tabs);
            Controls.Add(strip);

            if (startPage == "server") tabs.SelectedTab = pages[5];
            timer = new Timer { Interval = 2000 };
            timer.Tick += async (s, e) => await Tick();
            Shown += async (s, e) => { await RefreshCurrent(); timer.Start(); await CheckForUpdate(); };
            FormClosing += async (s, e) => { timer.Stop(); await Api.LogoutAsync(); };
        }

        async Task CheckForUpdate()
        {
            try
            {
                pendingUpdate = await Updater.CheckAsync(Api);
                if (pendingUpdate == null) return;
                updateLabel.Text = $"⬆ v{pendingUpdate.Str("latest")} に更新できます (クリックで更新)";
                updateLabel.Visible = true;
            }
            catch (Exception) { /* offline or no release yet: stay quiet */ }
        }

        ToolStripStatusLabel crashLabel;
        bool? lastHealth;
        string lastSvc;

        public static string ServiceText(string st) =>
            st == "Running" ? "実行中" : st == "Stopped" ? "停止中" : st == "StartPending" ? "起動中" : st == "StopPending" ? "停止処理中"
            : st == "NotInstalled" ? "未登録" : st;

        async Task Tick()
        {
            ticks++;
            if (ticks % 10800 == 0 && pendingUpdate == null) await CheckForUpdate();
            if (ticks % 3 == 0)
            {
                var st = WinService.Status();
                svcLabel.Text = "サービス: " + ServiceText(st);
                svcLabel.ForeColor = st == "Running" ? Ui.Ok : Ui.Bad;
                // The status bar and the open page must agree: probe health here too, and when the service
                // or connection state changes, reload the current page instead of leaving stale values on screen.
                var ok = await Api.HealthAsync();
                if (ok != lastHealth || st != lastSvc)
                {
                    if (ok && lastHealth == false) await CheckRecentCrash();
                    lastHealth = ok;
                    lastSvc = st;
                    SetConnected(ok, ok ? null : (st == "Running" ? "サーバーが応答しません (起動中の可能性)" : "サービスが停止しています"));
                    if (tabs.SelectedTab is AdminPage cur && !cur.AutoRefresh) await RefreshPage(cur, true);
                }
            }
            if (tabs.SelectedTab is AdminPage p && p.AutoRefresh) await RefreshPage(p, true);
        }

        string lastCrashSeen;

        /// <summary>After the server comes back, tell the admin whether it crashed (service host log) instead of
        /// leaving only a transient "disconnected" state.</summary>
        async Task CheckRecentCrash()
        {
            try
            {
                var r = await Api.GetAsync("/api/admin/logs?name=service.log&lines=80");
                var lines = r.Arr("lines").Cast<object>().Select(x => x.ToString()).ToList();
                var crash = lines.LastOrDefault(l => l.Contains("server exited with code"));
                if (crash == null || crash == lastCrashSeen) return;
                lastCrashSeen = crash;
                crashLabel.Text = "⚠ サーバーが異常終了し自動復旧しました: " + crash.Substring(0, Math.Min(crash.Length, 90)) + " (クリックでログ)";
                crashLabel.Visible = true;
            }
            catch (Exception) { /* informational only */ }
        }

        public async Task RefreshCurrent()
        {
            if (tabs.SelectedTab is AdminPage p) await RefreshPage(p, false);
        }

        async Task RefreshPage(AdminPage p, bool silent)
        {
            if (refreshing) return;
            refreshing = true;
            try
            {
                await p.RefreshAsync();
                SetConnected(true, null);
            }
            catch (ApiException ex)
            {
                SetConnected(false, ex.Message);
                if (ex.Status == 401 && !silent)
                {
                    MessageBox.Show(this, "セッションの有効期限が切れました。再度ログインしてください。", Text, MessageBoxButtons.OK, MessageBoxIcon.Information);
                    Application.Restart();
                }
                // Connection problems are shown in the status bar (and recover on their own); no pop-up for them.
                else if (!silent && ex.Status != 0) Ui.Error(this, ex);
            }
            finally { refreshing = false; }
        }

        public void SetConnected(bool ok, string message)
        {
            connLabel.Text = ok ? "● 接続中" : "● 切断: " + message;
            connLabel.ForeColor = ok ? Ui.Ok : Ui.Bad;
        }
    }

    abstract class AdminPage : TabPage
    {
        protected readonly MainForm Main;
        protected ApiClient Api => Main.Api;
        public virtual bool AutoRefresh => false;

        protected AdminPage(MainForm main, string title) : base(title)
        {
            Main = main;
            Padding = new Padding(8);
            BackColor = Ui.PanelBg;
            UseVisualStyleBackColor = false;
        }

        public abstract Task RefreshAsync();

        protected async void Run(Func<Task> action, bool refresh = true)
        {
            var old = Cursor.Current;
            try
            {
                Cursor.Current = Cursors.WaitCursor;
                await action();
                if (refresh) await RefreshAsync();
            }
            catch (Exception ex) { Ui.Error(this, ex); }
            finally { Cursor.Current = old; }
        }

        protected JObject RequireSelection(DataGridView g, string what)
        {
            var r = Ui.Selected(g);
            if (r == null) MessageBox.Show(this, $"{what}を選択してください。", "NextAI", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return r;
        }

        protected static GroupBox Group(string title, Control content, DockStyle dock = DockStyle.Fill, int height = 0)
        {
            var g = new GroupBox { Text = title, Dock = dock, Padding = new Padding(8), BackColor = Color.White };
            if (height > 0) g.Height = height;
            content.Dock = DockStyle.Fill;
            g.Controls.Add(content);
            return g;
        }
    }
}
