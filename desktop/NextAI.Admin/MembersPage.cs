using System;
using System.Drawing;
using System.Linq;
using System.Threading.Tasks;
using System.Windows.Forms;
using NextAI.Common;

namespace NextAI.Admin
{
    sealed class MembersPage : AdminPage
    {
        readonly DataGridView grid;

        public MembersPage(MainForm main) : base(main, "メンバー")
        {
            grid = Ui.Grid(("username", "ユーザー名", 130), ("display", "表示名", 140), ("role", "ロール", 70), ("state", "状態", 80),
                ("storage", "ストレージ", 120), ("gen", "生成(本日)", 90), ("conc", "同時実行", 70), ("prio", "優先度", 60),
                ("rate", "回/分", 60), ("sessions", "セッション", 70), ("devices", "信頼端末", 70), ("last", "最終ログイン", 110));
            grid.CellDoubleClick += (s, e) => Edit();
            var bar = Ui.Toolbar(
                Ui.Btn("＋ メンバー作成", (s, e) => Create(), true),
                Ui.Btn("編集 / クォータ", (s, e) => Edit()),
                Ui.Btn("停止", (s, e) => SetState("suspended")),
                Ui.Btn("再有効化", (s, e) => SetState("active")),
                Ui.Btn("無効化 (保持)", (s, e) => SetState("disabled")),
                Ui.Btn("完全削除", (s, e) => Delete()),
                Ui.Btn("パスワードリセット", (s, e) => ResetPassword()),
                Ui.Btn("セッション失効", (s, e) => RevokeSessions()),
                Ui.Btn("信頼端末の管理", (s, e) => Devices()),
                Ui.Btn("招待情報", (s, e) => Invitation()),
                Ui.Btn("全ユーザーのセッション失効", (s, e) => RevokeAll()),
                Ui.Btn("Claude用アカウント発行", (s, e) => IssueClaude()),
                Ui.Btn("APIトークン", (s, e) => { using (var d = new TokensDialog(Api)) d.ShowDialog(this); }),
                Ui.Btn("更新", (s, e) => Run(() => Task.CompletedTask)));
            var help = Ui.Label("状態: 有効 → 停止 (一時的・再有効化可) / 無効化 (ログイン不可・データ保持期間後に自動削除) → 完全削除 (無効化済みのみ・確認必須)", null, Ui.Muted);
            help.Dock = DockStyle.Bottom;
            Controls.Add(grid);
            Controls.Add(help);
            Controls.Add(bar);
        }

        static string StateText(string s) => s == "active" ? "有効" : s == "suspended" ? "停止中" : s == "disabled" ? "無効化" : s;

        public override async Task RefreshAsync()
        {
            var d = await Api.GetAsync("/api/admin/users");
            Ui.Fill(grid, d.Arr("users").Objects(), u => new object[]
            {
                u.Str("username"), u.Str("display_name"), u.Str("role") == "admin" ? "管理者" : u.Bool("is_agent") ? "AI (Claude)" : "メンバー", StateText(u.Str("state")),
                $"{u.Num("storage_used_mb"):F0} / {u.Int("storage_quota_mb")} MB", $"{u.Num("generation_used_today"):F0} / {u.Int("generation_quota_daily")}",
                u.Int("concurrent_jobs"), u.Int("queue_priority"), u.Int("rate_limit_per_min"), u.Int("active_sessions"), u.Int("trusted_devices"),
                Ui.Time(u.Num("last_login_at")),
            }, (row, u) => row.Cells["state"].Style.ForeColor = Ui.StatusColor(u.Str("state")));
        }

        void Create()
        {
            using (var dlg = new MemberDialog(null))
            {
                if (dlg.ShowDialog(this) != DialogResult.OK) return;
                Run(async () =>
                {
                    var res = await Api.PostAsync("/api/admin/users", dlg.Result);
                    Ui.ShowText(this, "メンバーを作成しました", res.Str("invitation"), "この内容をメンバーに安全な方法で伝えてください");
                });
            }
        }

        void IssueClaude()
        {
            var days = Ui.Prompt(this, "Claude用アカウント発行",
                "Claude Code がこのPC上でデバッグ・UI操作するための専用アカウントを発行します。\n"
                + "・メンバー「claude」(ブラウザUI用) + APIトークン (管理APIは読み取りと診断のみ)\n"
                + "・再発行すると以前のパスワードとトークンは無効になります\n\nトークンの有効日数 (1〜90):", "7");
            if (days == null) return;
            double d;
            if (!double.TryParse(days, out d) || d <= 0 || d > 90) { MessageBox.Show(this, "1〜90 の数値を入力してください"); return; }
            Run(async () =>
            {
                var r = await Api.PostAsync("/api/admin/agent-account", new JObject { ["days"] = d, ["debug"] = true });
                var env = System.Text.RegularExpressions.Regex.Replace(r.Str("env"), @"(?m)^NEXTAI_CA=.*$", "NEXTAI_CA=" + Main.Info.CaCertPath.Replace("$", "$$"));
                var dir = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".nextai");
                System.IO.Directory.CreateDirectory(dir);
                var path = System.IO.Path.Combine(dir, "claude.env");
                System.IO.File.WriteAllText(path, env.Replace("\n", "\r\n"), new System.Text.UTF8Encoding(false));
                Ui.ShowText(this, "Claude用アカウントを発行しました",
                    $"接続情報を保存しました: {path}\n\nClaude Code をこのPCで起動し、リポジトリの CLAUDE.md に従って\n"
                    + "「NextAI をデバッグして」のように依頼してください。\n\n" + env,
                    "秘密情報です。不要になったら「APIトークン」から失効してください");
            });
        }

        void Edit()
        {
            var u = RequireSelection(grid, "メンバー");
            if (u == null) return;
            using (var dlg = new MemberDialog(u))
            {
                if (dlg.ShowDialog(this) != DialogResult.OK) return;
                Run(() => Api.PatchAsync($"/api/admin/users/{u.Str("id")}", dlg.Result));
            }
        }

        void SetState(string state)
        {
            var u = RequireSelection(grid, "メンバー");
            if (u == null) return;
            var label = state == "active" ? "再有効化" : state == "suspended" ? "停止" : "無効化";
            if (state != "active" && !Ui.Confirm(this, $"{u.Str("username")} を{label}します。既存のセッションと信頼端末は即時に失効します。よろしいですか？")) return;
            Run(() => Api.PostAsync($"/api/admin/users/{u.Str("id")}/state", new JObject { ["state"] = state }));
        }

        void Delete()
        {
            var u = RequireSelection(grid, "メンバー");
            if (u == null) return;
            if (u.Str("state") != "disabled")
            {
                MessageBox.Show(this, "完全削除の前にアカウントを「無効化」してください。\n(無効化 → 保持 → 完全削除)", "完全削除", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return;
            }
            var typed = Ui.Prompt(this, "完全削除", $"{u.Str("username")} に紐づくデータをすべて消去します。元に戻せません。\n" +
                "・会話とメッセージ、長期メモリ、カスタム指示\n・アップロード / 生成したファイル、サンドボックスの作業ディレクトリ\n" +
                "・APIキー、ログイン中のセッション、信頼済み端末\n(監査ログの記録と、作成済みのバックアップ ZIP は残ります)\n\n確認のためユーザー名を入力してください:");
            if (typed == null) return;
            Run(async () =>
            {
                var r = await Api.DeleteAsync($"/api/admin/users/{u.Str("id")}", new JObject { ["confirm_username"] = typed });
                if (!r.Bool("files_removed", true))
                    MessageBox.Show(this, "一部のファイルが使用中のため、まだ消去できていません。サーバーが自動で再試行します (最大10分)。", "完全削除");
            });
        }

        void ResetPassword()
        {
            var u = RequireSelection(grid, "メンバー");
            if (u == null) return;
            if (!Ui.Confirm(this, $"{u.Str("username")} のパスワードをリセットします。新しい一時パスワードが発行され、全セッション・信頼端末が失効します。")) return;
            Run(async () =>
            {
                var res = await Api.PostAsync($"/api/admin/users/{u.Str("id")}/reset-password", new JObject { ["must_change"] = true });
                Ui.ShowText(this, "パスワードをリセットしました", res.Str("invitation"), "次回ログイン時にパスワード変更が必要です");
            });
        }

        void RevokeSessions()
        {
            var u = RequireSelection(grid, "メンバー");
            if (u == null || !Ui.Confirm(this, $"{u.Str("username")} のすべてのログインセッションを失効させますか？")) return;
            Run(async () =>
            {
                var r = await Api.PostAsync($"/api/admin/users/{u.Str("id")}/revoke-sessions");
                MessageBox.Show(this, $"{r.Int("revoked")} 件のセッションを失効しました。", "完了");
            });
        }

        void RevokeAll()
        {
            if (!Ui.Confirm(this, "全ユーザーのすべてのセッションを失効させます (このコンソールのセッションは除く)。よろしいですか？")) return;
            Run(async () =>
            {
                var r = await Api.PostAsync("/api/admin/sessions/revoke-all");
                MessageBox.Show(this, $"{r.Int("revoked")} 件のセッションを失効しました。", "完了");
            });
        }

        void Devices()
        {
            var u = RequireSelection(grid, "メンバー");
            if (u == null) return;
            using (var dlg = new DevicesDialog(Api, u)) dlg.ShowDialog(this);
            Run(() => Task.CompletedTask);
        }

        void Invitation()
        {
            var u = RequireSelection(grid, "メンバー");
            if (u == null) return;
            Run(async () =>
            {
                var r = await Api.GetAsync($"/api/admin/users/{u.Str("id")}/invitation");
                Ui.ShowText(this, "招待情報", r.Str("invitation"), "パスワードは作成時/リセット時にのみ表示されます");
            }, false);
        }
    }

    sealed class MemberDialog : Form
    {
        public JObject Result { get; private set; }
        readonly TextBox username, display, password, bio;
        readonly ComboBox role;
        readonly CheckBox mustChange;
        readonly NumericUpDown storage, gen, conc, prio, rate;
        readonly bool creating;

        public MemberDialog(JObject user)
        {
            creating = user == null;
            Text = creating ? "メンバー作成" : $"メンバー編集: {user.Str("username")}";
            Font = Ui.BaseFont;
            AutoScaleMode = AutoScaleMode.Dpi;
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = MinimizeBox = false;
            StartPosition = FormStartPosition.CenterParent;
            ClientSize = new Size(460, 520);
            var t = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 2, Padding = new Padding(12), AutoScroll = true };
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 170));
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            username = new TextBox { Dock = DockStyle.Fill, Enabled = creating, Text = user?.Str("username") ?? "" };
            display = new TextBox { Dock = DockStyle.Fill, Text = user?.Str("display_name") ?? "" };
            password = new TextBox { Dock = DockStyle.Fill, UseSystemPasswordChar = true };
            role = new ComboBox { Dock = DockStyle.Fill, DropDownStyle = ComboBoxStyle.DropDownList };
            role.Items.AddRange(new object[] { "member", "admin" });
            role.SelectedItem = user?.Str("role") ?? "member";
            mustChange = new CheckBox { Text = "初回ログイン時にパスワード変更を必須にする", Checked = true, AutoSize = true };
            NumericUpDown Num(decimal min, decimal max, decimal val) => new NumericUpDown { Minimum = min, Maximum = max, Value = Math.Max(min, Math.Min(max, val)), Dock = DockStyle.Left, Width = 120, ThousandsSeparator = true };
            storage = Num(0, 10000000, user?.Int("storage_quota_mb", 5120) ?? 5120);
            gen = Num(0, 100000, user?.Int("generation_quota_daily", 100) ?? 100);
            conc = Num(1, 16, user?.Int("concurrent_jobs", 2) ?? 2);
            prio = Num(-2, 2, user?.Int("queue_priority", 0) ?? 0);
            rate = Num(1, 10000, user?.Int("rate_limit_per_min", 30) ?? 30);
            bio = new TextBox { Dock = DockStyle.Fill, Multiline = true, Height = 60, Text = user?.Str("bio") ?? "" };
            void Row(string label, Control c) { t.Controls.Add(new Label { Text = label, AutoSize = true, Margin = new Padding(3, 8, 3, 3) }); t.Controls.Add(c); }
            Row("ユーザー名", username);
            Row("表示名", display);
            Row("ロール", role);
            if (creating)
            {
                Row("初期パスワード", password);
                t.Controls.Add(new Label());
                t.Controls.Add(new Label { Text = "空欄なら安全なパスワードを自動生成します", AutoSize = true, ForeColor = Ui.Muted });
                t.Controls.Add(new Label());
                t.Controls.Add(mustChange);
            }
            Row("ストレージ上限 (MB)", storage);
            Row("生成クォータ (回/日)", gen);
            Row("同時実行数", conc);
            Row("キュー優先度 (-2〜+2)", prio);
            Row("レート制限 (リクエスト/分)", rate);
            Row("プロフィール", bio);
            var ok = new Button { Text = creating ? "作成" : "保存", DialogResult = DialogResult.OK, AutoSize = true };
            var cancel = new Button { Text = "キャンセル", DialogResult = DialogResult.Cancel, AutoSize = true };
            var bar = new FlowLayoutPanel { Dock = DockStyle.Bottom, FlowDirection = FlowDirection.RightToLeft, AutoSize = true, Padding = new Padding(8) };
            bar.Controls.AddRange(new Control[] { cancel, ok });
            Controls.Add(t);
            Controls.Add(bar);
            AcceptButton = ok;
            CancelButton = cancel;
            ok.Click += (s, e) =>
            {
                if (creating && username.Text.Trim().Length < 3) { MessageBox.Show(this, "ユーザー名は3文字以上で入力してください"); DialogResult = DialogResult.None; return; }
                var r = new JObject
                {
                    ["display_name"] = display.Text.Trim() == "" ? username.Text.Trim() : display.Text.Trim(),
                    ["role"] = role.SelectedItem.ToString(),
                    ["storage_quota_mb"] = (long)storage.Value, ["generation_quota_daily"] = (long)gen.Value,
                    ["concurrent_jobs"] = (long)conc.Value, ["queue_priority"] = (long)prio.Value, ["rate_limit_per_min"] = (long)rate.Value,
                    ["bio"] = bio.Text,
                };
                if (creating)
                {
                    r["username"] = username.Text.Trim();
                    r["must_change_password"] = mustChange.Checked;
                    if (password.Text != "") r["password"] = password.Text;
                }
                Result = r;
            };
        }
    }

    sealed class DevicesDialog : Form
    {
        readonly ApiClient api;
        readonly JObject user;
        readonly DataGridView devices, sessions;

        public DevicesDialog(ApiClient api, JObject user)
        {
            this.api = api;
            this.user = user;
            Text = $"信頼端末とセッション: {user.Str("username")}";
            Font = Ui.BaseFont;
            AutoScaleMode = AutoScaleMode.Dpi;
            StartPosition = FormStartPosition.CenterParent;
            Size = new Size(860, 560);
            devices = Ui.Grid(("name", "端末名", 0), ("status", "状態", 80), ("created", "登録", 110), ("last", "最終利用", 110), ("ip", "最終IP", 120), ("active", "有効セッション", 90));
            sessions = Ui.Grid(("ua", "ブラウザ", 0), ("kind", "種別", 80), ("ip", "IP", 120), ("last", "最終アクセス", 110), ("created", "開始", 110));
            var split = new SplitContainer { Dock = DockStyle.Fill, Orientation = Orientation.Horizontal, SplitterDistance = 250 };
            split.Panel1.Controls.Add(devices);
            split.Panel1.Controls.Add(Ui.Toolbar(Ui.Btn("選択した端末を強制解除", async (s, e) => await Revoke()), Ui.Btn("全端末を解除", async (s, e) => await RevokeAll())));
            split.Panel2.Controls.Add(sessions);
            split.Panel2.Controls.Add(Ui.Label("アクティブなセッション"));
            Controls.Add(split);
            Shown += async (s, e) => await LoadData();
        }

        async Task LoadData()
        {
            try
            {
                var d = await api.GetAsync($"/api/admin/users/{user.Str("id")}");
                Ui.Fill(devices, d.Arr("devices").Objects(), x => new object[]
                {
                    x.Str("name"), x.Str("status") == "active" ? "有効" : x.Str("status") == "revoked" ? "解除済み" : "期限切れ",
                    Ui.Time(x.Num("created_at")), Ui.Time(x.Num("last_used_at")), x.Str("last_ip"), x.Int("active_sessions"),
                });
                Ui.Fill(sessions, d.Arr("sessions").Objects(), x => new object[]
                {
                    x.Str("user_agent"), x.Str("kind") == "admin_app" ? "管理アプリ" : "Web", x.Str("ip"), Ui.Time(x.Num("last_seen_at")), Ui.Time(x.Num("created_at")),
                });
            }
            catch (Exception ex) { Ui.Error(this, ex); }
        }

        async Task Revoke()
        {
            var d = Ui.Selected(devices);
            if (d == null || !Ui.Confirm(this, $"端末「{d.Str("name")}」の信頼を解除し、その端末のセッションを失効させますか？")) return;
            try { await api.DeleteAsync($"/api/admin/users/{user.Str("id")}/devices/{d.Str("id")}"); } catch (Exception ex) { Ui.Error(this, ex); }
            await LoadData();
        }

        async Task RevokeAll()
        {
            if (!Ui.Confirm(this, "このメンバーの全ての信頼端末を解除しますか？")) return;
            try { await api.PostAsync($"/api/admin/users/{user.Str("id")}/revoke-devices"); } catch (Exception ex) { Ui.Error(this, ex); }
            await LoadData();
        }
    }

    sealed class TokensDialog : Form
    {
        readonly ApiClient api;
        readonly DataGridView grid;

        public TokensDialog(ApiClient api)
        {
            this.api = api;
            Text = "APIトークン (自動化 / Claude)";
            Font = Ui.BaseFont;
            AutoScaleMode = AutoScaleMode.Dpi;
            StartPosition = FormStartPosition.CenterParent;
            Size = new Size(900, 420);
            grid = Ui.Grid(("user", "アカウント", 100), ("name", "名前", 110), ("scopes", "権限", 110), ("status", "状態", 70),
                ("created", "発行", 100), ("expires", "期限", 100), ("last", "最終利用", 100), ("ip", "最終IP", 0));
            Controls.Add(grid);
            Controls.Add(Ui.Toolbar(Ui.Btn("選択したトークンを失効", async (s, e) => await Revoke()),
                Ui.Label("debug 権限 = 管理APIの読み取りと診断実行のみ (変更操作は不可)", null, Ui.Muted)));
            Shown += async (s, e) => await LoadData();
        }

        async Task LoadData()
        {
            try
            {
                var d = await api.GetAsync("/api/admin/tokens");
                Ui.Fill(grid, d.Arr("tokens").Objects(), t => new object[]
                {
                    t.Str("username"), t.Str("name"), string.Join(",", t.Arr("scopes").Cast<object>()),
                    t.Str("status") == "active" ? "有効" : t.Str("status") == "revoked" ? "失効" : "期限切れ",
                    Ui.Time(t.Num("created_at")), Ui.Time(t.Num("expires_at")), Ui.Time(t.Num("last_used_at")), t.Str("last_ip"),
                }, (row, t) => row.Cells["status"].Style.ForeColor = Ui.StatusColor(t.Str("status") == "active" ? "ok" : "disabled"));
            }
            catch (Exception ex) { Ui.Error(this, ex); }
        }

        async Task Revoke()
        {
            var t = Ui.Selected(grid);
            if (t == null || !Ui.Confirm(this, "このトークンを失効させますか？")) return;
            try { await api.DeleteAsync($"/api/admin/tokens/{t.Str("id")}"); } catch (Exception ex) { Ui.Error(this, ex); }
            await LoadData();
        }
    }
}
