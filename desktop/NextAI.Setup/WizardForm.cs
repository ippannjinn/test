using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using NextAI.Common;

namespace NextAI.Setup
{
    sealed class WizardForm : Form
    {
        public static bool Force;
        readonly InstallOptions opt = new InstallOptions();
        readonly string version = Payload.Version();
        readonly Panel content = new Panel { Dock = DockStyle.Fill, Padding = new Padding(24, 18, 24, 8), BackColor = Color.White };
        readonly Button back, next, cancel;
        readonly List<Label> navLabels = new List<Label>();
        readonly List<(string title, Panel panel, Func<bool> leave)> pages = new List<(string, Panel, Func<bool>)>();
        int page;
        SystemProbe probe;
        List<ModelSet> sets = new List<ModelSet>();
        CancellationTokenSource cts;
        bool installing, finished, installOk;

        // controls that other pages read
        DataGridView checkGrid;
        Label checkSummary;
        TextBox installDir, dataDir, serverName, adminUser, adminPw, adminPw2;
        NumericUpDown port;
        CheckBox lan, desktop, autostart, skipModels;
        ComboBox setCombo;
        Label setInfo, adminError;
        ProgressBar overall, current;
        Label currentTitle, currentDetail;
        ListBox stepList;
        TextBox logBox;
        Label finishTitle;
        TextBox finishText;
        CheckBox launchAdmin;

        public WizardForm()
        {
            Text = $"NextAI Platform セットアップ v{version}";
            Icon = Ui.AppIcon();
            Font = Ui.BaseFont;
            AutoScaleMode = AutoScaleMode.Dpi;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(900, 620);
            MinimumSize = new Size(820, 580);

            string prevVersion;
            var existing = Shell.ReadExisting(out prevVersion);
            if (existing != null)
            {
                opt.Upgrade = true;
                opt.PreviousVersion = prevVersion;
                opt.InstallDir = existing.InstallDir;
                opt.DataDir = existing.DataDir;
                opt.Port = existing.Port;
            }

            var side = new Panel { Dock = DockStyle.Left, Width = 230, BackColor = Ui.Accent, Padding = new Padding(18) };
            var nav = new FlowLayoutPanel { Dock = DockStyle.Fill, FlowDirection = FlowDirection.TopDown, BackColor = Ui.Accent };
            nav.Controls.Add(new Label { Text = "NextAI\nPlatform", ForeColor = Color.White, Font = new Font("Yu Gothic UI", 20f, FontStyle.Bold), AutoSize = true, Margin = new Padding(0, 0, 0, 4) });
            nav.Controls.Add(new Label { Text = $"v{version}  セットアップ", ForeColor = Color.FromArgb(199, 210, 254), AutoSize = true, Margin = new Padding(2, 0, 0, 24) });
            side.Controls.Add(nav);

            var footer = new FlowLayoutPanel { Dock = DockStyle.Bottom, FlowDirection = FlowDirection.RightToLeft, Height = 52, Padding = new Padding(12, 10, 12, 8), BackColor = Ui.PanelBg };
            cancel = new Button { Text = "キャンセル", Width = 110, Height = 30 };
            next = new Button { Text = "次へ >", Width = 110, Height = 30, BackColor = Ui.Accent, ForeColor = Color.White, FlatStyle = FlatStyle.Flat };
            next.FlatAppearance.BorderSize = 0;
            back = new Button { Text = "< 戻る", Width = 110, Height = 30 };
            footer.Controls.AddRange(new Control[] { cancel, next, back });
            back.Click += (s, e) => Go(page - 1);
            next.Click += (s, e) => Next();
            cancel.Click += (s, e) => Close();
            FormClosing += OnClosing;

            Controls.Add(content);
            Controls.Add(footer);
            Controls.Add(side);

            AddPage("ようこそ", WelcomePage(), null);
            AddPage("システム診断", CheckPage(), LeaveCheck);
            AddPage("インストール設定", OptionsPage(), LeaveOptions);
            if (!opt.Upgrade) AddPage("管理者アカウント", AdminPage(), LeaveAdmin);
            AddPage("インストール", InstallPage(), null);
            AddPage("完了", FinishPage(), null);
            foreach (var p in pages)
            {
                var l = new Label { Text = "  " + p.title, ForeColor = Color.FromArgb(199, 210, 254), AutoSize = false, Width = 190, Height = 30, TextAlign = ContentAlignment.MiddleLeft };
                navLabels.Add(l);
                nav.Controls.Add(l);
            }
            Go(0);
        }

        void AddPage(string title, Panel panel, Func<bool> leave)
        {
            panel.Dock = DockStyle.Fill;
            panel.Visible = false;
            content.Controls.Add(panel);
            pages.Add((title, panel, leave));
        }

        static Label H(string text) => new Label { Text = text, Font = new Font("Yu Gothic UI", 15f, FontStyle.Bold), AutoSize = true, Dock = DockStyle.Top, Padding = new Padding(0, 0, 0, 10) };
        static Label P(string text, Color? c = null) => new Label { Text = text, AutoSize = false, Dock = DockStyle.Top, Height = 44, ForeColor = c ?? SystemColors.ControlText };

        void Go(int index)
        {
            if (index < 0 || index >= pages.Count) return;
            page = index;
            for (var i = 0; i < pages.Count; i++)
            {
                pages[i].panel.Visible = i == index;
                navLabels[i].ForeColor = i == index ? Color.White : i < index ? Color.FromArgb(165, 180, 252) : Color.FromArgb(150, 160, 230);
                navLabels[i].Font = i == index ? Ui.BoldFont : Ui.BaseFont;
                navLabels[i].Text = (i < index ? "✓ " : i == index ? "▶ " : "   ") + pages[i].title;
            }
            var title = pages[index].title;
            back.Enabled = index > 0 && title != "インストール" && title != "完了";
            next.Text = title == "完了" ? "完了" : NextIsInstall() ? "インストール" : "次へ >";
            next.Enabled = title != "インストール";
            cancel.Enabled = title != "完了";
            if (title == "システム診断" && probe == null) RunProbe();
            if (title == "インストール" && !installing) StartInstall();
        }

        bool NextIsInstall() => page + 1 < pages.Count && pages[page + 1].title == "インストール";

        void Next()
        {
            if (pages[page].title == "完了") { Finish(); return; }
            if (pages[page].title == "インストール")
            {
                if (!installing && !installOk) { cancel.Text = "キャンセル"; StartInstall(); }
                return;
            }
            var leave = pages[page].leave;
            if (leave != null && !leave()) return;
            Go(page + 1);
        }

        // ------------------------------------------------------------------ pages
        Panel WelcomePage()
        {
            var p = new Panel();
            var text = opt.Upgrade
                ? $"既存のインストール (v{opt.PreviousVersion}) を v{version} に更新します。\r\n\r\n会話履歴・ファイル・メモリ・設定・ダウンロード済みモデルはすべて保持されます。データベースと設定は自動で移行されます。"
                : "このセットアップは、このPCを「自己ホスト型・マルチユーザー AI プラットフォーム」として構成します。\r\n\r\n"
                  + "・環境診断 (Windows / GPU / VRAM / RAM / ストレージ / ドライバ)\r\n"
                  + "・Python 実行環境・推論ランタイム (llama.cpp 等) の自動取得\r\n"
                  + "・このPCに適したAIモデルセットの自動選択とダウンロード (再開可能・チェックサム検証)\r\n"
                  + "・AIサーバーを Windows サービスとして登録 (起動時に自動起動・自動復旧)\r\n"
                  + "・管理コンソール (デスクトップアプリ) と、メンバー用のブラウザ接続URLの発行\r\n\r\n"
                  + "PowerShell やコマンド操作は不要です。モデルのダウンロードに時間がかかるため (合計数十GB)、安定したインターネット接続で実行してください。途中で中断しても、再実行すると続きから再開します。";
            p.Controls.Add(new TextBox { Text = text, Multiline = true, ReadOnly = true, TabStop = false, BorderStyle = BorderStyle.None, BackColor = Color.White, Dock = DockStyle.Fill, Font = new Font("Yu Gothic UI", 10.5f) });
            p.Controls.Add(H(opt.Upgrade ? "NextAI Platform の更新" : "NextAI Platform へようこそ"));
            return p;
        }

        Panel CheckPage()
        {
            var p = new Panel();
            checkGrid = Ui.Grid(("status", "結果", 60), ("name", "項目", 150), ("value", "検出値", 280), ("advice", "対処", 0));
            checkSummary = new Label { Dock = DockStyle.Bottom, Height = 46, Padding = new Padding(0, 8, 0, 0) };
            var re = Ui.Toolbar(Ui.Btn("再チェック", (s, e) => RunProbe()));
            re.Dock = DockStyle.Bottom;
            p.Controls.Add(checkGrid);
            p.Controls.Add(checkSummary);
            p.Controls.Add(re);
            p.Controls.Add(P("このPCがAIプラットフォームを長期運用できるか確認しています。FAIL がある場合はインストールできません。"));
            p.Controls.Add(H("システム診断"));
            return p;
        }

        async void RunProbe()
        {
            checkSummary.Text = "診断中…";
            checkSummary.ForeColor = Ui.Muted;
            next.Enabled = false;
            try
            {
                probe = await Task.Run(() => SystemProbe.Run(opt.DataDir, opt.Port));
                ShowProbe();
            }
            catch (Exception ex)
            {
                checkSummary.Text = "診断中にエラーが発生しました: " + ex.Message;
                checkSummary.ForeColor = Ui.Bad;
                AppendLogSafe(ex.ToString());
            }
        }

        void AppendLogSafe(string s)
        {
            try { File.AppendAllText(Path.Combine(Path.GetTempPath(), "NextAI-Setup-error.log"), DateTime.Now + " " + s + "\r\n"); } catch { }
        }

        void ShowProbe()
        {
            sets = Catalog.Load(probe.VramGb, probe.RamGb, probe.DiskFreeGb);
            var rec = Catalog.Recommend(sets);
            var need = opt.Upgrade ? 5 : Math.Max(20, rec?.SizeGb ?? 20);
            var checks = probe.Evaluate(need, 15);
            if (opt.Upgrade)
                foreach (var c in checks.Where(c => c.Id == "port" && c.Status == "WARN")) { c.Status = "PASS"; c.Value = "既存サーバーが使用中 (更新時に停止します)"; c.Advice = ""; }
            checkGrid.Rows.Clear();
            foreach (var c in checks)
            {
                var i = checkGrid.Rows.Add(c.Status, c.Name, c.Value, c.Advice);
                checkGrid.Rows[i].Cells[0].Style.ForeColor = Ui.StatusColor(c.Status == "SKIP" ? "" : c.Status);
                checkGrid.Rows[i].Cells[0].Style.Font = Ui.BoldFont;
            }
            checkGrid.ClearSelection();
            var fails = checks.Where(c => c.Status == "FAIL").ToList();
            var warns = checks.Count(c => c.Status == "WARN");
            if (fails.Any())
            {
                checkSummary.Text = "インストールできません。不足している項目: " + string.Join(" / ", fails.Select(f => $"{f.Name} ({f.Advice})"));
                checkSummary.ForeColor = Ui.Bad;
            }
            else
            {
                checkSummary.Text = warns > 0 ? $"警告が {warns} 件あります。内容を確認のうえ続行できます。推奨モデルセット: {rec?.Name}" : $"すべての要件を満たしています。推奨モデルセット: {rec?.Name}";
                checkSummary.ForeColor = warns > 0 ? Ui.Warn : Ui.Ok;
            }
            next.Enabled = !fails.Any() || Force;
            if (fails.Any() && Force) checkSummary.Text += "  (/force 指定のため続行できます)";
            RefreshSets();
        }

        bool LeaveCheck() => probe != null;

        Panel OptionsPage()
        {
            var p = new Panel();
            var t = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 3, AutoScroll = true };
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 180));
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 90));
            installDir = new TextBox { Dock = DockStyle.Fill, Text = opt.InstallDir, Enabled = !opt.Upgrade };
            dataDir = new TextBox { Dock = DockStyle.Fill, Text = opt.DataDir, Enabled = !opt.Upgrade };
            dataDir.Leave += (s, e) => { if (probe != null) RefreshSets(); };
            port = new NumericUpDown { Minimum = 1024, Maximum = 65535, Value = opt.Port, Width = 100 };
            lan = new CheckBox { Text = "LAN内の端末 (スマホ等) からの接続を許可", Checked = true, AutoSize = true };
            serverName = new TextBox { Dock = DockStyle.Fill, Text = opt.ServerName };
            setCombo = new ComboBox { Dock = DockStyle.Fill, DropDownStyle = ComboBoxStyle.DropDownList };
            setCombo.SelectedIndexChanged += (s, e) => UpdateSetInfo();
            setInfo = new Label { Dock = DockStyle.Fill, AutoSize = false, Height = 84, ForeColor = Ui.Muted };
            skipModels = new CheckBox { Text = opt.Upgrade ? "モデルセットを変更しない (既存モデルを使用)" : "モデルを後でダウンロードする", AutoSize = true, Checked = opt.Upgrade };
            desktop = new CheckBox { Text = "デスクトップにショートカットを作成", Checked = true, AutoSize = true };
            autostart = new CheckBox { Text = "ログイン時に管理コンソールを起動 (サーバーは常に自動起動)", AutoSize = true };
            Button Browse(TextBox target) => Ui.Btn("参照…", (s, e) =>
            {
                using (var d = new FolderBrowserDialog { SelectedPath = target.Text })
                    if (d.ShowDialog(this) == DialogResult.OK) target.Text = Path.Combine(d.SelectedPath, "NextAI");
            });
            void Row(string label, Control c, Control extra = null)
            {
                t.Controls.Add(new Label { Text = label, AutoSize = true, Margin = new Padding(3, 8, 3, 3) });
                t.Controls.Add(c);
                t.Controls.Add(extra ?? new Label());
            }
            Row("インストール先 (アプリ)", installDir, opt.Upgrade ? null : Browse(installDir));
            Row("データ保存先 (モデル・DB)", dataDir, opt.Upgrade ? null : Browse(dataDir));
            Row("サーバー名", serverName);
            Row("HTTPS ポート", port);
            Row("", lan);
            Row("AIモデルセット", setCombo);
            Row("", setInfo);
            Row("", skipModels);
            Row("", desktop);
            Row("", autostart);
            p.Controls.Add(t);
            p.Controls.Add(P("推奨モデルセットは、このPCの VRAM / RAM / 空き容量で長期運用できる構成です (単純に最大のモデルを選ぶわけではありません)。"));
            p.Controls.Add(H("インストール設定"));
            return p;
        }

        void RefreshSets()
        {
            if (setCombo == null) return;
            double disk = probe?.DiskFreeGb ?? 0;
            try { disk = new DriveInfo(Path.GetPathRoot(Path.GetFullPath(dataDir.Text))).AvailableFreeSpace / 1073741824.0; } catch { }
            sets = Catalog.Load(probe?.VramGb ?? 0, probe?.RamGb ?? 0, disk);
            setCombo.Items.Clear();
            foreach (var s in sets) setCombo.Items.Add(s);
            var rec = Catalog.Recommend(sets);
            setCombo.SelectedItem = rec;
            UpdateSetInfo();
        }

        void UpdateSetInfo()
        {
            if (!(setCombo.SelectedItem is ModelSet s)) return;
            double free = 0;
            try { free = new DriveInfo(Path.GetPathRoot(Path.GetFullPath(dataDir.Text))).AvailableFreeSpace / 1073741824.0; } catch { }
            setInfo.Text = $"必要容量 約{s.SizeGb:F0}GB / 空き {free:F0}GB (インストール後の残り 約{free - s.SizeGb:F0}GB、安全マージン15GBを常時維持)\r\n"
                           + $"要件: VRAM {s.NeedVram}GB / RAM {s.NeedRam}GB 以上{(s.Eligible ? "" : "  ⚠ このPCは要件を満たしていません")}\r\n"
                           + "含まれるモデル: " + string.Join(", ", s.Models);
            setInfo.ForeColor = s.Eligible && free - s.SizeGb >= 15 ? Ui.Muted : Ui.Bad;
        }

        bool LeaveOptions()
        {
            opt.InstallDir = installDir.Text.Trim();
            opt.DataDir = dataDir.Text.Trim();
            opt.Port = (int)port.Value;
            opt.Lan = lan.Checked;
            opt.ServerName = serverName.Text.Trim() == "" ? "NextAI Platform" : serverName.Text.Trim();
            opt.Set = setCombo.SelectedItem as ModelSet;
            opt.SkipModels = skipModels.Checked;
            opt.DesktopShortcut = desktop.Checked;
            opt.AutostartAdmin = autostart.Checked;
            if (!Path.IsPathRooted(opt.InstallDir) || !Path.IsPathRooted(opt.DataDir))
            {
                MessageBox.Show(this, "フォルダは絶対パスで指定してください。");
                return false;
            }
            if (opt.Set != null && !opt.SkipModels)
            {
                double free = 0;
                try { free = new DriveInfo(Path.GetPathRoot(Path.GetFullPath(opt.DataDir))).AvailableFreeSpace / 1073741824.0; } catch { }
                if (free - opt.Set.SizeGb < 15 && MessageBox.Show(this, "選択したモデルセットでは安全マージン (15GB) を確保できない可能性があります。\nダウンロード前に容量不足が判明した場合は自動で停止します。続行しますか？",
                        "容量の確認", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return false;
                if (!opt.Set.Eligible && MessageBox.Show(this, "このPCは選択したモデルセットの推奨要件を満たしていません。続行しますか？", "確認", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes) return false;
            }
            return true;
        }

        Panel AdminPage()
        {
            var p = new Panel();
            var t = new TableLayoutPanel { Dock = DockStyle.Top, ColumnCount = 2, Height = 170 };
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, 180));
            t.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            adminUser = new TextBox { Width = 260, Text = "admin" };
            adminPw = new TextBox { Width = 260, UseSystemPasswordChar = true };
            adminPw2 = new TextBox { Width = 260, UseSystemPasswordChar = true };
            adminError = new Label { AutoSize = true, ForeColor = Ui.Bad };
            void Row(string l, Control c) { t.Controls.Add(new Label { Text = l, AutoSize = true, Margin = new Padding(3, 8, 3, 3) }); t.Controls.Add(c); }
            Row("管理者ユーザー名", adminUser);
            Row("パスワード (10文字以上)", adminPw);
            Row("パスワード (確認)", adminPw2);
            Row("", adminError);
            p.Controls.Add(P("管理者はメンバーの作成・停止・削除、モデル・サーバー管理を行います。管理機能はこのPCの「NextAI 管理コンソール」からのみ利用できます。\r\nパスワードはこのPC上でハッシュ化 (Argon2id) して保存され、平文では保存されません。"));
            p.Controls.Add(t);
            p.Controls.Add(H("管理者アカウントの作成"));
            return p;
        }

        bool LeaveAdmin()
        {
            var u = adminUser.Text.Trim();
            var pw = adminPw.Text;
            string err = null;
            if (!Regex.IsMatch(u, @"^[A-Za-z0-9_.\-]{3,32}$")) err = "ユーザー名は3〜32文字の英数字・_ . - で入力してください";
            else if (pw.Length < 10) err = "パスワードは10文字以上にしてください";
            else if (pw.IndexOf(u, StringComparison.OrdinalIgnoreCase) >= 0) err = "パスワードにユーザー名を含めないでください";
            else if (pw != adminPw2.Text) err = "パスワードが一致しません";
            else if (pw.Distinct().Count() < 4) err = "より複雑なパスワードにしてください";
            adminError.Text = err ?? "";
            if (err != null) return false;
            opt.AdminUser = u;
            opt.AdminPassword = pw;
            return true;
        }

        Panel InstallPage()
        {
            var p = new Panel();
            currentTitle = new Label { Dock = DockStyle.Top, Height = 26, Font = Ui.BoldFont };
            current = new ProgressBar { Dock = DockStyle.Top, Height = 16 };
            currentDetail = new Label { Dock = DockStyle.Top, Height = 24, ForeColor = Ui.Muted };
            overall = new ProgressBar { Dock = DockStyle.Top, Height = 10 };
            stepList = new ListBox { Dock = DockStyle.Left, Width = 330, IntegralHeight = false, BorderStyle = BorderStyle.FixedSingle };
            logBox = new TextBox { Dock = DockStyle.Fill, Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Font = Ui.MonoFont, BackColor = Color.FromArgb(15, 23, 42), ForeColor = Color.FromArgb(226, 232, 240) };
            var body = new Panel { Dock = DockStyle.Fill, Padding = new Padding(0, 8, 0, 0) };
            body.Controls.Add(logBox);
            body.Controls.Add(stepList);
            p.Controls.Add(body);
            p.Controls.Add(currentDetail);
            p.Controls.Add(current);
            p.Controls.Add(currentTitle);
            p.Controls.Add(overall);
            p.Controls.Add(H("インストール中"));
            return p;
        }

        void AppendLog(string line)
        {
            if (InvokeRequired) { BeginInvoke(new Action<string>(AppendLog), line); return; }
            if (logBox.TextLength > 400000) logBox.Text = logBox.Text.Substring(200000);
            logBox.AppendText(line + "\r\n");
        }

        async void StartInstall()
        {
            installing = true;
            installOk = false;
            cts = new CancellationTokenSource();
            next.Enabled = false;
            next.Text = "次へ >";
            cancel.Text = "中止";
            var inst = new Installer(opt);
            stepList.Items.Clear();
            foreach (var s in inst.Steps) stepList.Items.Add("   " + s);
            overall.Maximum = inst.Steps.Count * 100;
            inst.Log += AppendLog;
            inst.StepState += (i, state) => BeginInvoke(new Action(() =>
            {
                var mark = state == "run" ? "▶ " : state == "done" ? "✓ " : state == "warn" ? "⚠ " : "✗ ";
                stepList.Items[i] = mark + inst.Steps[i];
                stepList.SelectedIndex = i;
                if (state == "run") { currentTitle.Text = inst.Steps[i]; current.Style = ProgressBarStyle.Marquee; currentDetail.Text = ""; }
                overall.Value = Math.Min(overall.Maximum, (i + (state == "run" ? 0 : 1)) * 100);
            }));
            inst.StepProgress += (v, detail) => BeginInvoke(new Action(() =>
            {
                if (v < 0) current.Style = ProgressBarStyle.Marquee;
                else { current.Style = ProgressBarStyle.Continuous; current.Value = (int)Math.Max(0, Math.Min(100, v * 100)); }
                currentDetail.Text = detail;
            }));
            AppendLog($"NextAI Platform v{version} {(opt.Upgrade ? "更新" : "インストール")}  {DateTime.Now:yyyy-MM-dd HH:mm}");
            AppendLog($"アプリ: {opt.InstallDir}  データ: {opt.DataDir}  ポート: {opt.Port}  モデルセット: {(opt.SkipModels ? "(スキップ)" : opt.Set?.Id)}");
            try
            {
                await inst.RunAsync(cts.Token);
                installOk = true;
                current.Style = ProgressBarStyle.Continuous;
                current.Value = 100;
                overall.Value = overall.Maximum;
                FinishPageFill(inst);
                installing = false;
                finished = true;
                Go(pages.Count - 1);
            }
            catch (OperationCanceledException)
            {
                AppendLog("中止しました。セットアップを再実行すると続きから再開できます。");
                installing = false;
                cancel.Text = "閉じる";
                next.Text = "再試行";
                next.Enabled = true;
            }
            catch (Exception ex)
            {
                AppendLog(ex.Message);
                installing = false;
                current.Style = ProgressBarStyle.Continuous;
                MessageBox.Show(this, ex.Message + "\n\n「再試行」で続きから再開できます (ダウンロード済みのデータは再利用されます)。", "セットアップ", MessageBoxButtons.OK, MessageBoxIcon.Error);
                cancel.Text = "閉じる";
                next.Text = "再試行";
                next.Enabled = true;
            }
            finally
            {
                try { File.WriteAllText(Path.Combine(opt.DataDir, "logs", "setup.log"), logBox.Text); } catch { }
            }
        }

        Panel FinishPage()
        {
            var p = new Panel();
            finishTitle = H("セットアップが完了しました");
            finishText = new TextBox { Multiline = true, ReadOnly = true, TabStop = false, Dock = DockStyle.Fill, BorderStyle = BorderStyle.None, BackColor = Color.White, Font = new Font("Yu Gothic UI", 10.5f), ScrollBars = ScrollBars.Vertical };
            launchAdmin = new CheckBox { Text = "管理コンソールを起動してメンバーを作成する", Checked = true, AutoSize = true, Dock = DockStyle.Bottom };
            var copy = Ui.Btn("接続URLをコピー", (s, e) => { if (finishText.Tag is string u && u != "") Clipboard.SetText(u); });
            copy.Dock = DockStyle.Bottom;
            p.Controls.Add(finishText);
            p.Controls.Add(copy);
            p.Controls.Add(launchAdmin);
            p.Controls.Add(finishTitle);
            return p;
        }

        void FinishPageFill(Installer inst)
        {
            var first = inst.Urls.FirstOrDefault()?.Split(' ')[0] ?? $"https://localhost:{opt.Port}/";
            finishText.Tag = first;
            var lines = new List<string>
            {
                "AIサーバーは Windows サービスとして起動しており、PC起動時に自動で起動します。", "",
                "■ メンバー用の接続URL (ブラウザ / スマートフォン)",
            };
            lines.AddRange(inst.Urls.Select(u => "   " + u));
            lines.AddRange(new[]
            {
                "", "■ 次の手順",
                "   1. 管理コンソールで管理者としてログインし、「メンバー」→「メンバー作成」",
                "   2. 表示される招待情報 (URL・ユーザー名・初期パスワード) をメンバーに渡す",
                "   3. メンバーはブラウザでログインし、「この端末を信頼する」で次回から自動ログイン",
                "", "■ 実機性能の確認",
                "   管理コンソール →「サーバー」→「フル診断」で、このPCでの VRAM 使用量・推論速度・",
                "   モデルスワップ時間・GPU温度を計測できます (結果はモデル配置の最適化に使われます)。",
                "", "■ 外部 (自宅外) からのアクセス",
                "   ルーターのポート開放ではなく Tailscale 等の VPN の利用を推奨します (docs/DEPLOYMENT.md)。",
            });
            if (inst.Warnings.Any())
            {
                lines.Add("");
                lines.Add("■ 警告");
                lines.AddRange(inst.Warnings.Select(w => "   ⚠ " + w));
            }
            finishText.Text = string.Join("\r\n", lines);
        }

        void Finish()
        {
            if (installOk && launchAdmin.Checked) Shell.LaunchUnelevated(Path.Combine(opt.InstallDir, "NextAI.Admin.exe"));
            finished = true;
            Close();
        }

        void OnClosing(object sender, FormClosingEventArgs e)
        {
            if (installing)
            {
                if (MessageBox.Show(this, "インストールを中止しますか？\n(次回セットアップを実行すると続きから再開します)", "確認", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) != DialogResult.Yes)
                {
                    e.Cancel = true;
                    return;
                }
                cts?.Cancel();
                e.Cancel = true;
                return;
            }
            if (!finished && page > 0 && pages[page].title != "インストール" && !installOk &&
                MessageBox.Show(this, "セットアップを終了しますか？", "確認", MessageBoxButtons.YesNo) != DialogResult.Yes)
                e.Cancel = true;
        }
    }
}
