using System;
using System.Drawing;
using System.Windows.Forms;
using Microsoft.Win32;
using NextAI.Common;

namespace NextAI.Admin
{
    sealed class LoginForm : Form
    {
        readonly InstallInfo info;
        readonly TextBox url, user, pass;
        readonly CheckBox remember;
        readonly Label status, error;
        readonly Button login, startSvc;
        public ApiClient Api { get; private set; }

        public LoginForm(InstallInfo info)
        {
            this.info = info;
            Text = "NextAI 管理コンソール - ログイン";
            Icon = Ui.AppIcon();
            Font = Ui.BaseFont;
            AutoScaleMode = AutoScaleMode.Dpi;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(440, 360);

            var title = new Label { Text = "NextAI Platform", Font = new Font("Yu Gothic UI", 18f, FontStyle.Bold), ForeColor = Ui.Accent, AutoSize = true, Left = 24, Top = 18 };
            var sub = new Label { Text = "サーバー管理コンソール (管理者専用)", AutoSize = true, Left = 26, Top = 56, ForeColor = Ui.Muted };
            url = new TextBox { Left = 24, Top = 104, Width = 392, Text = info.BaseUrl };
            user = new TextBox { Left = 24, Top = 158, Width = 392 };
            pass = new TextBox { Left = 24, Top = 212, Width = 392, UseSystemPasswordChar = true };
            remember = new CheckBox { Left = 24, Top = 244, Width = 250, Text = "ユーザー名を記憶する", Checked = true };
            status = new Label { Left = 24, Top = 276, Width = 270, Height = 20, ForeColor = Ui.Muted };
            startSvc = new Button { Left = 300, Top = 270, Width = 116, Height = 28, Text = "サービスを開始", Visible = false };
            error = new Label { Left = 24, Top = 298, Width = 392, Height = 20, ForeColor = Ui.Bad };
            login = new Button { Left = 316, Top = 320, Width = 100, Height = 30, Text = "ログイン", BackColor = Ui.Accent, ForeColor = Color.White, FlatStyle = FlatStyle.Flat };
            login.FlatAppearance.BorderSize = 0;
            Controls.AddRange(new Control[] {
                title, sub,
                new Label { Text = "サーバーURL", Left = 24, Top = 86, AutoSize = true }, url,
                new Label { Text = "管理者ユーザー名", Left = 24, Top = 140, AutoSize = true }, user,
                new Label { Text = "パスワード", Left = 24, Top = 194, AutoSize = true }, pass,
                remember, status, startSvc, error, login });
            AcceptButton = login;
            login.Click += async (s, e) => await DoLogin();
            startSvc.Click += async (s, e) =>
            {
                startSvc.Enabled = false;
                status.Text = "サービスを開始しています…";
                await WinService.StartAsync();
                startSvc.Enabled = true;
                RefreshStatus();
            };
            try
            {
                using (var k = Registry.CurrentUser.OpenSubKey(@"Software\NextAI\Admin"))
                    user.Text = Convert.ToString(k?.GetValue("LastUser") ?? "");
            }
            catch { }
            Shown += (s, e) => { RefreshStatus(); (user.Text == "" ? user : pass).Focus(); };
        }

        void RefreshStatus()
        {
            var st = WinService.Status();
            var text = st == "Running" ? "実行中" : st == "Stopped" ? "停止中" : st == "NotInstalled" ? "未インストール" : st;
            status.Text = "サーバーサービス: " + text;
            status.ForeColor = st == "Running" ? Ui.Ok : Ui.Warn;
            startSvc.Visible = st == "Stopped";
        }

        async System.Threading.Tasks.Task DoLogin()
        {
            error.Text = "";
            login.Enabled = false;
            try
            {
                var api = new ApiClient(url.Text.Trim(), info.CaCertPath);
                if (!await api.HealthAsync())
                {
                    error.Text = "サーバーに接続できません (サービスが起動中か確認してください)";
                    return;
                }
                await api.LoginAsync(user.Text.Trim(), pass.Text);
                Api = api;
                if (remember.Checked)
                {
                    try
                    {
                        using (var k = Registry.CurrentUser.CreateSubKey(@"Software\NextAI\Admin"))
                            k.SetValue("LastUser", user.Text.Trim());
                    }
                    catch { }
                }
                DialogResult = DialogResult.OK;
                Close();
            }
            catch (ApiException ex) { error.Text = ex.Message; }
            catch (Exception ex) { error.Text = ex.Message; }
            finally { login.Enabled = true; pass.Text = error.Text == "" ? pass.Text : ""; }
        }
    }
}
