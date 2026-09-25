using System;
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
        readonly ToolStripStatusLabel connLabel, svcLabel, userLabel, verLabel;
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
            strip.Items.AddRange(new ToolStripItem[] { connLabel, new ToolStripSeparator(), svcLabel, new ToolStripSeparator(), userLabel, verLabel });

            Controls.Add(tabs);
            Controls.Add(strip);

            if (startPage == "server") tabs.SelectedTab = pages[5];
            timer = new Timer { Interval = 2000 };
            timer.Tick += async (s, e) => await Tick();
            Shown += async (s, e) => { await RefreshCurrent(); timer.Start(); };
            FormClosing += async (s, e) => { timer.Stop(); await Api.LogoutAsync(); };
        }

        async Task Tick()
        {
            ticks++;
            if (ticks % 3 == 0)
            {
                var st = WinService.Status();
                svcLabel.Text = "サービス: " + (st == "Running" ? "実行中" : st == "Stopped" ? "停止中" : st == "NotInstalled" ? "未登録" : st);
                svcLabel.ForeColor = st == "Running" ? Ui.Ok : Ui.Bad;
            }
            if (tabs.SelectedTab is AdminPage p && p.AutoRefresh) await RefreshPage(p, true);
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
                else if (!silent) Ui.Error(this, ex);
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
