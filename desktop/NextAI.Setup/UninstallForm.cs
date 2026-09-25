using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Win32;
using NextAI.Common;

namespace NextAI.Setup
{
    sealed class UninstallForm : Form
    {
        readonly InstallInfo info;
        readonly RadioButton appOnly, withModels, everything;
        readonly TextBox log;
        readonly Button run, close;

        public UninstallForm(InstallInfo info)
        {
            this.info = info;
            Text = "NextAI Platform のアンインストール";
            Icon = Ui.AppIcon();
            Font = Ui.BaseFont;
            AutoScaleMode = AutoScaleMode.Dpi;
            StartPosition = FormStartPosition.CenterScreen;
            ClientSize = new Size(620, 460);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            var title = new Label { Text = "NextAI Platform を削除します", Font = new Font("Yu Gothic UI", 14f, FontStyle.Bold), AutoSize = true, Left = 20, Top = 16 };
            var sub = new Label { Text = $"アプリ: {info.InstallDir}\nデータ: {info.DataDir}", Left = 22, Top = 52, Width = 580, Height = 40, ForeColor = Ui.Muted };
            appOnly = new RadioButton { Text = "アプリケーションのみ削除 (モデル・ユーザーデータ・設定は保持)", Left = 22, Top = 100, Width = 580, Checked = true };
            withModels = new RadioButton { Text = "アプリケーションとAIモデル・ランタイムを削除 (ユーザーデータは保持)", Left = 22, Top = 128, Width = 580 };
            everything = new RadioButton { Text = "すべて削除 (会話・ファイル・メモリ・アカウント・設定・モデルを完全に削除)", Left = 22, Top = 156, Width = 580, ForeColor = Ui.Bad };
            log = new TextBox { Left = 22, Top = 192, Width = 576, Height = 210, Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Font = Ui.MonoFont };
            run = new Button { Text = "アンインストール", Left = 380, Top = 416, Width = 120, Height = 30 };
            close = new Button { Text = "キャンセル", Left = 508, Top = 416, Width = 90, Height = 30, DialogResult = DialogResult.Cancel };
            Controls.AddRange(new Control[] { title, sub, appOnly, withModels, everything, log, run, close });
            run.Click += async (s, e) => await Run();
            CancelButton = close;
        }

        void Log(string s) => log.AppendText(s + "\r\n");

        async Task Run()
        {
            if (everything.Checked)
            {
                var typed = Ui.Prompt(this, "完全削除の確認", "すべてのユーザーデータを完全に削除します。元に戻せません。\n続行するには「削除」と入力してください:");
                if (typed != "削除") return;
            }
            else if (!Ui.Confirm(this, "アンインストールを開始しますか？")) return;
            run.Enabled = close.Enabled = appOnly.Enabled = withModels.Enabled = everything.Enabled = false;
            await Task.Run(() =>
            {
                void L(string m) => BeginInvoke(new Action(() => Log(m)));
                L("サービスを停止しています…");
                if (WinService.Exists())
                {
                    WinService.StopAsync().Wait(TimeSpan.FromSeconds(70));
                    Shell.Run("sc.exe", "delete", InstallInfo.ServiceName);
                    L("  サービスを削除しました");
                }
                foreach (var p in Process.GetProcessesByName("NextAI.Admin")) { try { p.Kill(); } catch { } }
                Shell.Run("netsh.exe", "advfirewall", "firewall", "delete", "rule", "name=NextAI Platform");
                L("  ファイアウォール規則を削除しました");
                try { if (Directory.Exists(Shell.StartMenuDir)) Directory.Delete(Shell.StartMenuDir, true); } catch { }
                try { if (File.Exists(Shell.DesktopLink)) File.Delete(Shell.DesktopLink); } catch { }
                try { Shell.AdminAutostart("", false); } catch { }
                try { Registry.LocalMachine.DeleteSubKeyTree(InstallInfo.UninstallKey, false); } catch { }
                try { Registry.LocalMachine.DeleteSubKeyTree(@"SOFTWARE\NextAI", false); } catch { }
                L("  ショートカットと登録情報を削除しました");
                TryDelete(info.InstallDir, L);
                if (withModels.Checked || everything.Checked)
                {
                    TryDelete(Path.Combine(info.DataDir, "models"), L);
                    TryDelete(Path.Combine(info.DataDir, "runtime"), L);
                }
                if (everything.Checked) TryDelete(info.DataDir, L);
                else L($"  ユーザーデータは保持しました: {info.DataDir}");
                L("完了しました。");
            });
            close.Text = "閉じる";
            close.Enabled = true;
            close.DialogResult = DialogResult.OK;
        }

        static void TryDelete(string dir, Action<string> log)
        {
            if (string.IsNullOrEmpty(dir) || !Directory.Exists(dir)) return;
            var full = Path.GetFullPath(dir).TrimEnd('\\');
            if (full.Length <= 3 || full.Equals(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), StringComparison.OrdinalIgnoreCase)
                || full.Equals(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), StringComparison.OrdinalIgnoreCase))
            {
                log($"  安全のため削除をスキップしました: {full}");
                return;
            }
            for (var i = 0; i < 5; i++)
            {
                try { Directory.Delete(full, true); log($"  削除しました: {full}"); return; }
                catch (Exception ex) when (i < 4) { System.Threading.Thread.Sleep(1500); if (i == 3) log("  再試行中: " + ex.Message); }
                catch (Exception ex) { log($"  一部削除できませんでした ({ex.Message}) — 再起動後に手動で削除してください: {full}"); }
            }
        }
    }
}
